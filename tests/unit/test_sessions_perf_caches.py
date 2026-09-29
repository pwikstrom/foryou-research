"""The Sessions tab's request-path caches.

The overview/detail endpoints hold several module-level caches so a filter
keystroke costs mask arithmetic, not storage reads. These tests pin the
invalidation rules: filter-range bounds cache per (fingerprint, study, floors)
and ignore the user's own filters; the index loader splits ``search_text``
out of the working frame without losing search; the detail artifact frames
reload on a fingerprint change; admin settings re-read after a save.
"""

import pandas as pd
import pytest

import web_interface.routes.api_sessions_routes as mod
from web_interface.services import sessions_data


@pytest.fixture
def cached_index(monkeypatch):
    """Install a frame as THE cached index (fingerprint set) and return it."""
    df = pd.DataFrame(
        {
            "collection_id": pd.array(["c1", "c2"], dtype="string"),
            "session_id": pd.array(["s1", "s2"], dtype="string"),
            "start_ts": ["2026-01-01T10:00:00", "2026-01-02T10:00:00"],
            "duration_min": [10.0, 20.0],
            "n_plays": [5, 8],
            "coverage_embedded": [0.5, 0.9],
            "min_window_cosdist": [0.3, 0.6],
            "n_episodes": [1, 2],
        }
    )
    df["_start_dt"] = pd.to_datetime(df["start_ts"])
    sessions_data._INDEX_CACHE.update({"df": df, "search": None, "fingerprint": "fp1"})
    return df


def test_filter_ranges_cache_hits_and_invalidation(cached_index, monkeypatch):
    calls = {"n": 0}
    real = sessions_data._filter_ranges

    def counting(df):
        calls["n"] += 1
        return real(df)

    monkeypatch.setattr(sessions_data, "_filter_ranges", counting)
    pop = pd.Series([True, True]).to_numpy()
    sig = (4, 0.0, 0.0, 0, 2, 123, ("a", "b"))

    r1 = sessions_data.cached_filter_ranges(cached_index, pop, "studyA", sig)
    r2 = sessions_data.cached_filter_ranges(cached_index, pop, "studyA", sig)
    assert calls["n"] == 1 and r1 is r2

    # A different floors/scope signature is a different population.
    sessions_data.cached_filter_ranges(cached_index, pop, "studyA", (5,) + sig[1:])
    assert calls["n"] == 2
    # A different study likewise.
    sessions_data.cached_filter_ranges(cached_index, pop, "studyB", sig)
    assert calls["n"] == 3

    # An index reload (new fingerprint) clears the cache.
    sessions_data._INDEX_CACHE["fingerprint"] = "fp2"
    sessions_data._RANGES_CACHE.clear()  # what load_index does on reload
    sessions_data.cached_filter_ranges(cached_index, pop, "studyA", sig)
    assert calls["n"] == 4


def test_filter_ranges_uncached_for_an_injected_frame(cached_index):
    """A frame that is not the cached index computes directly (test stubs)."""
    other = cached_index.copy()
    pop = pd.Series([True, True]).to_numpy()
    before = dict(sessions_data._RANGES_CACHE)
    out = sessions_data.cached_filter_ranges(other, pop, "studyA", (1,))
    assert out["duration_min"] == [10.0, 20.0]
    assert sessions_data._RANGES_CACHE == before


def test_search_blob_serves_cached_or_inline_column(cached_index):
    # The cached index holds the blob out-of-frame.
    blob = pd.Series(["alpha", "beta"], dtype="string")
    sessions_data._INDEX_CACHE["search"] = blob
    assert sessions_data.search_blob(cached_index) is blob
    # An injected frame carrying the column is served from it directly.
    inline = cached_index.copy()
    inline["search_text"] = ["x", "y"]
    assert list(sessions_data.search_blob(inline)) == ["x", "y"]
    # An injected frame without the column: search unavailable.
    assert sessions_data.search_blob(cached_index.copy()) is None


def test_load_index_splits_search_text_and_parses_start(monkeypatch):
    raw = pd.DataFrame(
        {
            "collection_id": ["c1"],
            "session_id": ["s1"],
            "start_ts": ["2026-01-01T10:00:00"],
            "search_text": ["recipes pasta"],
        }
    )
    monkeypatch.setattr(sessions_data, "artifact_fingerprint", lambda fn, location=None: "fpX")
    monkeypatch.setattr(sessions_data.data_io, "load_parquet_selective", lambda **kw: raw.copy())
    df = sessions_data.load_index()
    assert "search_text" not in df.columns
    assert "_start_dt" in df.columns
    assert df["_start_dt"].iloc[0] == pd.Timestamp("2026-01-01T10:00:00")
    assert list(sessions_data._INDEX_CACHE["search"]) == ["recipes pasta"]
    # The blob is row-aligned and reachable through search_blob.
    assert sessions_data.search_blob(df) is sessions_data._INDEX_CACHE["search"]


def test_artifact_frame_reloads_on_fingerprint_change(monkeypatch):
    frames = iter(
        [
            pd.DataFrame({"collection_id": ["c1"], "session_id": ["s1"], "v": [1]}),
            pd.DataFrame({"collection_id": ["c2"], "session_id": ["s2"], "v": [2]}),
        ]
    )
    loads = {"n": 0}

    def fake_load(**kw):
        loads["n"] += 1
        return next(frames)

    monkeypatch.setattr(sessions_data.data_io, "load_parquet_selective", fake_load)
    fps = {"fp": "A"}
    monkeypatch.setattr(sessions_data, "artifact_fingerprint", lambda fn, location=None: fps["fp"])

    cache = {"fingerprint": None, "df": None}
    import threading

    lock = threading.Lock()
    f1 = sessions_data._artifact_frame("whatever.parquet", cache, lock)
    f2 = sessions_data._artifact_frame("whatever.parquet", cache, lock)
    assert loads["n"] == 1 and f1 is f2
    fps["fp"] = "B"
    f3 = sessions_data._artifact_frame("whatever.parquet", cache, lock)
    assert loads["n"] == 2
    assert list(f3["collection_id"].astype(str)) == ["c2"]


def test_embedded_ids_prefers_injected_flags_then_sidecar_index():
    # Injected flags (tests / legacy) win.
    got = sessions_data.embedded_ids({"a", "b"}, {"embedded": {"b", "z"}})
    assert got == {"b"}

    # Production shape: empty embedded set -> sidecar id-index lookup.
    class FakeIndex:
        def lookup(self, ids):
            import numpy as np

            found = np.array([i == "a" for i in ids])
            return None, found

    sessions_data._FLAGS_CACHE["emb_index"] = FakeIndex()
    got = sessions_data.embedded_ids({"a", "b"}, {"embedded": set()})
    assert got == {"a"}

    # No dense store: nothing embedded.
    sessions_data._FLAGS_CACHE["emb_index"] = None
    assert sessions_data.embedded_ids({"a"}, {"embedded": set()}) == set()


def test_features_cache_invalidates_on_source_fingerprints(monkeypatch):
    """features() reloads only when video_map/scrapes actually change — never
    on a timer (the rebuild is a corpus-scale read)."""
    loads = {"n": 0}

    def fake_load(extra_map_cols=None, **kw):
        loads["n"] += 1
        return pd.DataFrame({"author": ["a"]}, index=pd.Index(["v1"], name="item_id"))

    monkeypatch.setattr(mod.session_explorer, "load_video_features", fake_load)
    monkeypatch.setattr(mod.session_explorer, "trend_numeric_columns", lambda: [])
    fps = {"video_map.parquet": "m1", mod.embeddings.SCRAPES_FILE: "s1"}
    monkeypatch.setattr(sessions_data, "artifact_fingerprint", lambda fn, location=None: fps[fn])

    f1 = sessions_data.features()
    f2 = sessions_data.features()
    assert loads["n"] == 1 and f1 is f2
    fps["video_map.parquet"] = "m2"
    sessions_data.features()
    assert loads["n"] == 2
    fps[mod.embeddings.SCRAPES_FILE] = "s2"
    sessions_data.features()
    assert loads["n"] == 3


def test_flag_sets_cache_invalidates_on_source_fingerprints(monkeypatch):
    builds = {"n": 0}

    def fake_sets(model, item_ids=None, include_embedded=True):
        builds["n"] += 1
        return {"scraped": set(), "downloaded": set(), "annotated": {"v1"}, "embedded": set()}

    class FakeBackend:
        def model_id(self):
            return "model-x"

    monkeypatch.setattr(mod.session_explorer, "enrichment_id_sets", fake_sets)
    monkeypatch.setattr(mod.embeddings, "active_embedding_backend", lambda: FakeBackend())
    monkeypatch.setattr(sessions_data.embedding_store, "load_index", lambda model: None)
    fps = {"fp": "A"}
    monkeypatch.setattr(sessions_data, "artifact_fingerprint", lambda fn, location=None: fps["fp"])

    s1 = sessions_data.flag_sets()
    s2 = sessions_data.flag_sets()
    assert builds["n"] == 1 and s1 is s2
    fps["fp"] = "B"
    sessions_data.flag_sets()
    assert builds["n"] == 2


def test_admin_settings_cache_invalidated_by_save(monkeypatch, tmp_path):
    from web_interface.services import admin_settings

    store = {"value": {"sessions_min_plays": 4}}
    monkeypatch.setattr(admin_settings.data_io, "exists", lambda **kw: True)
    monkeypatch.setattr(admin_settings.data_io, "load_json", lambda **kw: dict(store["value"]))
    saved = {}
    monkeypatch.setattr(admin_settings.data_io, "save_json", lambda **kw: saved.update(kw))

    assert admin_settings.load_admin_settings()["sessions_min_plays"] == 4
    # A store change WITHOUT a save is invisible inside the TTL...
    store["value"] = {"sessions_min_plays": 9}
    assert admin_settings.load_admin_settings()["sessions_min_plays"] == 4
    # ...but a save invalidates immediately.
    admin_settings.save_admin_settings(store["value"])
    assert admin_settings.load_admin_settings()["sessions_min_plays"] == 9
    # The cache hands out copies — mutating a result must not poison it.
    view = admin_settings.load_admin_settings()
    view["sessions_min_plays"] = 999
    assert admin_settings.load_admin_settings()["sessions_min_plays"] == 9
