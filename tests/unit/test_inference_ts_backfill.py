#!/usr/bin/env python3
"""Tests for scripts/backfill_inference_ts.py map-building and stamping.

Uses an in-memory data_io stub — no real storage is touched. Covers the raw
JSON → timestamp map extraction (with defaulting mirroring the refine step),
refined-parquet stamping incl. idempotency, and malformed-entry tolerance.

Usage:
    PYTHONPATH=. python -m pytest tests/unit/test_inference_ts_backfill.py
"""

import importlib.util
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _load_script():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "scripts", "backfill_inference_ts.py",
    )
    spec = importlib.util.spec_from_file_location("backfill_inference_ts", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod




class _StubDataIO:
    """In-memory json/parquet store keyed on (location, filename)."""

    def __init__(self):
        self.json = {}
        self.parquet = {}
        self.saved_parquets = []

    def exists(self, storage_location=None, filename=None, **kw):
        return (storage_location, filename) in self.json \
            or (storage_location, filename) in self.parquet

    def load_json(self, storage_location=None, filename=None, **kw):
        return self.json[(storage_location, filename)]

    def load_parquet(self, storage_location=None, filename=None, **kw):
        return self.parquet[(storage_location, filename)].copy()

    def save_parquet(self, df=None, storage_location=None, filename=None, **kw):
        self.parquet[(storage_location, filename)] = df.copy()
        self.saved_parquets.append((storage_location, filename))




def test_load_raw_ts_map_defaults_and_malformed():
    mod = _load_script()
    stub = _StubDataIO()
    stub.json[("machine_annotations_raw", "f.json")] = {
        "0": {"item_id": "a", "inference_ts": 100,
              "source_platform": "youtube", "annotation_version": "av_9"},
        "1": {"item_id": "b", "inference_ts": "200"},          # legacy entry, no platform/version
        "2": {"item_id": "c"},                                  # no inference_ts → malformed
        "3": "not-a-dict",                                      # malformed
    }
    orig = mod.data_io
    mod.data_io = stub
    try:
        by_item, by_key, malformed = mod.load_raw_ts_map("f.json")
    finally:
        mod.data_io = orig

    assert by_item == {"a": 100, "b": 200}
    assert by_key[("youtube", "a", "av_9")] == 100
    # Legacy entries default platform + version exactly like the refine step.
    import fyp.scrape_queues as scrape_queues
    from fyp.annotation import annotation_versioning
    assert by_key[(scrape_queues.default_platform(), "b",
                   annotation_versioning.LEGACY_VERSION)] == 200
    assert malformed == 2




def test_stamp_refined_file_fills_and_is_idempotent():
    mod = _load_script()
    stub = _StubDataIO()
    stub.parquet[("machine_annotations_refined", "f.parquet")] = pd.DataFrame({
        "item_id": ["a", "b", "c"],
    })
    orig = mod.data_io
    mod.data_io = stub
    try:
        stamped = mod.stamp_refined_file("f.parquet", {"a": 100, "b": 200}, dry_run=False)
        assert stamped == 2
        df = stub.parquet[("machine_annotations_refined", "f.parquet")]
        assert str(df["inference_ts"].dtype) == "int64[pyarrow]"
        assert df["inference_ts"].tolist()[:2] == [100, 200]
        assert pd.isna(df["inference_ts"].iloc[2])

        # Second run is a no-op (nothing left to fill).
        saves_before = len(stub.saved_parquets)
        assert mod.stamp_refined_file("f.parquet", {"a": 100, "b": 200}, dry_run=False) == 0
        assert len(stub.saved_parquets) == saves_before

        # A late-arriving mapping only fills the remaining NA.
        assert mod.stamp_refined_file("f.parquet", {"a": 999, "c": 300}, dry_run=False) == 1
        df = stub.parquet[("machine_annotations_refined", "f.parquet")]
        assert df["inference_ts"].tolist() == [100, 200, 300]
    finally:
        mod.data_io = orig




def test_stamp_refined_file_dry_run_saves_nothing():
    mod = _load_script()
    stub = _StubDataIO()
    stub.parquet[("machine_annotations_refined", "f.parquet")] = pd.DataFrame({
        "item_id": ["a"],
    })
    orig = mod.data_io
    mod.data_io = stub
    try:
        assert mod.stamp_refined_file("f.parquet", {"a": 100}, dry_run=True) == 1
        assert stub.saved_parquets == []
        df = stub.parquet[("machine_annotations_refined", "f.parquet")]
        assert "inference_ts" not in df.columns
    finally:
        mod.data_io = orig




def run():
    test_load_raw_ts_map_defaults_and_malformed()
    test_stamp_refined_file_fills_and_is_idempotent()
    test_stamp_refined_file_dry_run_saves_nothing()
    print("PASS: inference_ts backfill")




if __name__ == "__main__":
    run()
