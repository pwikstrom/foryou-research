"""Approving a warn-only (already ingested) file keeps its ledger record."""

import pytest

_ADMIN = "__approve_route_admin__"


@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_ADMIN, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _ADMIN:
            return User(username=_ADMIN, role=ROLE_ADMIN, password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)
    from web_interface import auth
    monkeypatch.setattr(auth.role_manager, "get_role_permissions",
                        lambda role: ["tab.data_management.ingestion"])
    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["_user_id"] = _ADMIN
            sess["_fresh"] = True
        yield test_client


class _Main:
    def __init__(self, files):
        self.ledger = {"schema_version": 1, "files": files}
        self.removed = []
        self.saved = 0

    def remove_from_ledger(self, filename):
        self.removed.append(filename)
        return self.ledger["files"].pop(filename, None) is not None

    def save_ledger(self):
        self.saved += 1


def _wire(monkeypatch, main):
    from fyp.core import structure_sentinel as ss
    from web_interface.routes.management import ingestion as mod

    monkeypatch.setattr(ss, "approve_file", lambda filename, reviewed_by: {
        "status": "approved", "platform": "tiktok", "source": "ddp"})
    monkeypatch.setattr(mod, "get_main_collection", lambda verbose=False: main)
    monkeypatch.setattr(mod.activity_log, "record", lambda **kw: None)


@pytest.mark.parametrize("outcome,removed", [
    ("quarantined_structure", True),   # withheld: drop so the next run reloads it
    ("added_as_new", False),           # ingested with a warning: the record stays
])
def test_approve_drops_ledger_entry_only_for_withheld_files(client, monkeypatch, outcome, removed):
    main = _Main({"f.json": {"outcome": outcome, "uploaded_by": "someone"}})
    _wire(monkeypatch, main)
    resp = client.post("/api/manage/ingestion/structure/approve", json={"filename": "f.json"})
    assert resp.status_code == 200, resp.get_json()
    assert (main.removed == ["f.json"]) is removed
    assert (main.saved == 1) is removed
    assert ("f.json" in main.ledger["files"]) is not removed
