"""App scripts share one pair of JSON fetch helpers (``js/core/api.js``).

Four scripts once carried private copies of the same get/post-JSON helpers
with slightly different error handling. New code calls ``getJSON`` /
``postJSON``; this keeps another private copy from appearing.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web_interface"
STATIC = WEB / "static"
_COPY = re.compile(r"\bfunction\s+_?(get|post)_?json\s*\(", re.I)


def test_no_script_defines_its_own_json_fetch_helper():
    offenders = [
        str(p.relative_to(STATIC))
        for p in STATIC.rglob("*.js")
        if "vendor" not in p.parts
        and p.relative_to(STATIC).as_posix() != "js/core/api.js"
        and _COPY.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"use getJSON/postJSON from js/core/api.js: {offenders}"


def test_api_js_loads_for_app_pages_only(monkeypatch):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _web import web_client

    with web_client(monkeypatch, {"u": "admin"}, login_as="u") as client:
        assert "js/core/api.js" in client.get("/").get_data(as_text=True)
    with web_client(monkeypatch, {}) as client:
        assert "js/core/api.js" not in client.get("/about").get_data(as_text=True)
