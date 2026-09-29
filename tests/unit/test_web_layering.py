"""The web layer's package boundaries hold.

Routes and workers sit above the service layer: ``services/`` and ``tasks/``
never import a route module or the Flask-facing auth modules, and no worker
imports a route module (docs/web_interface.md, "Layering").
"""

import ast
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web_interface"

FLASK_AUTH = ("web_interface.auth.security", "web_interface.auth.permissions")
ROUTES = "web_interface.routes"


def _imported_modules(path: Path) -> set[str]:
    """Absolute dotted names of everything ``path`` imports (lazy imports included)."""
    package = list(path.relative_to(WEB.parent).with_suffix("").parts[:-1])
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join(base + (node.module.split(".") if node.module else []))
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def _violations(subpackage: str, banned: tuple[str, ...]) -> list[str]:
    out = []
    for path in sorted((WEB / subpackage).rglob("*.py")):
        for module in _imported_modules(path):
            if any(module == b or module.startswith(b + ".") for b in banned):
                out.append(f"{path.relative_to(WEB)} imports {module}")
    return out


def test_services_and_tasks_import_no_routes_or_flask_auth():
    banned = (ROUTES, *FLASK_AUTH)
    assert _violations("services", banned) == []
    assert _violations("tasks", banned) == []


def test_workers_import_no_routes():
    assert _violations("workers", (ROUTES,)) == []


def test_the_scan_sees_lazy_and_relative_imports():
    # A route module's relative, function-level imports resolve to absolute names.
    found = _imported_modules(WEB / "routes" / "process_routes.py")
    assert "web_interface.tasks.runtime" in found
    assert "web_interface.auth.permissions" in found
