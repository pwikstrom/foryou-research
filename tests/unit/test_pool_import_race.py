"""No thread-pool worker body may resolve a flat ``fyp.<name>`` alias shim.

The flat modules in ``fyp/`` are alias shims whose last statement is
``sys.modules[__name__] = _real``. That swap is a window: while one thread is
inside the shim body, ``sys.modules['fyp.<name>']`` still holds an EMPTY module.
Normally a second thread would just block on the module lock — but when two
threads resolve different branches of the ``fyp`` tree at once, CPython's
per-module-lock deadlock detector breaks the cycle by handing back the
partially-initialized module instead of raising. The victim then fails with
"cannot import name X from fyp.<name>" even though the symbol plainly exists.

Measured on this tree (9-12 threads, cold interpreter, barrier-synchronised):

    one cold shim, alone in the body ................. 0 of 9 threads fail
    one cold shim after any other cold import ........ 0-2 of 9 fail (flaky)
    two cold shims in one body ....................... 11 of 12 fail
    two cold CANONICAL paths in one body ............. 0 of 12 fail

So the hazard is the shim's swap window, not concurrency as such, and the fix
is to import ``fyp.<subpackage>.<module>`` from any code a pool can run.
Prod lost 5-7 collections per timelines batch to this on 2026-08-15;
``test_timelines_import_race.py`` guards that specific worker, this file
generalises the rule to every pool in the tree.

Module-level shim imports are FINE — they resolve once, single-threaded, at
import time. Only function-level (lazy) imports on a pool path are the problem.
"""

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# Flat fyp/<name>.py modules that perform the sys.modules swap.
SHIMS = {
    p.stem
    for p in (REPO / "fyp").glob("*.py")
    if p.stem != "__init__" and "sys.modules[__name__]" in p.read_text()
}

# The mandated lazy accessors (see DEVELOPING.md "Import-Cycle Rule"). They exist to
# break the fyp_config cycle, are called on essentially every code path, and are
# warm long before any pool starts.
EXEMPT_FUNCTIONS = {"_cf", "_data_io", "_gcf"}

# Pool bodies reached through an interface, which no static call graph can
# resolve: machine_annotation.call_machine_threads submits
# ``backend.annotate_one`` once per item, up to ``backend.max_workers``.
DISPATCHED_POOL_BODIES = [
    ("fyp/annotation/backends/gemini.py", "annotate_one"),
    ("fyp/annotation/backends/qwen_api.py", "annotate_one"),
    ("fyp/annotation/backends/qwen_local.py", "annotate_one"),
    ("fyp/annotation/backends/minicpm_local.py", "annotate_one"),
]

SOURCE_FILES = sorted(
    list((REPO / "fyp").rglob("*.py")) + list((REPO / "web_interface").rglob("*.py"))
)


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text())


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    """Every function in a module, nested ones included, keyed by bare name."""
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node)
    return out


def _shim_imports(fn: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, module)`` for each flat-shim import in a function body."""
    if getattr(fn, "name", None) in EXEMPT_FUNCTIONS:
        return []
    found = []
    for node in ast.walk(fn):
        # Skip nested functions that are themselves exempt accessors.
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
            if node.module == "fyp":
                # `from fyp import media_paths` imports the fyp.media_paths shim.
                names += [f"fyp.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        for mod in names:
            parts = mod.split(".")
            if len(parts) == 2 and parts[0] == "fyp" and parts[1] in SHIMS:
                found.append((node.lineno, mod))
    return found


def _pool_targets(tree: ast.AST) -> set[str]:
    """Names passed as the callable to ``.submit(fn, ...)`` / ``.map(fn, ...)``."""
    out = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("submit", "map")
                and node.args):
            first = node.args[0]
            if isinstance(first, ast.Name):
                out.add(first.id)
            elif isinstance(first, ast.Attribute):
                out.add(first.attr)
    return out


def _reachable(funcs: dict[str, ast.AST], start: str, depth: int = 3) -> set[str]:
    """``start`` plus the same-module functions it can call, to ``depth``."""
    seen: set[str] = set()
    stack = [(start, 0)]
    while stack:
        name, d = stack.pop()
        if name in seen or name not in funcs or d > depth:
            continue
        seen.add(name)
        for node in ast.walk(funcs[name]):
            if isinstance(node, ast.Call):
                callee = None
                if isinstance(node.func, ast.Name):
                    callee = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    callee = node.func.attr
                if callee in funcs:
                    stack.append((callee, d + 1))
    return seen


def test_shim_scan_is_not_vacuous():
    """Guard the guard: if the shim set is empty every assertion below passes."""
    assert len(SHIMS) > 20, f"shim detection broke (found {len(SHIMS)})"
    assert {"timeline_analysis", "machine_annotation", "data_io"} <= SHIMS


@pytest.mark.parametrize("relpath,fname", DISPATCHED_POOL_BODIES,
                         ids=[p.split("/")[-1] for p, _ in DISPATCHED_POOL_BODIES])
def test_dispatched_pool_body_imports_no_alias_shim(relpath, fname):
    """Each backend's annotate_one runs in call_machine_threads' pool."""
    path = REPO / relpath
    funcs = _functions(_parse(path))
    assert fname in funcs, f"{fname}() not found in {relpath}"

    offenders = sorted(
        (line, mod)
        for reachable in _reachable(funcs, fname)
        for line, mod in _shim_imports(funcs[reachable])
    )
    assert not offenders, (
        f"{relpath}::{fname} runs in a thread pool "
        f"(machine_annotation.call_machine_threads, up to backend.max_workers "
        f"threads); these lazy imports resolve a flat alias shim and can be "
        f"handed a partially-initialized module: {offenders}. "
        f"Import the canonical fyp.<subpackage>.<module> path instead."
    )


def test_no_pool_worker_in_the_tree_imports_an_alias_shim():
    """Sweep every ThreadPoolExecutor in fyp/ and web_interface/.

    Resolves each ``submit``/``map`` callable within its own module and follows
    same-module calls a few levels deep, so a helper the worker calls is covered
    too (this is what catches ab_eval's ``annotate_one`` -> ``_build_contents``).
    """
    offenders: list[str] = []
    for path in SOURCE_FILES:
        src = path.read_text()
        if "ThreadPoolExecutor" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        funcs = _functions(tree)
        rel = path.relative_to(REPO)
        for target in _pool_targets(tree):
            if target not in funcs:
                continue  # imported callable or builtin - not ours to check
            for reachable in _reachable(funcs, target):
                for line, mod in _shim_imports(funcs[reachable]):
                    offenders.append(
                        f"{rel}:{line} {reachable}() imports {mod} "
                        f"(pool body: {target}())"
                    )

    assert not offenders, (
        "thread-pool worker bodies must not lazily import a flat fyp.<name> "
        "alias shim - a cold shim resolved from two pool threads can yield a "
        "partially-initialized module:\n  " + "\n  ".join(sorted(offenders))
        + "\nUse the canonical fyp.<subpackage>.<module> path."
    )


def test_module_level_shim_imports_are_still_allowed():
    """The rule is about LAZY imports; module-level ones resolve single-threaded.

    machine_annotation imports several shims at module level and must keep
    working - if this ever fails, the rule above has been over-applied.
    """
    tree = _parse(REPO / "fyp" / "annotation" / "machine_annotation.py")
    top_level = {
        alias.name
        for node in tree.body if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert any(m.split(".")[-1] in SHIMS for m in top_level), (
        "expected machine_annotation to still import shims at module level"
    )


@pytest.mark.parametrize("_run", range(3))
def test_backend_annotate_one_import_shapes_survive_a_barrier(_run):
    """End-to-end: race the imports the backends actually make, 12 ways.

    Trivially green once the modules are warm in this interpreter; its value is
    failing loudly if a shim path is reintroduced AND the suite reaches it cold.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(12)

    def work(_):
        barrier.wait()
        from fyp.annotation import annotation_versioning  # noqa: F401
        from fyp.annotation.annotation_schema import (  # noqa: F401
            get_annotation_json_schema,
        )
        from fyp.annotation.machine_annotation import initialize_machine  # noqa: F401
        from fyp.core import media_paths  # noqa: F401
        return True

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert all(pool.map(work, range(12)))
