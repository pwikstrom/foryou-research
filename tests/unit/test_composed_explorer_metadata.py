"""/api/explore/metadata/base against a composed ("Everyone & Me") study.

A composed study stores NO artifacts of its own — its explorer metadata is
base ∪ overlay, assembled per request and cached on the two sources' mtimes.
The endpoint used to ignore that: with no ``{study}_explorer_metadata.json``
on disk it cold-built one from the composed frame and SAVED it under the
composed name, where nothing could ever invalidate it — the staleness check
compares the JSON against ``{study}_recoded.parquet``, which a composed study
does not have, so the file was served forever while base and overlay moved on
beneath it. Meanwhile the read-side merge, which every other caller uses, was
never consulted, so the two answers diverged silently.

Uses the Flask test client with a stubbed admin (same approach as
``test_cold_open_fast_path.py``).
"""

import pytest

from web_interface.routes import api_explorer_routes as routes

_TEST_ADMIN = "__composedmeta_test_admin__"
_STUDY = "__me_plus__p-9@example.org"
_CANONICAL = f"{_STUDY}_explorer_metadata.json"


@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_ADMIN, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_ADMIN:
            return User(username=uid, role=ROLE_ADMIN, password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)
    monkeypatch.setattr(routes, "study_access_error", lambda study: None)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["_user_id"] = _TEST_ADMIN
            sess["_fresh"] = True
        yield test_client


@pytest.fixture
def storage(monkeypatch):
    """Record every cache read/write the endpoint attempts, and neutralise the
    finalization steps that would otherwise reach for real project files."""
    seen = {"exists": [], "saved": []}

    def _exists(storage_location=None, filename=None, **kw):
        seen["exists"].append(filename)
        return False

    def _save_json(data=None, storage_location=None, filename=None, **kw):
        seen["saved"].append(filename)

    monkeypatch.setattr(routes.data_io, "exists", _exists)
    monkeypatch.setattr(routes.data_io, "save_json", _save_json)
    monkeypatch.setattr(routes, "_get_recoded_mtime", lambda s: None)
    monkeypatch.setattr(routes, "load_schema_metadata", lambda m: m)
    monkeypatch.setattr(routes, "load_display_id_map", lambda: {})
    monkeypatch.setattr(routes, "get_collection_tags", lambda: {})
    monkeypatch.setattr(routes, "get_study_collections",
                        lambda s: [{"collection_id": c} for c in ("c1", "c2", "c9")])
    monkeypatch.setattr(routes, "resolve_compose",
                        lambda s: ("main_study", "__me__p-9@example.org"))
    return seen


def _merged_payload():
    return {
        "collection_id": {
            "type": "category",
            "values": [{"value": "c9", "count": 90},
                       {"value": "c2", "count": 40},   # the owner's own
                       {"value": "c1", "count": 10}],
            "total_unique": 3,
        },
        "collection_ids": ["c1", "c9", "c2"],
        "filter_priority": ["collection_id"],
        "total_stats": {"duration": {"type": "density", "x": [1.0], "y": [1.0]}},
        routes.TOTAL_STATS_PROVISIONAL_KEY: True,
    }


def test_composed_metadata_comes_from_the_merge_and_is_never_written(client, storage,
                                                                    monkeypatch):
    monkeypatch.setattr(routes, "get_explorer_metadata_cached",
                        lambda s: _merged_payload())

    res = client.get(f"/api/explore/metadata/base?study={_STUDY}")
    payload = res.get_json()

    assert res.status_code == 200
    # The owner's own collection is selectable, which is what the study is for.
    assert [v["value"] for v in payload["collection_id"]["values"]] == ["c9", "c2", "c1"]
    # Nothing was read from or written to an artifact named for the composed study.
    assert _CANONICAL not in storage["exists"]
    assert storage["saved"] == []
    # Internal bookkeeping for the filter endpoint is not shipped as a column.
    assert routes.TOTAL_STATS_PROVISIONAL_KEY not in payload


def test_composed_metadata_never_mutates_the_shared_cache_entry(client, storage,
                                                                monkeypatch):
    """Finalization writes into the payload (display-id labels, the
    study-membership filter). The merge is a shared cache entry, so the
    endpoint must work on its own copy."""
    cached = _merged_payload()
    monkeypatch.setattr(routes, "get_explorer_metadata_cached", lambda s: cached)
    monkeypatch.setattr(routes, "get_study_collections",
                        lambda s: [{"collection_id": "c1"}])  # drops c2 and c9

    res = client.get(f"/api/explore/metadata/base?study={_STUDY}")

    assert [v["value"] for v in res.get_json()["collection_id"]["values"]] == ["c1"]
    assert len(cached["collection_id"]["values"]) == 3
    assert routes.TOTAL_STATS_PROVISIONAL_KEY in cached


def test_composed_cold_build_still_writes_nothing(client, storage, monkeypatch):
    """With a source's metadata missing there is nothing to merge, so the
    endpoint falls back to the frame — and still saves no artifact."""
    monkeypatch.setattr(routes, "get_explorer_metadata_cached", lambda s: {})
    monkeypatch.setattr(routes, "get_explorer_data",
                        lambda study, **kw: ("frame", {"collection_id": "category"}))
    monkeypatch.setattr(routes, "_build_full_metadata",
                        lambda df, col_types, study: _merged_payload())

    res = client.get(f"/api/explore/metadata/base?study={_STUDY}")

    assert res.status_code == 200
    assert storage["saved"] == []


def test_a_regular_study_still_uses_its_file(client, storage, monkeypatch):
    """The guard is composed-only: a normal study still cold-builds and saves."""
    monkeypatch.setattr(routes, "resolve_compose", lambda s: None)
    monkeypatch.setattr(routes, "get_explorer_data",
                        lambda study, **kw: ("frame", {"collection_id": "category"}))
    monkeypatch.setattr(routes, "_build_full_metadata",
                        lambda df, col_types, study: _merged_payload())

    res = client.get("/api/explore/metadata/base?study=main_study")

    assert res.status_code == 200
    assert storage["saved"] == ["main_study_explorer_metadata.json"]
