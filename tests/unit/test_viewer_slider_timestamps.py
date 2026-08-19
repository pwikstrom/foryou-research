"""The timestamp payload /api/video_analysis/ids feeds the slider scrub chip.

The chip names the activity time of the video under the slider thumb while the
researcher drags. Two sources, because a study can hold tens of thousands of
videos and the client only ever downloads a 1000-row chunk:

  ``timestamps``  — one per returned row, exact, aligned with ``ids``.
  ``time_marks``  — a coarse index -> timestamp ladder over the WHOLE filtered
                    set, first chunk only, for positions outside the chunk.

Both are on the same clock as the header's ``time_span`` (participant local time
when the study has it), so the chip never disagrees with the date range above it
or the detail panel below it.

Fixtures mirror ``test_viewer_focus_item.py``: Flask test client, stubbed user,
monkeypatched study data — no parquets.
"""

import pandas as pd
import pytest

_TEST_VIEWER = "__viewer_ts_test_user__"
_STUDY = "slider_ts_study"
_ENDPOINT = "/api/video_analysis/ids"

# More than one chunk, so the ladder has to cover rows the chunk does not.
_N_ROWS = 2500

# Mirrors the route's sampling cap; the ladder must not exceed it.
_MARK_CAP = 500




def _frame():
    """Chronological rows whose local clock is deliberately offset from UTC.

    The two-hour skew is what proves the payload is built from the participant's
    ``local_timestamp`` rather than the ``utc_timestamp`` the route sorts on.
    """
    utc = pd.date_range("2026-01-01 00:00", periods=_N_ROWS, freq="h")
    return pd.DataFrame({
        "item_id": [f"v{i:05d}" for i in range(_N_ROWS)],
        "utc_timestamp": utc,
        "local_timestamp": utc + pd.Timedelta(hours=2),
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
    from web_interface import auth, explorer_backend
    from web_interface.routes import api_viewer_routes as routes

    monkeypatch.setattr(
        auth.role_manager, "get_role_permissions", lambda role: ["tab.video_analysis"],
    )
    monkeypatch.setattr(
        "web_interface.routes._access.get_accessible_studies", lambda *a, **k: [_STUDY],
    )

    def _data(study, context=None, columns=None):
        df = _frame()
        col_types = explorer_backend.classify_columns(df)
        if columns is not None:
            keep = [c for c in columns if c in df.columns]
            df = df[keep]
            col_types = {k: v for k, v in col_types.items() if k in keep}
        return df, col_types

    monkeypatch.setattr(routes, "get_explorer_data", _data)
    monkeypatch.setattr(routes, "enrich_with_user_tags", lambda df, ct, user, **kw: (df, ct))
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




def test_chunk_timestamps_align_with_the_returned_ids(viewer):
    """One timestamp per returned row, in the same order, on the local clock."""
    data = _post(viewer)

    assert len(data["timestamps"]) == len(data["ids"]) == 1000
    # Row 0 is 2026-01-01 00:00 UTC, i.e. 02:00 on the participant's clock.
    assert data["timestamps"][0] == "2026-01-01T02:00:00"
    assert data["timestamps"][1] == "2026-01-01T03:00:00"
    # Same basis as the header span, which is the point of using span_col.
    assert data["time_span"]["first"] == data["timestamps"][0]




def test_chunk_timestamps_follow_pagination(viewer):
    """A paged request describes its own rows, not the first chunk's."""
    data = _post(viewer, offset=1000, limit=1000)

    assert data["timestamps"][0] == "2026-02-11T18:00:00"   # row 1000
    assert len(data["timestamps"]) == 1000




def test_time_marks_ladder_spans_the_whole_filtered_set(viewer):
    """Bounded sample count, ends pinned to the real first and last rows."""
    marks = _post(viewer)["time_marks"]

    assert len(marks["idx"]) == len(marks["ts"]) <= _MARK_CAP
    assert marks["idx"] == sorted(marks["idx"])
    assert marks["idx"][0] == 0
    assert marks["idx"][-1] == _N_ROWS - 1
    assert marks["ts"][0] == "2026-01-01T02:00:00"
    assert marks["ts"][-1] == "2026-04-15T05:00:00"




def test_time_marks_are_exact_for_a_small_result_set(viewer, monkeypatch):
    """At or under the cap every index is sampled, so the chip never approximates."""
    from web_interface import explorer_backend
    from web_interface.routes import api_viewer_routes as routes

    n_small = 120

    def _small(study, context=None, columns=None):
        df = _frame().head(n_small)
        return df, explorer_backend.classify_columns(df)

    monkeypatch.setattr(routes, "get_explorer_data", _small)
    marks = _post(viewer)["time_marks"]

    assert marks["idx"] == list(range(n_small))




def test_time_marks_ride_only_on_the_first_chunk(viewer):
    """The client keeps the ladder; resending it per page would be waste."""
    assert "time_marks" in _post(viewer, offset=0)
    assert "time_marks" not in _post(viewer, offset=1000)




def test_payload_survives_a_study_with_no_timestamp_column(viewer, monkeypatch):
    """No clock to report — the keys degrade rather than the request failing."""
    from web_interface import explorer_backend
    from web_interface.routes import api_viewer_routes as routes

    def _no_ts(study, context=None, columns=None):
        df = _frame()[["item_id", "niche_name"]]
        return df, explorer_backend.classify_columns(df)

    monkeypatch.setattr(routes, "get_explorer_data", _no_ts)
    data = _post(viewer)

    assert data["timestamps"] == []
    assert "time_marks" not in data
    assert data["count"] == _N_ROWS
