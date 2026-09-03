"""A routine enrichment append re-segments only the collections it touches.

2026-09-03, prod: 50 newly annotated videos → 46 vectors appended to the
embedding store → the sessions planner saw a moved store fingerprint and
rebuilt all 99 covered collections (~8 min, 8 chain links), though only the
collections containing those 50 videos could have changed. The global rule
existed because the per-collection fingerprint is computed from the activity
file and cannot see enrichment at all; the store fingerprint was the only
signal that the vector set grew. This change works out WHERE it grew:

  * the embedding shards are append-only, so if every shard the previous
    build recorded is still present byte-identical, the store only grew
    (`shards_appended_only`);
  * rows at or past the previous build's vector count are the new vectors,
    and the dense index maps them to item ids (`new_vector_item_ids`);
  * annotation rows carry an `inference_ts`; those past the previous build's
    watermark are the changed annotations (`annotation_items_changed_since`);
  * the activity file maps items to collections (`collections_containing`).

Untouched collections keep the corpus mean they were centred on. That drift is
negligible for an append but must not accumulate: once appends exceed
`[sessions] rebaseline_fraction` of the corpus since the last full build, the
next refresh is full again. Anything the scope cannot prove — a rewritten
shard, a build from before the shard set / watermark were recorded, an
unmappable id set — falls back to the full rebuild exactly as before.

Usage:
    python -m pytest tests/unit/test_sessions_enrichment_scope.py
"""

from __future__ import annotations

import types

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

import fyp.data_io as data_io
from fyp.analysis import embedding_store, embeddings
from fyp.analysis import session_explorer as se

WIDE = [["1969-12-29", "2100-01-04"]]
PARAMS = {"cut": 0.5, "mem": 6}
MODEL = "test-model"
TREND = ["log_plays"]

OLD_SHARDS = [["shard_a.parquet", 100, 1.0], ["shard_b.parquet", 200, 2.0]]
NEW_SHARD = ["shard_c.parquet", 50, 3.0]


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def test_shards_appended_only_accepts_growth_and_rejects_rewrites():
    assert se.shards_appended_only(OLD_SHARDS, OLD_SHARDS + [NEW_SHARD])
    assert se.shards_appended_only(OLD_SHARDS, OLD_SHARDS)
    # a shard changed size → rewritten
    assert not se.shards_appended_only(OLD_SHARDS, [["shard_a.parquet", 101, 1.0], OLD_SHARDS[1]])
    # a shard changed mtime → rewritten
    assert not se.shards_appended_only(OLD_SHARDS, [OLD_SHARDS[0], ["shard_b.parquet", 200, 2.5]])
    # a shard vanished → compaction
    assert not se.shards_appended_only(OLD_SHARDS, [OLD_SHARDS[0], NEW_SHARD])
    # nothing recorded → cannot prove anything
    assert not se.shards_appended_only([], OLD_SHARDS)
    assert not se.shards_appended_only(None, OLD_SHARDS)


def _index(ids, rows):
    return types.SimpleNamespace(ids=pa.array(ids, type=pa.string()),
                                 rows=np.asarray(rows, dtype=np.int32))


def test_new_vector_item_ids_are_the_rows_past_the_old_count():
    # index is sorted by item_id, not by row — rows are what matter
    idx = _index(["a", "b", "c", "d"], [3, 0, 5, 1])
    assert se.new_vector_item_ids(idx, old_count=4) == {"c"}
    assert se.new_vector_item_ids(idx, old_count=2) == {"a", "c"}
    assert se.new_vector_item_ids(idx, old_count=6) == set()
    assert se.new_vector_item_ids(idx, old_count=0) == {"a", "b", "c", "d"}


def _batches(monkeypatch, frames_by_file: dict):
    """iter_parquet_batches / exists stand-ins serving one frame per filename."""
    def fake_iter(storage_location="", filename="", columns=None, filters=None, **kw):
        df = frames_by_file[filename]
        if filters:
            for col, op, val in filters:
                assert op == "in"
                df = df[df[col].astype(str).isin([str(v) for v in val])]
        if columns:
            df = df[columns]
        yield pa.RecordBatch.from_pandas(df.reset_index(drop=True), preserve_index=False)

    monkeypatch.setattr(data_io, "iter_parquet_batches", fake_iter)
    monkeypatch.setattr(data_io, "exists",
                        lambda storage_location="", filename="", **k: filename in frames_by_file)


def test_changed_annotations_are_those_past_the_watermark(monkeypatch):
    anno = pd.DataFrame({
        "item_id": ["v1", "v2", "v3", "v4"],
        "inference_ts": ["2026-09-01T00:00:00+00:00", "2026-09-03T03:20:00+00:00",
                         None, "2026-09-03T03:25:00+00:00"],
    })
    _batches(monkeypatch, {embeddings.ANNOTATIONS_FILE: anno})

    changed, max_ts = se.annotation_items_changed_since("2026-09-02T00:00:00+00:00")
    assert changed == {"v2", "v4"}, "rows without a timestamp never count"
    assert max_ts == "2026-09-03T03:25:00+00:00"

    # No watermark: unknown, but the corpus max is still reported.
    changed, max_ts = se.annotation_items_changed_since(None)
    assert changed is None and max_ts == "2026-09-03T03:25:00+00:00"
    assert se.annotation_corpus_max_ts() == "2026-09-03T03:25:00+00:00"


def test_collections_containing_maps_items_within_the_covered_set(monkeypatch):
    activity = pd.DataFrame({
        "collection_id": ["c1", "c1", "c2", "c3", "c9"],
        "item_id": ["v1", "v2", "v2", "v7", "v1"],
    })
    _batches(monkeypatch, {f"{se.COLLECTIONS_LABEL}_recoded.parquet": activity})

    assert se.collections_containing({"v1", "v2"}, allow={"c1", "c2", "c3"}) == {"c1", "c2"}
    assert se.collections_containing({"v7"}, allow={"c1", "c2"}) == set()
    assert se.collections_containing(set(), allow={"c1"}) == set()
    # an absurd id set is not worth mapping — the caller rebuilds everything
    assert se.collections_containing({str(i) for i in range(10)}, {"c1"}, max_items=5) is None


# --------------------------------------------------------------------------
# enrichment_change_scope — the decision, end to end on stubs
# --------------------------------------------------------------------------

def _meta(**over):
    meta = {"store_fingerprint": "fp-old", "annotations_fingerprint": "afp-old",
            "corpus_mean_count": 4, "baseline_corpus_count": 4,
            "store_shards": OLD_SHARDS, "annotations_max_ts": "2026-09-02T00:00:00+00:00",
            "collections": {}}
    meta.update(over)
    return meta


@pytest.fixture
def world(monkeypatch):
    """A store that grew by two vectors (items e, f) and one new annotation (f)."""
    monkeypatch.setattr(embedding_store, "shard_entries",
                        lambda: [tuple(s) for s in OLD_SHARDS] + [tuple(NEW_SHARD)])
    monkeypatch.setattr(embedding_store, "load_index",
                        lambda model: _index(["a", "b", "c", "d", "e", "f"], [0, 1, 2, 3, 4, 5]))
    anno = pd.DataFrame({"item_id": ["a", "f"],
                         "inference_ts": ["2026-08-01T00:00:00+00:00", "2026-09-03T03:25:00+00:00"]})
    activity = pd.DataFrame({"collection_id": ["c1", "c2", "c2", "c3", "c4"],
                             "item_id": ["a", "e", "b", "f", "e"]})
    _batches(monkeypatch, {embeddings.ANNOTATIONS_FILE: anno,
                           f"{se.COLLECTIONS_LABEL}_recoded.parquet": activity})
    return {"covered": {"c1", "c2", "c3"}}   # c4 is in no study


def test_append_scopes_to_the_collections_holding_the_new_items(world):
    out = se.enrichment_change_scope(_meta(), "fp-new", 6, "afp-new", MODEL,
                                     world["covered"], fraction=0.5)
    assert out["local"] is True
    assert out["affected"] == {"c2", "c3"}          # e→c2 (c4 uncovered), f→c3
    assert out["n_new_vectors"] == 2 and out["n_changed_annotations"] == 1
    assert out["annotations_max_ts"] == "2026-09-03T03:25:00+00:00"


def test_unchanged_enrichment_is_local_and_empty(world):
    out = se.enrichment_change_scope(_meta(), "fp-old", 4, "afp-old", MODEL, world["covered"])
    assert out["local"] is True and out["affected"] == set()


def test_rewritten_shards_are_not_local(world, monkeypatch):
    monkeypatch.setattr(embedding_store, "shard_entries",
                        lambda: [("shard_a.parquet", 999, 1.0), tuple(OLD_SHARDS[1])])
    out = se.enrichment_change_scope(_meta(), "fp-new", 6, "afp-old", MODEL, world["covered"])
    assert out["local"] is False and "rewritten" in out["reason"]


def test_drift_budget_forces_a_rebaseline(world):
    # 4 → 6 vectors is +50 % on the baseline; a 10 % budget refuses
    out = se.enrichment_change_scope(_meta(), "fp-new", 6, "afp-old", MODEL,
                                     world["covered"], fraction=0.10)
    assert out["local"] is False and "drift budget" in out["reason"]
    # …and the budget is measured from the last FULL build, not the last merge
    out = se.enrichment_change_scope(_meta(baseline_corpus_count=100, corpus_mean_count=4),
                                     "fp-new", 6, "afp-old", MODEL, world["covered"], fraction=0.10)
    assert out["local"] is True


def test_first_build_after_this_rule_bootstraps_once(world):
    # a meta from before shard sets were recorded
    out = se.enrichment_change_scope(_meta(store_shards=None), "fp-new", 6, "afp-old",
                                     MODEL, world["covered"])
    assert out["local"] is False and "no shard set" in out["reason"]
    # a meta from before the annotation watermark was recorded
    out = se.enrichment_change_scope(_meta(annotations_max_ts=None), "fp-old", 4, "afp-new",
                                     MODEL, world["covered"])
    assert out["local"] is False and "no annotation watermark" in out["reason"]
    assert out["annotations_max_ts"] == "2026-09-03T03:25:00+00:00", \
        "the watermark is still reported so this build can record it"


def test_unmappable_ids_are_not_local(world, monkeypatch):
    monkeypatch.setattr(se, "collections_containing", lambda *a, **k: None)
    out = se.enrichment_change_scope(_meta(), "fp-new", 6, "afp-old", MODEL, world["covered"],
                                     fraction=0.9)
    assert out["local"] is False and "could not map" in out["reason"]


# --------------------------------------------------------------------------
# The planner with a scope
# --------------------------------------------------------------------------

def _plan_meta(collections, **over):
    meta = {"embedding_model": MODEL, "params": dict(PARAMS), "trend_vars": list(TREND),
            "store_fingerprint": "fp1", "annotations_fingerprint": "afp1",
            "collections": collections}
    meta.update(over)
    return meta


def _rec(n):
    return {"windows": WIDE, "n_plays": n, "built_at": "2026-08-01T00:00:00Z"}


def _plan(discovered, meta, **over):
    kwargs = {"params": PARAMS, "model": MODEL, "trend_cols": TREND,
              "artifacts_exist": True, "plays_schema_ok": True, "scope": None,
              "store_fp": "fp1", "annotations_fp": "afp1", "enrichment_scope": None}
    kwargs.update(over)
    return se.compute_refresh_plan(discovered, {c: WIDE for c, _ in discovered}, meta, **kwargs)


def test_a_local_store_change_merges_only_the_touched_collections():
    disc = [("a", 10), ("b", 20), ("c", 30)]
    meta = _plan_meta({"a": _rec(10), "b": _rec(20), "c": _rec(30)}, store_fingerprint="OLD")
    plan = _plan(disc, meta, enrichment_scope={"local": True, "affected": {"b"}})
    assert plan["mode"] == "merge" and plan["refresh"] == ["b"] and plan["drop"] == []
    assert "touched by new enrichment" in plan["reason"]


def test_without_a_scope_the_store_change_still_rebuilds_everything():
    disc = [("a", 10), ("b", 20)]
    meta = _plan_meta({"a": _rec(10), "b": _rec(20)}, store_fingerprint="OLD")
    assert _plan(disc, meta)["mode"] == "full"
    assert _plan(disc, meta, enrichment_scope={"local": False, "affected": None})["mode"] == "full"


def test_a_local_annotation_change_merges_the_touched_collections():
    disc = [("a", 10), ("b", 20)]
    meta = _plan_meta({"a": _rec(10), "b": _rec(20)}, annotations_fingerprint="OLD")
    plan = _plan(disc, meta, enrichment_scope={"local": True, "affected": {"a"}})
    assert plan["mode"] == "merge" and plan["refresh"] == ["a"]


def test_touched_and_stale_combine_in_discovery_order():
    disc = [("a", 10), ("b", 20), ("c", 30)]
    meta = _plan_meta({"a": _rec(10), "b": _rec(99), "c": _rec(30)}, store_fingerprint="OLD")
    plan = _plan(disc, meta, enrichment_scope={"local": True, "affected": {"c"}})
    assert plan["refresh"] == ["b", "c"]   # b stale by play count, c touched


def test_a_local_change_touching_nothing_records_the_fingerprints():
    disc = [("a", 10)]
    meta = _plan_meta({"a": _rec(10)}, store_fingerprint="OLD")
    plan = _plan(disc, meta, enrichment_scope={"local": True, "affected": set()})
    assert plan["mode"] == "merge" and plan["refresh"] == [] and plan["drop"] == []
    assert "recording the fingerprints" in plan["reason"]


def test_explicit_scope_still_narrows_touched_collections():
    disc = [("a", 10), ("b", 20)]
    meta = _plan_meta({"a": _rec(10), "b": _rec(20)}, store_fingerprint="OLD")
    plan = _plan(disc, meta, scope={"a"}, enrichment_scope={"local": True, "affected": {"a", "b"}})
    assert plan["refresh"] == ["a"]


# --------------------------------------------------------------------------
# The published meta carries what the next run needs
# --------------------------------------------------------------------------

def test_merge_meta_records_shards_watermark_and_carries_the_baseline(monkeypatch):
    from web_interface import run_sessions_refresh as rsr

    monkeypatch.setattr(embedding_store, "shard_entries",
                        lambda: [tuple(s) for s in OLD_SHARDS] + [tuple(NEW_SHARD)])
    fp = embedding_store.fingerprint_of([tuple(s) for s in OLD_SHARDS] + [tuple(NEW_SHARD)])
    old = {"store_fingerprint": "fp-old", "baseline_corpus_count": 4, "corpus_mean_count": 4,
           "annotations_max_ts": "2026-09-02T00:00:00+00:00", "collections": {}}

    meta = rsr._merge_meta(old, {"refresh": [], "drop": []}, PARAMS, MODEL, fp, 6, 8,
                           TREND, "afp-new", "2026-09-03T03:25:00+00:00")
    assert meta["baseline_corpus_count"] == 4, "a merge does not move the baseline"
    assert meta["corpus_mean_count"] == 6
    assert meta["annotations_max_ts"] == "2026-09-03T03:25:00+00:00"
    assert meta["store_shards"] == [[n, s, m] for n, s, m in OLD_SHARDS + [NEW_SHARD]]
    assert meta["corpus_mean_drift"] is True

    # watermark carried forward when this run did not scan the corpus
    meta = rsr._merge_meta(old, {"refresh": [], "drop": []}, PARAMS, MODEL, fp, 6, 8, TREND, "afp-old")
    assert meta["annotations_max_ts"] == "2026-09-02T00:00:00+00:00"

    # shards are only recorded when they still match the pinned fingerprint
    assert rsr._store_shards_for("some-other-fp") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
