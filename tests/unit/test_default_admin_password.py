"""First-run default admin gets a random one-time password, not "admin".

Guards the public-release security fix in ``UserManager._ensure_default_admin``:
an empty user store creates admin@admin.net with a ``secrets``-generated
password printed once to the console, and a non-empty store creates nothing.
"""

from __future__ import annotations

import web_interface.auth as auth






def _manager_with_files(monkeypatch, files: list[str]) -> auth.UserManager:
    """Build a lazy UserManager and stub the user-store listing."""
    manager = auth.UserManager(bootstrap=False)
    monkeypatch.setattr(auth.data_io, "listdir", lambda **kwargs: files)
    return manager






def test_empty_store_creates_admin_with_random_password(monkeypatch, capsys):
    """No user files → admin created; password is random and printed once."""
    manager = _manager_with_files(monkeypatch, ["roles.json"])
    created = {}

    def fake_add_user(username, password, role, approved=False, **kwargs):
        created.update(username=username, password=password, role=role, approved=approved)

    monkeypatch.setattr(manager, "add_user", fake_add_user)
    manager._ensure_default_admin()

    assert created["username"] == "admin@admin.net"
    assert created["role"] == auth.ROLE_ADMIN
    assert created["approved"] is True
    assert created["password"] != "admin"
    assert len(created["password"]) >= 16
    out = capsys.readouterr().out
    assert created["password"] in out
    assert "admin@admin.net" in out






def test_populated_store_creates_nothing(monkeypatch):
    """Any existing user file suppresses the default admin entirely."""
    manager = _manager_with_files(monkeypatch, ["alice@example.org.json"])
    monkeypatch.setattr(
        manager, "add_user",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("add_user must not be called")),
    )
    manager._ensure_default_admin()






def test_listing_failure_creates_nothing(monkeypatch):
    """A failed listing must never fabricate an admin."""
    manager = auth.UserManager(bootstrap=False)

    def boom(**kwargs):
        raise OSError("storage down")

    monkeypatch.setattr(auth.data_io, "listdir", boom)
    monkeypatch.setattr(
        manager, "add_user",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("add_user must not be called")),
    )
    manager._ensure_default_admin()
