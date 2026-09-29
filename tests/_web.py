"""Shared helpers for tests that drive the Flask app through its test client.

Accounts are stubbed, never persisted: ``user_manager.get_user`` is patched to
serve the given usernames (every other lookup falls through to the real store).
"""

from contextlib import contextmanager


@contextmanager
def web_client(monkeypatch, users: dict[str, str], login_as: str | None = None):
    """A test client for the full app with CSRF off and stub accounts.

    Args:
        monkeypatch: The test's pytest ``monkeypatch`` fixture.
        users: ``{username: role}`` for the stub accounts to serve.
        login_as: Username to log in as before yielding, if any.

    Yields:
        The Flask test client.
    """
    from web_interface.auth import accounts
    from web_interface.auth.accounts import User
    from web_interface.fyp_data_hub import app

    orig_get_user = accounts.user_manager.get_user

    def _fake_get(uid):
        if uid in users:
            return User(username=uid, role=users[uid], password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(accounts.user_manager, "get_user", _fake_get)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        if login_as is not None:
            login(test_client, login_as)
        yield test_client


def login(client, username: str) -> None:
    """Log ``client`` in as ``username`` (a stub account from ``web_client``)."""
    with client.session_transaction() as sess:
        sess["_user_id"] = username
        sess["_fresh"] = True
