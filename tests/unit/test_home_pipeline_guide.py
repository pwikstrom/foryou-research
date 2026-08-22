"""The logged-in home pane is a permission-filtered user guide.

The pane walks the Hub's pipeline stages, and it must walk only the stages the
account can actually reach — an analysis-only user has no business reading
about ingestion or the permission matrix. It also carries the same footer
anonymous visitors get, including the copy-ready citation.
"""

import pytest

_TEST_USER = "__pipeline_guide_test__"


@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_VIEWER, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_USER:
            # Dismissed, so the assertions below see the guide itself rather
            # than the one-shot orientation panel that sits above it.
            return User(username=_TEST_USER, role=ROLE_VIEWER, password_hash="",
                        approved=True, settings={"getting_started_dismissed": True})
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


def _render(client, monkeypatch, perms):
    from web_interface import auth

    monkeypatch.setattr(auth.role_manager, "get_role_permissions", lambda role: list(perms))
    with client.session_transaction() as sess:
        sess["_user_id"] = _TEST_USER
        sess["_fresh"] = True
    res = client.get("/")
    assert res.status_code == 200
    return res.data.decode()


def test_analysis_only_user_sees_only_the_analyse_stage(client, monkeypatch):
    from web_interface.permissions import DEFAULT_NON_ADMIN_PERMISSIONS

    html = _render(client, monkeypatch, DEFAULT_NON_ADMIN_PERMISSIONS)

    assert 'id="see-panel-analyse"' in html
    for hidden in ("ingest", "enrich", "annotate", "share"):
        assert f'id="see-panel-{hidden}"' not in html
    # A lone stage has nothing to step between, so the pill row is dropped.
    assert "see-tabs--single" in html
    assert "--see-step-count: 1;" in html


def test_full_access_walks_all_five_stages(client, monkeypatch):
    html = _render(client, monkeypatch, ["*"])

    for step in ("ingest", "enrich", "annotate", "analyse", "share"):
        assert f'id="see-panel-{step}"' in html
    assert "--see-step-count: 5;" in html
    assert "see-tabs--single" not in html


def test_analyse_rows_follow_the_individual_tab_grants(client, monkeypatch):
    """Holding Explore must not surface the Sessions or Semantic Space screens."""
    html = _render(client, monkeypatch, ["tab.explore"])

    assert "Compare across the corpus" in html
    for hidden in ("Zoom in on a single sitting", "Map the whole corpus",
                   "Follow a feed over time", "Find structure and patterns"):
        assert hidden not in html


def test_pipeline_stage_needs_a_page_inside_it(client, monkeypatch):
    """Scrape alone lights up Enrich and nothing else in Data Pipeline."""
    html = _render(client, monkeypatch, ["tab.explore", "tab.data_management.scrape"])

    assert 'id="see-panel-enrich"' in html
    assert 'id="see-panel-ingest"' not in html
    assert 'id="see-panel-annotate"' not in html


def test_home_pane_ends_with_the_public_footer_and_a_citation(client, monkeypatch):
    from web_interface.permissions import DEFAULT_NON_ADMIN_PERMISSIONS

    html = _render(client, monkeypatch, DEFAULT_NON_ADMIN_PERMISSIONS)

    assert 'class="public-footer"' in html
    assert 'id="footer-cite"' in html
    assert 'id="footer-cite-apa"' in html
    assert 'id="footer-cite-bibtex"' in html
    assert "@software{" in html
    # Promotional / housekeeping blocks that used to sit under the screenshots.
    assert "Open source" not in html
    # The footer carries no copyright line at all (removed 2026-08-22).
    assert "public-footer-legal" not in html
    assert "&copy;" not in html


def test_home_pane_no_longer_advertises_the_retired_sample_dataset(client, monkeypatch):
    """The synthetic demo dataset was removed in 023185c6; the copy followed it."""
    from web_interface.permissions import DEFAULT_NON_ADMIN_PERMISSIONS

    html = _render(client, monkeypatch, DEFAULT_NON_ADMIN_PERMISSIONS)
    assert "sample study" not in html


def test_visible_pipeline_steps_are_returned_in_pipeline_order(monkeypatch):
    """Grants arrive in arbitrary order; the stepper needs pipeline order."""
    from web_interface import auth
    from web_interface.permissions import visible_pipeline_steps

    class _User:
        is_authenticated = True
        role = "some_role"

        def is_admin(self):
            return False

    monkeypatch.setattr(auth.role_manager, "get_role_permissions", lambda role: [
        "tab.admin.roles", "tab.explore", "tab.data_management.ingestion",
    ])
    assert visible_pipeline_steps(_User()) == ["ingest", "analyse", "share"]
