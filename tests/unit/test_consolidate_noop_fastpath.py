"""The no-op consolidation fast path must not touch corpus-scale data.

Pinned 2026-09-02: a consolidate_enrichment run with zero new files still cost
265-335 s in prod — the quiet lane paths downloaded both recoded blobs
(~0.8 GB) just to return them, and update_enrichment_status rebuilt the whole
status parquet from the event-level activity table. consolidate_enrichment_data
now (a) calls the lanes with return_saved_data=False so quiet lanes return
(False, None, set()), and (b) skips the status rebuild entirely when both lanes
are quiet AND the persisted input-fingerprint marker matches
(_status_inputs_unchanged). The marker is written AFTER the status parquet save
so a crash between the two can only cost an extra rebuild, never skip one.

Usage:
    python tests/unit/test_consolidate_noop_fastpath.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

import fyp.analysis.datasets.common as datasets_common
import fyp.analysis.datasets.enrichment_status as datasets_status
import fyp.analysis.datasets.refresh as datasets_refresh
import fyp.analysis.organize_datasets as od
import fyp.core.fyp_config
from fyp.core.fyp_config import fyp_cf


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def stub(self, name, retval=None):
        def _fn(*a, **k):
            self.calls.append((name, k))
            return retval

        return _fn

    def names(self):
        return [name for name, _ in self.calls]


def _patched(rec: _Recorder, *, quiet: bool, marker_matches: bool):
    """Patch the heavy sub-steps; return the originals for restore."""
    lanes_ret = (False, None, set()) if quiet else (True, pd.DataFrame(), {"vid1"})
    orig = {
        "anno": datasets_status.consolidate_and_save_refined_annotations,
        "scrape": datasets_status.consolidate_and_save_scrape_data,
        "status": datasets_status.update_enrichment_status,
        "load": od.data_io.load_parquet,
        "exists": od.data_io.exists,
        "marker": datasets_refresh._status_inputs_unchanged,
    }
    datasets_status.consolidate_and_save_refined_annotations = rec.stub("lane_anno", lanes_ret)
    datasets_status.consolidate_and_save_scrape_data = rec.stub("lane_scrape", (False, None, set()))
    datasets_status.update_enrichment_status = rec.stub("update_enrichment_status")
    od.data_io.load_parquet = rec.stub(
        "load_parquet",
        pd.DataFrame({"item_id": ["vid1"], datasets_common.collection_id_column: ["c1"]}),
    )
    od.data_io.exists = rec.stub("exists", True)
    datasets_refresh._status_inputs_unchanged = rec.stub("marker_check", marker_matches)
    fyp_cf["study_defs"] = {}
    return orig


def _restore(orig) -> None:
    datasets_status.consolidate_and_save_refined_annotations = orig["anno"]
    datasets_status.consolidate_and_save_scrape_data = orig["scrape"]
    datasets_status.update_enrichment_status = orig["status"]
    od.data_io.load_parquet = orig["load"]
    od.data_io.exists = orig["exists"]
    datasets_refresh._status_inputs_unchanged = orig["marker"]


def test_noop_fast_path_skips_all_corpus_reads() -> None:
    rec = _Recorder()
    orig = _patched(rec, quiet=True, marker_matches=True)
    try:
        result = datasets_status.consolidate_enrichment_data(
            force_consolidation=False, verbose=False
        )
    finally:
        _restore(orig)
    assert result["had_new_data"] is False
    assert result["impact"] is None
    assert "update_enrichment_status" not in rec.names(), "status rebuilt on a no-op run"
    assert "load_parquet" not in rec.names(), "corpus parquet loaded on a no-op run"


def test_marker_mismatch_forces_status_rebuild() -> None:
    """Quiet lanes but drifted inputs (e.g. failed-scrapes changed) must rebuild."""
    rec = _Recorder()
    orig = _patched(rec, quiet=True, marker_matches=False)
    try:
        result = datasets_status.consolidate_enrichment_data(
            force_consolidation=False, verbose=False
        )
    finally:
        _restore(orig)
    assert result["had_new_data"] is False
    assert "update_enrichment_status" in rec.names()
    # Quiet lanes returned None frames — the rebuild path must lazy-load them
    # (collections + both recoded frames).
    assert rec.names().count("load_parquet") == 3


def test_new_data_bypasses_fast_path() -> None:
    rec = _Recorder()
    orig = _patched(rec, quiet=False, marker_matches=True)
    try:
        result = datasets_status.consolidate_enrichment_data(
            force_consolidation=False, verbose=False
        )
    finally:
        _restore(orig)
    assert result["had_new_data"] is True
    assert "update_enrichment_status" in rec.names()
    # The marker check must not even run when there is new data.
    assert "marker_check" not in rec.names()
    assert result["impact"] is not None
    assert result["impact"]["changed_item_count"] == 1


def test_lanes_called_without_return_saved_data() -> None:
    rec = _Recorder()
    orig = _patched(rec, quiet=True, marker_matches=True)
    try:
        datasets_status.consolidate_enrichment_data(force_consolidation=False, verbose=False)
    finally:
        _restore(orig)
    for name, kwargs in rec.calls:
        if name.startswith("lane_"):
            assert kwargs.get("return_saved_data") is False, (
                f"{name} must be called with return_saved_data=False"
            )


def test_marker_written_after_status_save() -> None:
    """Inside the real update_enrichment_status, the marker write follows the save."""
    order: list[str] = []
    orig_save = od.data_io.save_parquet
    orig_marker = datasets_refresh._write_status_inputs_marker
    orig_exists = od.data_io.exists
    od.data_io.save_parquet = lambda **k: order.append("save_parquet")
    datasets_refresh._write_status_inputs_marker = lambda **k: order.append("marker")
    od.data_io.exists = lambda **k: False  # no prior status file / votes
    orig_failed = datasets_status.load_failed_scrapes
    datasets_status.load_failed_scrapes = lambda **k: []
    try:
        collections = pd.DataFrame(
            {
                "item_id": pd.array(["aaa", "aaa", "bbb"], dtype="string[pyarrow]"),
                datasets_common.collection_id_column: pd.array(
                    ["c1", "c2", "c1"], dtype="string[pyarrow]"
                ),
            }
        )
        datasets_status.update_enrichment_status(
            all_datasets={
                datasets_common._collections_label(): collections,
                datasets_common._machine_annotations_label(): pd.DataFrame(),
                datasets_common._scrapes_label(): pd.DataFrame(),
            },
            save_to_disk=True,
            verbose=False,
        )
    finally:
        od.data_io.save_parquet = orig_save
        datasets_refresh._write_status_inputs_marker = orig_marker
        od.data_io.exists = orig_exists
        datasets_status.load_failed_scrapes = orig_failed
    assert order == ["save_parquet", "marker"], f"unexpected order: {order}"


_TESTS = [
    test_noop_fast_path_skips_all_corpus_reads,
    test_marker_mismatch_forces_status_rebuild,
    test_new_data_bypasses_fast_path,
    test_lanes_called_without_return_saved_data,
    test_marker_written_after_status_save,
]


def _main() -> int:
    passed = 0
    for test in _TESTS:
        try:
            test()
            print(f"PASS  {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{passed}/{len(_TESTS)} passed")
    return 0 if passed == len(_TESTS) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
