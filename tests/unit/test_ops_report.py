"""Registration, rendering, and route gates for the daily ops report."""

import pytest


# --------------------------------------------------------------- wiring

def test_ops_report_registered_everywhere():
    """The classic four-places registration must be complete."""
    from fyp.fyp_config import OPS_REPORT_SCRIPT
    from web_interface import process_manager
    from web_interface.routes import process_routes

    assert OPS_REPORT_SCRIPT.name == "run_ops_report.py"
    assert OPS_REPORT_SCRIPT.exists()
    assert "ops_report" in process_manager.CLOUD_TASK_ELIGIBLE
    assert "ops_report" in process_manager.processes

    process_routes._ensure_task_functions_loaded()
    assert "ops_report" in process_routes.TASK_FUNCTIONS
    # Deliberate: a queue retry would re-send the report email.
    assert "ops_report" not in process_routes.QUEUE_RETRY_SAFE


def test_ops_report_permission_key_registered():
    from web_interface.permissions import (
        ALL_PERMISSION_KEYS,
        PERMISSION_KEY_IMPLIED_GRANTS,
    )
    assert "tab.admin.ops_report" in ALL_PERMISSION_KEYS
    assert "tab.admin.ops_report" in \
        PERMISSION_KEY_IMPLIED_GRANTS["tab.admin.system_info"]


# --------------------------------------------------------------- render

def _minimal_doc(**overrides):
    doc = {
        "generated_at": "2026-08-27T00:00:00+00:00",
        "generated_at_local": "Thursday 27 August 2026, 10:00 AEST",
        "previous_run_at": None,
        "stats": [{"label": "Scrape queue · tiktok", "value": 3,
                   "status": "blue", "sub": "+1 since last report"}],
        "sections": [{"title": "Users & access", "checks": [
            {"title": "Accounts", "status": "blue",
             "summary": "2 real accounts", "details": []},
            {"title": "Pending approval", "status": "red",
             "summary": "1 account(s) awaiting approval",
             "details": ["<script>alert(1)</script>@example.com"]},
        ]}],
        "overall": "red",
        "counts": {"green": 0, "blue": 1, "yellow": 0, "red": 1},
    }
    doc.update(overrides)
    return doc


def test_render_html_escapes_content():
    from web_interface.services.ops_report import render_html

    page = render_html(_minimal_doc(), "## Action needed\n- Fix <b>this</b> now")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "Fix &lt;b&gt;this&lt;/b&gt; now" in page
    assert "lamp-red" in page and "Scrape queue · tiktok" in page
    assert page.lstrip().startswith("<!doctype html>")


def test_render_email_html_is_gmail_safe():
    from web_interface.services.ops_report import render_email_html

    page = render_email_html(_minimal_doc(), "## Action needed\n- Fix `it`")
    # Gmail drops CSS custom properties and class-based rules entirely.
    assert "var(--" not in page
    assert "<style" not in page
    assert 'class="' not in page
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "#9679;" in page  # the coloured status dots
    assert "Scrape queue · tiktok" in page


def test_fallback_narrative_lists_reds():
    from web_interface.services.ops_report import _fallback_narrative

    md = _fallback_narrative(_minimal_doc())
    assert "## Action needed" in md
    assert "Pending approval" in md


def test_md_to_html_subset():
    from web_interface.services.ops_report import _md_to_html

    out = _md_to_html("## Head\n- one `x`\n- **two**\n\npara")
    assert "<h3>Head</h3>" in out
    assert "<code>x</code>" in out and "<strong>two</strong>" in out
    assert out.count("<ul>") == 1 and out.count("</ul>") == 1
    assert "<p>para</p>" in out


# --------------------------------------------------------------- routes

_TEST_ADMIN = "__ops_report_test_admin__"
_TEST_VIEWER = "__ops_report_test_viewer__"


@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_ADMIN, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_ADMIN:
            return User(username=_TEST_ADMIN, role=ROLE_ADMIN,
                        password_hash="", approved=True)
        if uid == _TEST_VIEWER:
            return User(username=_TEST_VIEWER, role="viewer",
                        password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


def _login(client, username):
    with client.session_transaction() as sess:
        sess["_user_id"] = username
        sess["_fresh"] = True


def test_ops_report_routes_require_permission(client, monkeypatch):
    from web_interface import auth
    monkeypatch.setattr(auth.role_manager, "get_role_permissions",
                        lambda role: [])
    _login(client, _TEST_VIEWER)
    assert client.get("/api/admin/ops-report").status_code == 403
    assert client.get("/api/admin/ops-report/html").status_code == 403
    assert client.post("/api/admin/ops-report/run").status_code == 403


def test_ops_report_meta_and_html_for_admin(client, monkeypatch):
    import fyp.data_io as data_io

    stored = {
        "ops_report/latest.json": {
            "generated_at": "2026-08-27T00:00:00+00:00",
            "generated_at_local": "Thursday",
            "overall": "green", "counts": {"green": 1},
            "narrative_source": "fallback", "narrative": "secret-prose",
        },
    }
    monkeypatch.setattr(
        data_io, "load_json",
        lambda storage_location="", filename="", **kw: stored.get(filename))
    monkeypatch.setattr(
        data_io, "load_text",
        lambda storage_location="", filename="", **kw:
        "<!doctype html><html><body>report</body></html>"
        if filename == "ops_report/latest.html" else None)

    _login(client, _TEST_ADMIN)
    res = client.get("/api/admin/ops-report")
    assert res.status_code == 200
    body = res.get_json()
    assert body["available"] is True and body["overall"] == "green"
    assert "narrative" not in body

    res = client.get("/api/admin/ops-report/html")
    assert res.status_code == 200
    assert res.mimetype == "text/html"
    assert b"report" in res.data


def test_ops_report_html_404_when_missing(client, monkeypatch):
    import fyp.data_io as data_io
    monkeypatch.setattr(data_io, "load_text",
                        lambda storage_location="", filename="", **kw: None)
    _login(client, _TEST_ADMIN)
    assert client.get("/api/admin/ops-report/html").status_code == 404
