"""Home-tab getting-started panel (S3 item 2).

Renders the logged-in SPA shell for users with different permission sets and
settings, asserting the panel is permission-keyed and dismissible. Same
stubbed-user approach as ``test_endpoint_gates.py``.
"""

import pytest

_TEST_VIEWER = "__home_test_viewer__"


@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_VIEWER, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user
    state = {"settings": {}}

    def _fake_get(uid):
        if uid == _TEST_VIEWER:
            user = User(username=_TEST_VIEWER, role=ROLE_VIEWER, password_hash="",
                        approved=True, settings=dict(state["settings"]))
            return user
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        test_client._settings_state = state
        yield test_client






def _login(client, username):
    with client.session_transaction() as sess:
        sess["_user_id"] = username
        sess["_fresh"] = True






def _grant_permissions(monkeypatch, perms):
    from web_interface import auth

    monkeypatch.setattr(auth.role_manager, "get_role_permissions", lambda role: list(perms))






def test_panel_renders_for_default_viewer(client, monkeypatch):
    from web_interface.permissions import DEFAULT_NON_ADMIN_PERMISSIONS

    _grant_permissions(monkeypatch, DEFAULT_NON_ADMIN_PERMISSIONS)
    _login(client, _TEST_VIEWER)

    res = client.get("/")
    assert res.status_code == 200
    html = res.data.decode()
    assert 'id="getting-started-panel"' in html
    assert "Getting started" in html
    # All five analysis-tab cards for the default viewer grant
    for label in ("Explore", "Timelines", "Video Analysis", "Correlations", "Semantic Space"):
        assert label in html
    # No ingestion permission -> no upload pointer
    assert "Have participant data to add?" not in html
    # Guide is linked
    assert "/guide" in html






def test_upload_pointer_needs_ingestion_permission(client, monkeypatch):
    _grant_permissions(monkeypatch, ["tab.explore", "tab.data_management.ingestion"])
    _login(client, _TEST_VIEWER)

    html = client.get("/").data.decode()
    assert "Have participant data to add?" in html
    # Cards for tabs the user does NOT hold are omitted
    assert "<h3>Timelines</h3>" not in html






def test_panel_hidden_after_dismissal(client, monkeypatch):
    from web_interface.permissions import DEFAULT_NON_ADMIN_PERMISSIONS

    _grant_permissions(monkeypatch, DEFAULT_NON_ADMIN_PERMISSIONS)
    client._settings_state["settings"] = {"getting_started_dismissed": True}
    _login(client, _TEST_VIEWER)

    html = client.get("/").data.decode()
    assert 'id="getting-started-panel"' not in html




def test_dismissed_panel_can_be_restored_from_preferences(client, monkeypatch):
    """The dismissal used to be one-way — nothing ever wrote the flag back."""
    from web_interface.permissions import DEFAULT_NON_ADMIN_PERMISSIONS

    _grant_permissions(monkeypatch, DEFAULT_NON_ADMIN_PERMISSIONS)
    client._settings_state["settings"] = {"getting_started_dismissed": True}
    _login(client, _TEST_VIEWER)

    html = client.get("/").data.decode()
    # My stuff -> Preferences, and the help modal, both offer a way back.
    assert 'id="getting-started-restore"' in html
    assert "restoreGettingStarted()" in html






def test_shell_carries_the_platform_url_templates(client, monkeypatch):
    """Client-side "open on platform" links resolve from this map.

    Without it the Semantic Space drill-down has no way to build a correct
    per-platform URL, which is how the hardcoded TikTok link survived.
    """
    from fyp.ingest import platform_url_templates
    from web_interface.permissions import DEFAULT_NON_ADMIN_PERMISSIONS

    _grant_permissions(monkeypatch, DEFAULT_NON_ADMIN_PERMISSIONS)
    _login(client, _TEST_VIEWER)

    html = client.get("/").data.decode()
    assert "window.PLATFORM_URL_TEMPLATES" in html
    for platform in platform_url_templates():
        assert platform in html






def test_shell_renders_the_relocated_surfaces_for_a_plain_viewer(client, monkeypatch):
    """Markup that moved or is new must reach a non-admin, not just an admin."""
    from web_interface.permissions import DEFAULT_NON_ADMIN_PERMISSIONS

    _grant_permissions(monkeypatch, DEFAULT_NON_ADMIN_PERMISSIONS)
    _login(client, _TEST_VIEWER)

    html = client.get("/").data.decode()
    # Methods note moved off Explore onto My Studies.
    assert 'id="study-methods-modal"' in html
    assert 'id="explorer-v2-methods-btn"' not in html
    # Read-only study definitions need the modal in the DOM.
    assert 'id="editStudyModal"' in html
    # Correlations is deliberately minimal: no Sample/filter panel (the study
    # is the sample), no split control, no disattenuation toggle.
    assert 'id="pca-view-toggle"' in html
    assert 'id="corr-sample-summary"' not in html
    assert 'id="pca-split-select"' not in html
    assert 'id="pca-disattenuate"' not in html
    # Video Analysis drill-down miss notice.
    assert 'id="viewer-drilldown-notice"' in html
