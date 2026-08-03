"""The ``focus_item_id`` lookup on /api/video_analysis/ids.

The Semantic Space drill-down needs to land on one specific video. The client
cannot resolve its position itself — it only ever holds a 1000-row chunk, and
``item_id`` is an identifier column so it cannot be passed as a filter — so the
route answers with the item's index in the fully filtered, sorted order.

Uses the Flask test client with a stubbed user (same approach as
``test_correlations_api.py``); study data is monkeypatched, so no parquets are
needed.
"""

import pandas as pd
import pytest

_TEST_VIEWER = "__viewer_focus_test_user__"
_STUDY = "focus_study"
_ENDPOINT = "/api/video_analysis/ids"

# Deliberately more than one chunk, so the focused item sits past the 1000 rows
# the client downloads — the case a client-side indexOf would miss.
_N_ROWS = 2500






def _frame():
    """Chronological rows; item ids are zero-padded so order is unambiguous."""
    return pd.DataFrame({
        "item_id": [f"v{i:05d}" for i in range(_N_ROWS)],
        "utc_timestamp": pd.date_range("2026-01-01", periods=_N_ROWS, freq="h"),
        "niche_name": ["Cat Mischief" if i % 2 else "Guitar Covers" for i in range(_N_ROWS)],
    })






@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_VIEWER, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_VIEWER:
            return User(username=_TEST_VIEWER, role=ROLE_VIEWER, password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client






@pytest.fixture
def viewer(client, monkeypatch):
    """Logged-in viewer with the tab permission and one accessible study."""
    from web_interface import auth
    from web_interface.routes import api_viewer_routes as routes

    monkeypatch.setattr(
        auth.role_manager, "get_role_permissions", lambda role: ["tab.video_analysis"],
    )
    monkeypatch.setattr(
        "web_interface.routes._access.get_accessible_studies", lambda *a, **k: [_STUDY],
    )
    # Real column classification, so `niche_name` is filterable exactly as it is
    # in production (the category branch of filter_dataframe is dtype-gated).
    from web_interface import explorer_backend

    def _data(study, context=None):
        df = _frame()
        return df, explorer_backend.classify_columns(df)

    monkeypatch.setattr(routes, "get_explorer_data", _data)
    monkeypatch.setattr(routes, "enrich_with_user_tags", lambda df, ct, user: (df, ct))
    monkeypatch.setattr(routes, "load_display_id_map", lambda: {})

    with client.session_transaction() as sess:
        sess["_user_id"] = _TEST_VIEWER
        sess["_fresh"] = True
    return client






def _post(client, **extra):
    body = {"study": _STUDY, "filters": {}, "offset": 0, "limit": 1000}
    body.update(extra)
    res = client.post(_ENDPOINT, json=body)
    assert res.status_code == 200, res.data
    return res.get_json()






def test_focus_index_beyond_the_first_chunk(viewer):
    """An item past row 1000 still resolves — that's the whole point."""
    data = _post(viewer, focus_item_id="v01777")

    assert data["focus_index"] == 1777
    assert data["count"] == _N_ROWS
    # Not in the returned chunk; the client pages to it via loadViewerItem.
    assert "v01777" not in data["ids"]






def test_focus_index_respects_active_filters(viewer):
    """The index is a position in the filtered order, not in the raw frame."""
    filters = {"niche_name": {"type": "category", "value": ["Cat Mischief"]}}
    data = _post(viewer, filters=filters, focus_item_id="v00101")

    # Odd ids carry "Cat Mischief"; v00101 is the 51st of them (0-based 50).
    assert data["focus_index"] == 50
    assert data["count"] == _N_ROWS // 2






def test_focus_index_is_none_when_the_filters_exclude_the_item(viewer):
    """Present in the study but filtered out — the caller must not land on row 0."""
    filters = {"niche_name": {"type": "category", "value": ["Cat Mischief"]}}
    data = _post(viewer, filters=filters, focus_item_id="v00100")   # even id => other niche

    assert data["focus_index"] is None
    assert data["count"] > 0






def test_focus_index_is_none_for_an_item_outside_the_study(viewer):
    """A Semantic Space dot for a video this study does not contain."""
    data = _post(viewer, focus_item_id="not-in-this-study")

    assert data["focus_index"] is None






def test_key_is_absent_when_not_requested(viewer):
    """No behaviour change for every existing caller."""
    assert "focus_index" not in _post(viewer)
