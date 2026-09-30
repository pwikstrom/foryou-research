"""Comment text is gated on the ``data.sensitive_activity`` role permission.

A role without the key still sees that a comment was made (the bare
``comment`` token in ``extra_data``) but never what was said: not in the
Video Analysis item record and not through global search. Summaries of
``extra_data`` (stats, timelines, the My Collections emoji) never carry the
raw cells to anyone who isn't the donor or a key holder.

Study data is served through a patched ``study_data._cached_study_frame``, so
the real accessors (and their redaction) run without any parquet.
"""

import pandas as pd
import pytest

from fyp.core.activity_vocabulary import (
    parse_extra_data_tokens,
    redact_comment_text,
    strip_comment_text,
)
from tests._web import web_client

_USER = "__comment_text_test_user__"
_ADMIN = "__comment_text_test_admin__"
_STUDY = "comment_text_study"
_SECRET = "I miss my brother"


# --- The redaction itself ------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        (f"fave,comment:{_SECRET}", "fave,comment"),
        ("comment:x,share:copy_link", "comment,share:copy_link"),
        ("comment:a,comment:b", "comment,comment"),
        ("Comment:shouting", "Comment"),
        ("comment", "comment"),
        ("fave,save", "fave,save"),
        ("share:chat_head ×3", "share:chat_head ×3"),
        ("", ""),
        (None, None),
    ],
)
def test_strip_comment_text(cell, expected):
    assert strip_comment_text(cell) == expected


def test_redaction_keeps_every_engagement_token():
    cells = [f"fave,comment:{_SECRET}", "comment:x,share:copy_link", "save", "comment"]
    for cell in cells:
        assert parse_extra_data_tokens(strip_comment_text(cell)) == parse_extra_data_tokens(cell)


def test_vectorized_matches_per_cell_and_keeps_dtype():
    cells = [f"fave,comment:{_SECRET}", None, "save", "comment:a,share:link", "Europe/Paris"]
    series = pd.Series(cells, dtype="string[pyarrow]")
    out = redact_comment_text(series)
    assert out.dtype == series.dtype
    assert out.tolist() == [
        "fave,comment",
        pd.NA,
        "save",
        "comment,share:link",
        "Europe/Paris",
    ]
    assert series.iloc[0] == f"fave,comment:{_SECRET}"  # input untouched


# --- The study-data accessors ---------------------------------------------------


def _frame():
    return pd.DataFrame(
        {
            "item_id": ["v1", "v2", "v3"],
            "collection_id": ["c1", "c1", "c1"],
            "extra_data": pd.Series(
                [f"fave,comment:{_SECRET}", "fave", None], dtype="string[pyarrow]"
            ),
            "niche_name": ["Cats", "Dogs", "Cats"],
        }
    )


_COL_TYPES = {
    "item_id": "identifier",
    "collection_id": "category",
    "extra_data": "category",
    "niche_name": "category",
}


@pytest.fixture
def cached_study(monkeypatch):
    """Serve ``_frame()`` as the study's cached frame; yields the cached frame."""
    from web_interface.services import study_data

    frame = _frame()
    monkeypatch.setattr(
        study_data,
        "_cached_study_frame",
        lambda study, verbose=False: (frame, dict(_COL_TYPES), {"ok": True}),
    )
    return frame


def test_explorer_data_hides_comment_text_by_default(cached_study):
    from web_interface.services.study_data import get_explorer_data

    df, _ = get_explorer_data(_STUDY)
    assert df["extra_data"].iloc[0] == "fave,comment"

    df, _ = get_explorer_data(_STUDY, hide_comment_text=False)
    assert df["extra_data"].iloc[0] == f"fave,comment:{_SECRET}"
    # The shared cached frame is never rewritten.
    assert cached_study["extra_data"].iloc[0] == f"fave,comment:{_SECRET}"


def test_explorer_rows_hide_comment_text_by_default(cached_study):
    from web_interface.services.study_data import get_explorer_rows

    rows, _ = get_explorer_rows(_STUDY, item_id="v1")
    assert rows["extra_data"].tolist() == ["fave,comment"]

    rows, _ = get_explorer_rows(_STUDY, item_id="v1", hide_comment_text=False)
    assert rows["extra_data"].tolist() == [f"fave,comment:{_SECRET}"]
    assert cached_study["extra_data"].iloc[0] == f"fave,comment:{_SECRET}"


def test_projection_without_extra_data_is_untouched(cached_study):
    from web_interface.services.study_data import get_explorer_data

    df, _ = get_explorer_data(_STUDY, columns=["item_id"])
    assert "extra_data" not in df.columns


# --- The routes -----------------------------------------------------------------


def _client_for(monkeypatch, perms):
    """A logged-in client whose role holds ``perms`` (plus admin as a second user)."""
    from web_interface.auth import accounts
    from web_interface.routes import api_viewer_routes as viewer_routes

    monkeypatch.setattr(accounts.role_manager, "get_role_permissions", lambda role: list(perms))
    monkeypatch.setattr(
        "web_interface.routes._access.get_accessible_studies", lambda *a, **k: [_STUDY]
    )
    monkeypatch.setattr(viewer_routes, "enrich_with_user_tags", lambda df, ct, user, **kw: (df, ct))
    monkeypatch.setattr(viewer_routes, "load_display_id_map", dict)
    monkeypatch.setattr(viewer_routes, "load_shared_tags", lambda users: ({}, {}))


@pytest.fixture
def client(monkeypatch, cached_study):
    from web_interface.auth.accounts import ROLE_ADMIN, ROLE_VIEWER

    with web_client(monkeypatch, {_USER: ROLE_VIEWER, _ADMIN: ROLE_ADMIN}) as test_client:
        yield test_client


def _login(client, username):
    with client.session_transaction() as sess:
        sess["_user_id"] = username
        sess["_fresh"] = True


def _item(client):
    res = client.get(f"/api/video_analysis/item/{_STUDY}/v1")
    assert res.status_code == 200, res.data
    return res.get_json()


def _search_count(client, word):
    body = {"study": _STUDY, "filters": {}, "offset": 0, "limit": 1000, "search_query": word}
    res = client.post("/api/video_analysis/ids", json=body)
    assert res.status_code == 200, res.data
    return res.get_json()["count"]


def test_role_without_key_sees_that_a_comment_was_made_but_not_the_text(client, monkeypatch):
    _client_for(monkeypatch, ["tab.video_analysis"])
    _login(client, _USER)

    record = _item(client)
    assert record["extra_data"] == "fave,comment"
    assert _SECRET not in str(record)
    # The text is not searchable either; engagement tokens still are.
    assert _search_count(client, "brother") == 0
    assert _search_count(client, "comment") == 1


def test_role_with_key_reads_and_searches_comment_text(client, monkeypatch):
    _client_for(monkeypatch, ["tab.video_analysis", "data.sensitive_activity"])
    _login(client, _USER)

    assert _item(client)["extra_data"] == f"fave,comment:{_SECRET}"
    assert _search_count(client, "brother") == 1


def test_admin_reads_comment_text(client, monkeypatch):
    _client_for(monkeypatch, [])
    _login(client, _ADMIN)

    assert _item(client)["extra_data"] == f"fave,comment:{_SECRET}"


# --- Summaries never carry raw cells ------------------------------------------


def test_stats_count_tokens_not_raw_cells():
    from web_interface.services import explorer_backend

    res = explorer_backend.get_current_stats(_frame(), {"extra_data": "category"})
    assert res["stats"]["extra_data"] == {"fave": 2, "comment": 1}


def test_stale_metadata_stats_are_scrubbed():
    from web_interface.services.explorer_backend import scrub_extra_data_stats

    stale = {
        "extra_data": {"values": [{"value": "fave", "count": 9}, {"value": "comment", "count": 2}]},
        "total_stats": {"extra_data": {"fave": 7, f"fave,comment:{_SECRET}": 1}, "x": {"a": 1}},
    }
    scrub_extra_data_stats(stale)
    assert stale["total_stats"] == {"extra_data": {"fave": 9, "comment": 2}, "x": {"a": 1}}

    no_filter_meta = {"total_stats": {"extra_data": {f"comment:{_SECRET}": 1}}}
    scrub_extra_data_stats(no_filter_meta)
    assert "extra_data" not in no_filter_meta["total_stats"]

    clean = {"total_stats": {"extra_data": {"fave": 3}}}
    assert scrub_extra_data_stats(clean) == {"total_stats": {"extra_data": {"fave": 3}}}


def test_new_key_is_in_the_catalog_but_granted_to_no_role_by_default():
    from web_interface.auth import permissions

    assert permissions.SENSITIVE_ACTIVITY_KEY in permissions.ALL_PERMISSION_KEYS
    assert permissions.SENSITIVE_ACTIVITY_KEY not in permissions.DEFAULT_NON_ADMIN_PERMISSIONS
    assert permissions.SENSITIVE_ACTIVITY_KEY not in permissions.STUDENT_PERMISSIONS
    assert permissions.SENSITIVE_ACTIVITY_KEY not in permissions.PERMISSION_KEYS_GRANT_ALL
    for implied in permissions.PERMISSION_KEY_IMPLIED_GRANTS.values():
        assert permissions.SENSITIVE_ACTIVITY_KEY not in implied


@pytest.mark.parametrize(
    ("perms", "owned", "emoji_shown"),
    [
        (["tab.data_management.edit_collections"], [], False),
        (["tab.data_management.edit_collections", "data.sensitive_activity"], [], True),
        (["tab.my_stuff.my_collections"], ["c1"], True),  # the donor's own
    ],
)
def test_personality_emoji_needs_ownership_or_key(monkeypatch, perms, owned, emoji_shown):
    from web_interface.auth import accounts
    from web_interface.auth.accounts import ROLE_VIEWER
    from web_interface.services import collection_accounts, my_collections_service

    monkeypatch.setattr(accounts.role_manager, "get_role_permissions", lambda role: list(perms))
    monkeypatch.setattr(collection_accounts, "collections_for_user", lambda u, **kw: owned)
    monkeypatch.setattr(
        my_collections_service,
        "build_personality",
        lambda cids: {"emoji": {"char": "😭", "count": 3}, "searches": []},
    )
    with web_client(monkeypatch, {_USER: ROLE_VIEWER}, login_as=_USER) as client:
        res = client.get("/api/my/collections/c1/personality")
    assert res.status_code == 200, res.data
    assert (res.get_json()["emoji"] is not None) is emoji_shown
