"""Every function-level relative import in web_interface must resolve.

Module-level relative imports fail loudly at app boot, but function-level
(lazy) ones only explode when their endpoint is finally hit — the
2026-07-21 prod 500 on /api/manage/enrichment/refresh-downstream came from a
``from ..process_manager import ...`` two levels deep in
``routes/management/enrichment.py`` that had needed three dots since the
management-package split. This test AST-walks every module and resolves each
relative import's target without executing any route code.
"""

import ast
import importlib.util
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[2] / "web_interface"
PACKAGE_ROOT = "web_interface"






def _module_name(path: Path) -> str:
    rel = path.relative_to(WEB_ROOT.parent).with_suffix("")
    return ".".join(rel.parts)






def _resolve_relative(current_module: str, node: ast.ImportFrom) -> str:
    """The absolute module a ``from ...x import y`` inside ``current_module`` targets."""
    package_parts = current_module.split(".")[:-1]
    if node.level > len(package_parts):
        return ""
    base = package_parts[:len(package_parts) - node.level + 1]
    return ".".join(base + ([node.module] if node.module else []))






def test_all_relative_imports_resolve():
    failures = []
    for path in WEB_ROOT.rglob("*.py"):
        module = _module_name(path)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level == 0:
                continue
            target = _resolve_relative(module, node)
            if not target or importlib.util.find_spec(target) is None:
                failures.append(
                    f"{path.relative_to(WEB_ROOT.parent)}:{node.lineno} — "
                    f"'from {'.' * node.level}{node.module or ''} import ...' "
                    f"resolves to {target or '<beyond package root>'!r}, which does not exist")
    assert not failures, "Unresolvable relative imports:\n" + "\n".join(failures)
