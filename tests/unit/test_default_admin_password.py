"""First-run default admin gets a random one-time password, not "admin".

Guards the public-release security fix in ``UserManager._ensure_default_admin``:
an empty user store creates admin@admin.net with a ``secrets``-generated
password printed once to the console, and a non-empty store creates nothing.
"""

from __future__ import annotations

import web_interface.auth as auth






def _manager_with_files(monkeypatch, files: list[str], contents: dict | None = None) -> auth.UserManager:
    """Build a lazy UserManager and stub the user-store listing + reads.

    ``contents`` maps filename → parsed JSON for the content-aware default-admin
    check; unlisted candidate files load as ``{}`` (no ``username`` → not a user).
    """
    contents = contents or {}
    manager = auth.UserManager(bootstrap=False)
    monkeypatch.setattr(auth.data_io, "listdir", lambda **kwargs: files)
    monkeypatch.setattr(
        auth.data_io, "load_json",
        lambda storage_location, filename, **kwargs: contents.get(filename, {}),
    )
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
    """A real user file (has a ``username``) suppresses the default admin."""
    manager = _manager_with_files(
        monkeypatch,
        ["alice@example.org.json"],
        contents={"alice@example.org.json": {"username": "alice@example.org"}},
    )
    monkeypatch.setattr(
        manager, "add_user",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("add_user must not be called")),
    )
    manager._ensure_default_admin()






def test_reset_store_with_only_sidecar_files_creates_admin(monkeypatch, capsys):
    """Deleting the admin file but leaving sidecar JSON must recreate the admin.

    Reproduces the reset-lockout: the user removes admin@admin.net.json to mint
    a fresh admin, but var_presentation.json / irrelevant_words.json / the
    activity log remain. None carries a ``username``, so the store is genuinely
    user-less and a new admin must be created.
    """
    manager = _manager_with_files(
        monkeypatch,
        [
            "roles.json",
            "var_presentation.json",
            "irrelevant_words.json",
            "admin_settings.json",
            "admin@admin.net_log.json",
            "some_collection_tags.json",
        ],
        contents={
            "var_presentation.json": {"version": 1, "surfaces": {}},
            "irrelevant_words.json": {"words": []},
            "admin_settings.json": {"signup_enabled": False},
        },
    )
    created = {}
    monkeypatch.setattr(
        manager, "add_user",
        lambda username, password, role, approved=False, **k: created.update(
            username=username, role=role, approved=approved),
    )
    manager._ensure_default_admin()

    assert created.get("username") == "admin@admin.net"
    assert created.get("role") == auth.ROLE_ADMIN
    assert "admin@admin.net" in capsys.readouterr().out






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
