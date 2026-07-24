#!/usr/bin/env python3
"""Tests for the annotation-queue selection helper (version / timeframe modes).

Covers ``_select_annotated_item_ids`` (pure filtering over the all-versions
annotation archive) and ``_parse_selection_date`` in
``web_interface.routes.management.enrichment``: version equality, timeframe
boundary semantics (from inclusive / to exclusive), NA-``inference_ts``
exclusion + count, study intersection, and the annotated_ok gate.

Usage:
    PYTHONPATH=. python -m pytest tests/unit/test_annotation_queue_selection.py
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from web_interface.routes.management.enrichment import (
    _parse_selection_date,
    _select_annotated_item_ids,
)




def _archive() -> pd.DataFrame:
    return pd.DataFrame({
        "item_id": ["a", "b", "c", "d", "e", "a"],
        "source_platform": ["tiktok"] * 6,
        "annotation_version": ["av_1", "av_1", "av_2", "av_2", "av_2", "av_2"],
        "annotated_ok": [True, False, True, True, True, True],
        "inference_ts": pd.array([100, 110, 200, pd.NA, 300, 250], dtype="int64[pyarrow]"),
    })




def test_version_filter_requires_annotated_ok():
    ids, skipped = _select_annotated_item_ids(_archive(), version="av_1")
    # "b" is av_1 but annotated_ok=False.
    assert ids == ["a"]
    assert skipped == 0




def test_version_filter_selects_all_ok_rows():
    ids, skipped = _select_annotated_item_ids(_archive(), version="av_2")
    assert ids == ["a", "c", "d", "e"]
    assert skipped == 0




def test_timeframe_boundaries_inclusive_from_exclusive_to():
    # from=200 inclusive, to=300 exclusive → c (200) and a (250), not e (300).
    ids, skipped = _select_annotated_item_ids(_archive(), ts_from=200, ts_to=300)
    assert ids == ["a", "c"]
    # "d" is annotated_ok with NA inference_ts → counted as skipped.
    assert skipped == 1




def test_timeframe_open_ended():
    ids, _ = _select_annotated_item_ids(_archive(), ts_from=250)
    assert ids == ["a", "e"]
    ids, _ = _select_annotated_item_ids(_archive(), ts_to=150)
    assert ids == ["a"]




def test_timeframe_without_inference_ts_column():
    archive = _archive().drop(columns=["inference_ts"])
    ids, skipped = _select_annotated_item_ids(archive, ts_from=0, ts_to=10**10)
    assert ids == []
    # Every annotated_ok row is unfilterable → all reported as skipped.
    assert skipped == 5




def test_study_intersection():
    ids, skipped = _select_annotated_item_ids(
        _archive(), version="av_2", study_item_ids={"c", "d"}
    )
    assert ids == ["c", "d"]
    assert skipped == 0
    # The skipped-count respects the study scope too: "d" (NA ts) is inside,
    # so a timeframe query over the same scope reports exactly one skip.
    ids, skipped = _select_annotated_item_ids(
        _archive(), ts_from=0, ts_to=10**10, study_item_ids={"c", "d"}
    )
    assert ids == ["c"]
    assert skipped == 1




def test_empty_archive():
    assert _select_annotated_item_ids(pd.DataFrame(), version="av_1") == ([], 0)
    assert _select_annotated_item_ids(None, version="av_1") == ([], 0)




def test_parse_selection_date_utc_midnight():
    assert _parse_selection_date("1970-01-02") == 86400
    # Explicit offsets are honoured.
    assert _parse_selection_date("1970-01-02T00:00:00+01:00") == 86400 - 3600




def run():
    test_version_filter_requires_annotated_ok()
    test_version_filter_selects_all_ok_rows()
    test_timeframe_boundaries_inclusive_from_exclusive_to()
    test_timeframe_open_ended()
    test_timeframe_without_inference_ts_column()
    test_study_intersection()
    test_empty_archive()
    test_parse_selection_date_utc_midnight()
    print("PASS: annotation queue selection")




if __name__ == "__main__":
    run()
