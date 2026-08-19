"""The /api/status TTL cache and single-listing task-status reader.

2026-08-19: every open tab polls /api/status once per second, and on Cloud Run
each poll cost ~40-50 GCS round-trips (process_stats reload + an exists()+
load_json() pair per CLOUD_TASK_ELIGIBLE process + a list_blobs scan for
study_refresh). Under load the polls queued into the 10s range and competed
with real requests for the single gunicorn process. The fix: one list_blobs
pass for all status files, and a 3s single-flight cache around the assembled
payload — Cloud Run only, so local dev keeps its free always-fresh path.

The cache stores the UNREDACTED payload and redaction pops keys per request,
so the copy-before-redact step is load-bearing: without it the first viewer
poll would strip task_args from the payload every admin receives for the rest
of the TTL window.

Uses the Flask test client with stubbed users (same approach as
``test_admin_settings_route.py``).
"""

import pytest

from web_interface.routes import process_routes

_TEST_ADMIN = "__status_test_admin__"
_TEST_VIEWER = "__status_test_viewer__"


@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_ADMIN, ROLE_VIEWER, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_ADMIN:
            return User(username=uid, role=ROLE_ADMIN, password_hash="", approved=True)
        if uid == _TEST_VIEWER:
            return User(username=uid, role=ROLE_VIEWER, password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _fresh_cache():
    process_routes._status_cache["payload"] = None
    process_routes._status_cache["ts"] = 0.0
    yield
    process_routes._status_cache["payload"] = None
    process_routes._status_cache["ts"] = 0.0


def _login(client, uid):
    with client.session_transaction() as sess:
        sess["_user_id"] = uid
        sess["_fresh"] = True


def _stub_build(monkeypatch, calls):
    def _build():
        calls.append(1)
        return {
            "pca_refresh": {
                "state": "idle",
                "task_args": {"study": "secret_study"},
                "last_run_study": "secret_study",
            }
        }

    monkeypatch.setattr(process_routes, "_build_status_payload", _build)


def test_cloud_run_polls_share_one_build_within_ttl(client, monkeypatch):
    """Two polls inside the TTL window must trigger exactly one build."""
    calls = []
    _stub_build(monkeypatch, calls)
    monkeypatch.setattr(process_routes, "is_cloud_run", lambda: True)
    _login(client, _TEST_ADMIN)

    first = client.get("/api/status").get_json()
    second = client.get("/api/status").get_json()

    assert len(calls) == 1
    assert first == second
    assert first["pca_refresh"]["task_args"] == {"study": "secret_study"}


def test_viewer_redaction_does_not_poison_the_cache(client, monkeypatch):
    """A viewer poll strips task_args from its own response only — an admin
    poll inside the same TTL window must still see the full entry."""
    calls = []
    _stub_build(monkeypatch, calls)
    monkeypatch.setattr(process_routes, "is_cloud_run", lambda: True)

    _login(client, _TEST_VIEWER)
    viewer_payload = client.get("/api/status").get_json()
    assert "task_args" not in viewer_payload["pca_refresh"]
    assert "last_run_study" not in viewer_payload["pca_refresh"]

    _login(client, _TEST_ADMIN)
    admin_payload = client.get("/api/status").get_json()
    assert len(calls) == 1  # same cached build served both
    assert admin_payload["pca_refresh"]["task_args"] == {"study": "secret_study"}
    assert admin_payload["pca_refresh"]["last_run_study"] == "secret_study"


def test_local_dev_stays_uncached(client, monkeypatch):
    """Off Cloud Run the payload is rebuilt per request — the in-memory build
    is free and must stay perfectly fresh for local subprocess state."""
    calls = []
    _stub_build(monkeypatch, calls)
    monkeypatch.setattr(process_routes, "is_cloud_run", lambda: False)
    _login(client, _TEST_ADMIN)

    client.get("/api/status")
    client.get("/api/status")
    assert len(calls) == 2


class _FakeBlob:
    def __init__(self, name, payload=b"{}"):
        self.name = name
        self._payload = payload

    def download_as_bytes(self):
        if self._payload is None:
            raise RuntimeError("boom")
        return self._payload


class _FakeBucket:
    def __init__(self, blobs):
        self._blobs = blobs
        self.prefixes_seen = []

    def list_blobs(self, prefix):
        self.prefixes_seen.append(prefix)
        return [b for b in self._blobs if b.name.startswith(prefix)]


def test_read_all_task_statuses_single_listing(monkeypatch):
    """One list_blobs pass; cancel files and unreadable blobs are skipped;
    keys are the filename stems (including keyed study_refresh files)."""
    bucket = _FakeBucket(
        [
            _FakeBlob("cache/task_status/pca_refresh.json", b'{"state": "running"}'),
            _FakeBlob(
                "cache/task_status/study_refresh__mystudy.json",
                b'{"state": "running"}',
            ),
            _FakeBlob("cache/task_status/pca_refresh_cancel.json"),
            _FakeBlob("cache/task_status/broken.json", None),
            _FakeBlob("cache/task_status/notes.txt"),
        ]
    )
    import fyp.fyp_config

    # Setting a real attribute shadows the module __getattr__ that lazily
    # serves fyp_cf; monkeypatch removes it again afterwards.
    monkeypatch.setattr(
        fyp.fyp_config,
        "fyp_cf",
        {"data_io": {"bucket": bucket}, "gcs_paths": {"cache": "cache"}},
        raising=False,
    )

    statuses = process_routes._read_all_task_statuses()

    assert bucket.prefixes_seen == ["cache/task_status/"]
    assert set(statuses) == {"pca_refresh", "study_refresh__mystudy"}
    assert statuses["pca_refresh"] == {"state": "running"}
