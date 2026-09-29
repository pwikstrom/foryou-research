"""Unexpected errors reach the client as a reference id, never as exception text.

Exception text can carry file paths, bucket names or data, so a route must not
answer with ``str(e)`` from a broad ``except Exception``: it lets the error
escape to the app-wide handler, which logs the traceback under a short id and
returns only that id. Narrow handlers for our own exception types (a
``ValueError`` raised for bad input, a conflict error) may show their message.
"""

import ast
import logging
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web_interface"

# Broad handlers allowed to put the exception into what they return, and why.
ALLOWED = {
    "services/system_health.py": "the admin-only System Health page shows each probe's "
    "failure detail by design",
    "services/worker_status.py": "the admin worker card shows why the cookie probe failed",
}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(ast.unparse(t) in ("Exception", "BaseException") for t in types)


def _leaks(path: Path, base: Path = WEB) -> list[str]:
    found = set()
    for handler in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name or not _is_broad(handler):
            continue
        for node in ast.walk(handler):
            is_response = isinstance(node, ast.Return) and node.value is not None
            is_jsonify = isinstance(node, ast.Call) and getattr(node.func, "id", None) == "jsonify"
            if (is_response or is_jsonify) and any(
                isinstance(n, ast.Name) and n.id == handler.name
                for n in ast.walk(node.value if is_response else node)
            ):
                found.add(f"{path.relative_to(base)}:{node.lineno}")
    return sorted(found)


def test_no_broad_handler_returns_its_exception_text():
    leaks = []
    for sub in ("routes", "services"):
        for path in sorted((WEB / sub).rglob("*.py")):
            if str(path.relative_to(WEB)) not in ALLOWED:
                leaks += _leaks(path)
    assert leaks == [], "return a reference id instead (see routes/_errors.py): " + ", ".join(leaks)


def test_the_scan_catches_a_leak(tmp_path):
    src = tmp_path / "r.py"
    src.write_text(
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except Exception as e:\n"
        "        return jsonify({'error': str(e)}), 500\n"
        "    except ValueError as e:\n"
        "        return jsonify({'error': str(e)}), 400\n"
    )
    assert _leaks(src, base=tmp_path) == ["r.py:5"]


@pytest.fixture
def app_with_failing_routes(monkeypatch):
    """A fresh app from the real factory, plus two routes that raise."""
    import web_interface.fyp_data_hub as hub

    app = hub.create_app()
    app.config["TESTING"] = True

    @app.route("/api/__test_raises")
    def _api_raises():
        raise RuntimeError("secret path /srv/data/x.parquet")

    @app.route("/__test_page_raises")
    def _page_raises():
        raise RuntimeError("secret path /srv/data/x.parquet")

    return app


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.text = []

    def emit(self, record):
        self.text.append(logging.Formatter().format(record))


def test_unexpected_api_error_returns_only_a_reference(app_with_failing_routes):
    capture = _Capture()
    logging.getLogger("web_interface.errors").addHandler(capture)
    try:
        res = app_with_failing_routes.test_client().get("/api/__test_raises")
    finally:
        logging.getLogger("web_interface.errors").removeHandler(capture)
    assert res.status_code == 500
    body = res.get_json()
    assert set(body) == {"error"}
    assert body["error"].startswith("Internal error (ref ")
    ref = body["error"][len("Internal error (ref ") : -1]
    assert len(ref) == 8
    assert "secret" not in res.get_data(as_text=True)
    logged = "\n".join(capture.text)
    assert f"ref {ref}" in logged and "secret path" in logged and "Traceback" in logged


def test_unexpected_page_error_is_a_plain_500_and_http_errors_pass_through(
    app_with_failing_routes,
):
    client = app_with_failing_routes.test_client()
    res = client.get("/__test_page_raises")
    assert res.status_code == 500
    assert "secret" not in res.get_data(as_text=True)
    assert client.get("/api/__no_such_route").status_code == 404


def test_every_status_error_response_also_carries_error():
    """``{"status": "error"}`` bodies keep the documented ``"error"`` key too."""
    missing = []
    for path in sorted(WEB.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value if isinstance(k, ast.Constant) else None for k in node.keys]
            if "status" not in keys:
                continue
            status = node.values[keys.index("status")]
            if isinstance(status, ast.Constant) and status.value == "error" and "error" not in keys:
                missing.append(f"{path.relative_to(WEB)}:{node.lineno}")
    assert missing == []
