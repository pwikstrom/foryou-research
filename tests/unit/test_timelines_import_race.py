"""The timelines worker must not resolve a cold alias shim inside a pool thread.

2026-08-15 prod: 5-7 collections per batch died with "cannot import name
'analyse_timeline' from 'fyp.timeline_analysis'" even though the symbol exists.
``process_one_collection`` imported it lazily, so nine pool threads raced the
first resolution. Two threads importing different branches of the ``fyp`` tree
can deadlock CPython's per-module locks, and the deadlock detector resolves that
by returning a PARTIALLY-INITIALIZED module instead of raising — long enough to
observe ``fyp/timeline_analysis.py`` before its closing
``sys.modules[__name__] = _real``. Reproduced 8-times-in-9 with a barrier.

Two independent guards, both static so they cannot flake:
  * the worker imports the canonical subpackage path (no shim, no swap window);
  * every lazy import it makes is also made by ``_warm_worker_imports``, which
    runs single-threaded before the pool starts.
"""

import ast
import pathlib

import pytest

from web_interface import run_timelines_refresh

WORKER_SRC = pathlib.Path(run_timelines_refresh.__file__).read_text()
TREE = ast.parse(WORKER_SRC)

# Flat fyp.<name> modules that are alias shims doing the sys.modules swap.
SHIMS = {
    p.stem
    for p in (pathlib.Path(run_timelines_refresh.__file__).parents[1] / "fyp").glob("*.py")
    if p.stem != "__init__" and "sys.modules[__name__]" in p.read_text()
}


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in run_timelines_refresh")


def _imported_modules(fn: ast.FunctionDef) -> set[str]:
    """Every module name imported inside a function body."""
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def test_shims_were_actually_detected():
    """Guard the guard: if the shim scan finds nothing the tests below are vacuous."""
    assert "timeline_analysis" in SHIMS, f"shim scan broken (found {len(SHIMS)})"


def test_pool_worker_imports_no_alias_shim():
    offenders = sorted(
        mod for mod in _imported_modules(_function("process_one_collection"))
        if mod.startswith("fyp.") and mod.split(".")[1] in SHIMS
        and len(mod.split(".")) == 2
    )
    assert not offenders, (
        "process_one_collection runs in a ThreadPoolExecutor; these flat imports "
        f"resolve an alias shim and can race on a cold interpreter: {offenders}. "
        "Import the fyp.<subpackage>.<module> path instead."
    )


def test_warm_up_covers_every_lazy_import_the_pool_worker_makes():
    warmed = _imported_modules(_function("_warm_worker_imports"))
    # from fyp.analysis import timeline_analysis  ->  covers fyp.analysis.timeline_analysis
    warmed |= {f"{m}.{a.name}"
               for node in ast.walk(_function("_warm_worker_imports"))
               if isinstance(node, ast.ImportFrom) and (m := node.module)
               for a in node.names}

    missing = sorted(
        mod for mod in _imported_modules(_function("process_one_collection"))
        if mod not in warmed
    )
    assert not missing, (
        "these modules are first resolved inside a pool thread and are not "
        f"pre-warmed: {missing}. Add them to _warm_worker_imports()."
    )


def test_warm_up_runs_before_the_pool_is_created():
    body = ast.get_source_segment(WORKER_SRC, _function("_process_batch")) or ""
    warm_at = body.find("_warm_worker_imports()")
    pool_at = body.find("ThreadPoolExecutor(")
    assert warm_at != -1, "_process_batch no longer warms the imports"
    assert pool_at != -1, "_process_batch no longer creates a ThreadPoolExecutor"
    assert warm_at < pool_at, "the warm-up must run before the pool is created"


@pytest.mark.parametrize("_run", range(3))
def test_the_real_import_trio_survives_a_thread_barrier(_run):
    """End-to-end: the imports the worker actually makes, raced 9 ways.

    Passes trivially once the modules are warm in this interpreter — its value
    is that it fails loudly if someone reintroduces a shim path AND the suite
    happens to reach it cold.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(9)

    def work(_):
        barrier.wait()
        import fyp.data_io  # noqa: F401
        from fyp.analysis.timeline_analysis import analyse_timeline  # noqa: F401
        return True

    with ThreadPoolExecutor(max_workers=9) as pool:
        assert all(pool.map(work, range(9)))
