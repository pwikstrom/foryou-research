"""Tests for the per-study methods/provenance note (S3 item 3).

Builder tests use synthetic frames + a stubbed registry so no data files are
touched; endpoint tests reuse the stubbed-user approach from
``test_endpoint_gates.py``.
"""

import pandas as pd
import pytest

from web_interface.services import methods_note

_TEST_VIEWER = "__methods_test_viewer__"


_FAKE_REGISTRY = {
    "versions": {
        "av_new": {
            "label": "gemini-3-flash:abc123",
            "model": "gemini-3-flash-preview",
            "prompt_hash": "abc123",
            "schema_hash": "def456",
            "created_at": "2026-06-01T00:00:00+00:00",
        },
        "av_old": {
            "label": "gemini-2.5:xyz",
            "model": "gemini-2.5-flash",
            "prompt_hash": "xyz",
            "schema_hash": "uvw",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    },
    "preferred": "av_new",
}


def _synthetic_study_df() -> pd.DataFrame:
    df = pd.DataFrame({
        "item_id": pd.array(["1", "2", "3", "4"], dtype="string[pyarrow]"),
        "collection_id": pd.array(["c1", "c1", "c2", "c2"], dtype="string[pyarrow]"),
        "local_timestamp": pd.to_datetime(
            ["2024-03-01 10:00", "2024-04-15 11:00", "2024-05-01 12:00", "2024-06-30 13:00"]),
        "annotation_version": pd.array(["av_new", "av_new", "av_old", None], dtype="string[pyarrow]"),
        "scrape_contract_version": pd.array(["sv_a", "sv_a", "sv_a", None], dtype="string[pyarrow]"),
        "activity_contract_version": pd.array(["acv_a"] * 4, dtype="string[pyarrow]"),
    })
    return df


def _study_config(**overrides) -> dict:
    cfg = {
        "STUDY_NAME": "teststudy",
        "SELECTED_COLLECTIONS": ["c1", "c2"],
        "START_DATE": "2024-03-01",
        "END_DATE": "2024-06-30",
        "SAMPLE_FRAME": "off",
        "last_updated": "2026-07-30T00:00:00+00:00",
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture
def stub_io(monkeypatch):
    monkeypatch.setattr(methods_note.annotation_versioning, "load_registry",
                        lambda: dict(_FAKE_REGISTRY))
    monkeypatch.setattr(methods_note.data_io, "getmtime",
                        lambda **kw: 1000.0)
    monkeypatch.setattr(methods_note.data_io, "exists", lambda **kw: False)






def test_version_distribution_buckets():
    df = _synthetic_study_df()
    dist = methods_note._version_distribution(df, "annotation_version")
    assert dist == {"av_new": 2, "av_old": 1, "unversioned": 1}

    # Missing column tolerated
    assert methods_note._version_distribution(df, "nope") == {}
    assert methods_note._version_distribution(None, "annotation_version") == {}






def test_build_methods_note_full(stub_io):
    df = _synthetic_study_df()
    stats = {
        "total_activities": 4, "unique_videos": 4, "unique_collections": 2,
        "active_days": 4, "scraped_videos": 3, "annotated_videos": 3,
        "activities_scraped": 3, "activities_annotated": 3,
    }
    note = methods_note.build_methods_note(
        study_name="teststudy",
        study_config=_study_config(),
        df_study=df,
        stats=stats,
        refresh_action="full_rebuild",
        refresh_trigger="study_save",
    )

    assert note["schema_version"] == methods_note.SCHEMA_VERSION
    assert note["study"]["name"] == "teststudy"

    sel = note["selection"]
    assert sel["collections_selected"] == 2
    assert sel["sampling_active"] is False
    assert "thresholds" not in sel
    win = sel["date_window"]
    assert win["configured_start"] == "2024-03-01"
    assert win["actual_min"].startswith("2024-03-01")
    assert win["actual_max"].startswith("2024-06-30")

    assert note["counts"]["activities"] == 4
    assert note["counts"]["collections"] == 2

    ann = note["annotation"]
    assert ann["preferred_version"] == "av_new"
    assert ann["version_in_use"] == "av_new"
    assert ann["version_in_use_descriptor"]["model"] == "gemini-3-flash-preview"
    assert ann["versions_in_rows"]["av_old"] == 1
    assert ann["mixed_versions"] is True
    assert "mixed_versions_note" in ann

    assert note["contracts"]["scrape_versions_in_rows"]["sv_a"] == 3
    assert note["contracts"]["activity_versions_in_rows"] == {"acv_a": 4}
    assert note["semantic_map"] is None  # no niche column
    assert note["freshness"]["refresh_action"] == "full_rebuild"
    assert note["freshness"]["refresh_trigger"] == "study_save"
    assert note["freshness"]["row_level_fields_from"] == "dataframe"






def test_build_methods_note_pinned_version(stub_io):
    note = methods_note.build_methods_note(
        study_name="teststudy",
        study_config=_study_config(annotation_version="av_old"),
        df_study=_synthetic_study_df(),
    )
    ann = note["annotation"]
    assert ann["pinned_version"] == "av_old"
    assert ann["preferred_version"] == "av_new"
    assert ann["version_in_use"] == "av_old"
    assert ann["version_in_use_descriptor"]["model"] == "gemini-2.5-flash"
    assert "pinned" in ann["version_in_use_note"]






def test_build_methods_note_sampling_block(stub_io):
    df = _synthetic_study_df()
    df.attrs["sampling_report"] = {
        "n_excluded_collections": 2, "n_downsampled_collections": 1,
        "min_cells_per_collection": 20, "max_cells_per_collection": 200,
    }
    note = methods_note.build_methods_note(
        study_name="teststudy",
        study_config=_study_config(SAMPLE_FRAME="annotated",
                                   MIN_ACTIVITY_COUNT_PER_GROUP=30),
        df_study=df,
    )
    sel = note["selection"]
    assert sel["sampling_active"] is True
    assert sel["sample_frame_label"] == "Sampled from videos with AI labels"
    assert sel["random_seed"] == 42
    assert sel["thresholds"]["min_activity_per_group"] == 30
    assert sel["sampling_report"]["collections_excluded_by_thresholds"] == 2






def test_build_methods_note_without_dataframe(stub_io):
    """Degraded mode: registry/config facts only, honestly flagged."""
    note = methods_note.build_methods_note(
        study_name="teststudy",
        study_config=_study_config(),
        df_study=None,
    )
    assert note["annotation"]["versions_in_rows"] == {}
    assert note["freshness"]["row_level_fields_from"] == "unavailable"
    assert note["selection"]["date_window"]["actual_min"] is None






def test_note_staleness(monkeypatch):
    monkeypatch.setattr(methods_note.data_io, "getmtime", lambda **kw: 2000.0)
    fresh_note = {"freshness": {"source_parquet_mtime": 2000.0}}
    assert methods_note.note_staleness("s", fresh_note)["stale"] is False

    stale_note = {"freshness": {"source_parquet_mtime": 500.0}}
    assert methods_note.note_staleness("s", stale_note)["stale"] is True

    # Missing mtimes are never "stale"
    assert methods_note.note_staleness("s", {"freshness": {}})["stale"] is False






# ============================================================================
# Endpoint gating + payload
# ============================================================================


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






def _login(client, username):
    with client.session_transaction() as sess:
        sess["_user_id"] = username
        sess["_fresh"] = True






def _grant_permissions(monkeypatch, perms):
    from web_interface import auth

    monkeypatch.setattr(auth.role_manager, "get_role_permissions", lambda role: list(perms))






def test_methods_endpoint_requires_auth(client):
    res = client.get("/api/studies/whatever/methods")
    assert res.status_code in (302, 401)






def test_methods_endpoint_requires_tab_permission(client, monkeypatch):
    _grant_permissions(monkeypatch, [])
    _login(client, _TEST_VIEWER)
    res = client.get("/api/studies/whatever/methods")
    assert res.status_code == 403






def test_methods_endpoint_requires_study_access(client, monkeypatch):
    from web_interface.routes import _access

    _grant_permissions(monkeypatch, ["tab.explore"])
    monkeypatch.setattr(_access, "get_accessible_studies", lambda *a, **k: ["other"])
    _login(client, _TEST_VIEWER)
    res = client.get("/api/studies/secret/methods")
    assert res.status_code == 403






def test_methods_endpoint_missing_note_404_with_hint(client, monkeypatch):
    from web_interface.routes import _access

    _grant_permissions(monkeypatch, ["tab.explore"])
    monkeypatch.setattr(_access, "get_accessible_studies", lambda *a, **k: ["mystudy"])
    monkeypatch.setattr(methods_note, "read_methods_note", lambda study: None)
    _login(client, _TEST_VIEWER)

    res = client.get("/api/studies/mystudy/methods")
    assert res.status_code == 404
    assert "hint" in res.get_json()






def test_methods_endpoint_returns_note_with_staleness(client, monkeypatch):
    from web_interface.routes import _access

    _grant_permissions(monkeypatch, ["tab.explore"])
    monkeypatch.setattr(_access, "get_accessible_studies", lambda *a, **k: ["mystudy"])
    monkeypatch.setattr(methods_note, "read_methods_note",
                        lambda study: {"schema_version": 1, "study": {"name": study},
                                       "freshness": {"source_parquet_mtime": 100.0}})
    monkeypatch.setattr(methods_note.data_io, "getmtime", lambda **kw: 100.0)
    _login(client, _TEST_VIEWER)

    res = client.get("/api/studies/mystudy/methods")
    assert res.status_code == 200
    payload = res.get_json()
    assert payload["study"]["name"] == "mystudy"
    assert payload["staleness"]["stale"] is False
