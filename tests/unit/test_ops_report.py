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


# --------------------------------------------------- log + queue analysis
#
# All three pin fixes made after the 2026-08-27 report, which turned one
# incident (four 500s on an oversized video response) into three findings: it
# counted the request log entries a second time as "6 ERROR entries", it left
# the platform warning that actually explained them uncollected, and the
# narrative then tied two unrelated instance-drain aborts to the 500s eight
# hours away. The same report called 52 TikTok items parked for 14 days "no
# unusual queue growth".

def test_log_ts_parses_nanosecond_stamps():
    """Cloud Logging stamps have 9 fractional digits; fromisoformat refuses."""
    from web_interface.services.ops_report import _log_ts

    ts = _log_ts({"timestamp": "2026-08-27T11:12:26.603430292Z"})
    assert ts is not None and ts.year == 2026 and ts.microsecond == 603430
    assert _log_ts({"timestamp": "not a time"}) is None
    assert _log_ts({}) is None


def test_instance_drain_aborts_are_recognised():
    from web_interface.services.ops_report import _is_instance_drain, _log_ts

    term = _log_ts({"timestamp": "2026-08-27T11:12:25.291670Z"})
    abort = {"timestamp": "2026-08-27T11:12:26.603430292Z",
             "textPayload": "Uncaught signal: 6, pid=2, tid=25, fault_addr=0."}

    assert _is_instance_drain(abort, [term]) is True
    # The same abort with no teardown behind it is a real crash.
    assert _is_instance_drain(abort, []) is False
    # A teardown hours away must not launder it either.
    stale = _log_ts({"timestamp": "2026-08-27T03:10:25.000000Z"})
    assert _is_instance_drain(abort, [stale]) is False
    # Anything that is not an abort stays an error regardless.
    assert _is_instance_drain(
        {"timestamp": "2026-08-27T11:12:26.6Z", "textPayload": "boom"}, [term]) is False


def test_stalled_queue_is_flagged_even_when_length_is_unchanged():
    """The incident case: 52 items, drain last successful 17 days earlier."""
    from datetime import datetime, timezone

    from web_interface.services.ops_report import _stalled_queues

    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    stats = {
        "queue_scraper_tiktok": {"last_success": "2026-08-10T19:48:07+00:00"},
        "queue_scraper_youtube": {"last_success": "2026-08-27T09:00:00+00:00"},
    }
    lines = _stalled_queues(
        {"scrape_tiktok": 52, "scrape_youtube": 4, "scrape_instagram": 0,
         "annotate": 0},
        stats, now)

    assert len(lines) == 1
    assert "scrape_tiktok: 52 item(s) waiting" in lines[0]
    assert "queue_scraper_tiktok" in lines[0]
    # An empty queue is never stalled, however long its drain has been quiet.
    assert not _stalled_queues({"scrape_tiktok": 0}, {}, now)
    # A queue whose drain has never run at all still counts.
    assert _stalled_queues({"annotate": 7}, {}, now)[0].startswith("annotate: 7")


class _FakeResponse:
    def __init__(self, entries):
        self._entries = entries

    def raise_for_status(self):
        pass

    def json(self):
        return {"entries": self._entries}


def test_log_summary_drops_request_logs_and_drains(monkeypatch):
    """One incident must produce one finding, with its explanation attached."""
    import google.auth
    import google.auth.transport.requests as gart

    from web_interface.services import ops_report

    filters = []
    abort = {"timestamp": "2026-08-27T11:12:26.603430292Z",
             "textPayload": "Uncaught signal: 6, pid=2, tid=25, fault_addr=0."}
    real = {"timestamp": "2026-08-27T04:00:00.000000Z",
            "textPayload": "Traceback: something actually broke"}

    class _FakeSession:
        def __init__(self, creds):
            pass

        def post(self, url, json=None, timeout=None):
            flt = json["filter"]
            filters.append(flt)
            if "Handling signal: term" in flt:
                return _FakeResponse(
                    [{"timestamp": "2026-08-27T11:12:25.291670Z",
                      "textPayload": "[1] [INFO] Handling signal: term"}])
            if "httpRequest.status>=500" in flt:
                return _FakeResponse([{
                    "timestamp": "2026-08-27T03:10:28.349273Z",
                    "httpRequest": {"status": 500,
                                    "requestUrl": "https://x/api/video/s/1"}}])
            if "varlog%2Fsystem" in flt:
                return _FakeResponse([
                    {"textPayload": "Response size was too large."},
                    {"textPayload": "Response size was too large."}])
            return _FakeResponse([abort, real] if "fyp-data-hub" in flt else [])

    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("K_SERVICE", "fyp-data-hub")
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (object(), "proj"))
    monkeypatch.setattr(gart, "AuthorizedSession", _FakeSession)

    errors, fivexx, notes = ops_report._cloud_run_log_summary(24)

    # The 500's own request-log entry is never counted as a second problem.
    error_filters = [f for f in filters if "severity>=ERROR" in f]
    assert error_filters and all("logName!=" in f and "%2Frequests" in f
                                 for f in error_filters)
    # The drain abort is filtered out; the genuine error survives.
    hub = errors["fyp-data-hub"]
    assert len(hub) == 1 and "actually broke" in hub[0]
    assert len(fivexx) == 1 and "/api/video/" in fivexx[0]
    # The platform's own explanation is collected, deduped, and counted.
    assert notes == ["platform: Response size was too large. (x2)"]


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
