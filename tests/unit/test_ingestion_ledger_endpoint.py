"""GET /api/manage/ingestion/ledger — gate + payload shape (S3 item 1, UI)."""

from types import SimpleNamespace

import pytest

from tests._web import login, web_client

_TEST_VIEWER = "__ledger_test_viewer__"


@pytest.fixture
def client(monkeypatch):
    from web_interface.auth.accounts import ROLE_VIEWER

    with web_client(monkeypatch, {_TEST_VIEWER: ROLE_VIEWER}) as test_client:
        yield test_client


_login = login


def _grant_permissions(monkeypatch, perms):
    from web_interface.auth import accounts

    monkeypatch.setattr(accounts.role_manager, "get_role_permissions", lambda role: list(perms))


_FAKE_LEDGER = {
    "schema_version": 1,
    "files": {
        "new.zip": {
            "outcome": "added_as_new",
            "raw_rows": 100,
            "processed_rows": 90,
            "kept_rows": 85,
            "deduped_rows": 5,
            "dropped": {"not_parseable": 8, "missing_required": 2},
            "platform": "tiktok",
            "source": "ddp",
            "ts_last_seen": "2026-07-30T02:00:00+00:00",
        },
        "legacy.zip": {  # pre-extension entry: no processed/deduped/dropped
            "outcome": "fully_deduped",
            "raw_rows": 50,
            "kept_rows": 0,
            "platform": "instagram",
            "source": "ddp",
            "ts_last_seen": "2026-01-01T00:00:00+00:00",
        },
    },
}


def _stub_main_collection(monkeypatch):
    from web_interface.routes.management import ingestion

    monkeypatch.setattr(
        ingestion,
        "get_main_collection",
        lambda verbose=False: SimpleNamespace(ledger=dict(_FAKE_LEDGER)),
    )


def test_ledger_requires_auth(client):
    res = client.get("/api/manage/ingestion/ledger")
    assert res.status_code in (302, 401)


def test_ledger_requires_permission(client, monkeypatch):
    _grant_permissions(monkeypatch, [])
    _login(client, _TEST_VIEWER)
    res = client.get("/api/manage/ingestion/ledger")
    assert res.status_code == 403


def test_ledger_payload_shape_and_order(client, monkeypatch):
    _grant_permissions(monkeypatch, ["tab.data_management.ingestion"])
    _stub_main_collection(monkeypatch)
    _login(client, _TEST_VIEWER)

    res = client.get("/api/manage/ingestion/ledger")
    assert res.status_code == 200
    payload = res.get_json()
    assert payload["count"] == 2

    files = payload["files"]
    # Newest first by ts_last_seen
    assert [f["filename"] for f in files] == ["new.zip", "legacy.zip"]
    assert files[0]["dropped"] == {"not_parseable": 8, "missing_required": 2}
    # Legacy entries pass through without the new keys (UI renders em-dashes)
    assert "dropped" not in files[1]


def test_ledger_platform_filter(client, monkeypatch):
    _grant_permissions(monkeypatch, ["tab.data_management.ingestion"])
    _stub_main_collection(monkeypatch)
    _login(client, _TEST_VIEWER)

    res = client.get("/api/manage/ingestion/ledger?platform=instagram")
    payload = res.get_json()
    assert payload["count"] == 1
    assert payload["files"][0]["filename"] == "legacy.zip"
