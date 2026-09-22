"""`run_ingest_refresh._removed_rows_breakdown` splits the merge's removals.

The merge dedupes the whole dataset, so the gap between the rows a run's files
kept and the net dataset change is either older copies a re-donation replaced
in its own collection, or duplicates already stored in collections the run
never touched (2026-09-23: an Instagram upload became a new collection while
828 duplicate share rows a migration had appended to TikTok collections were
cleared — the panel blamed the new upload).
"""

import pandas as pd

from web_interface.run_ingest_refresh import _removed_rows_breakdown


def _frame(rows: list[tuple[str, str, int]]) -> pd.DataFrame:
    """(raw_file, collection_id, n_rows) → a frame with that many rows each."""
    out = []
    for rf, cid, n in rows:
        out += [{"raw_file": rf, "collection_id": cid}] * n
    return pd.DataFrame(out)


def _added(filename: str, cid: str, outcome: str = "added_as_new") -> dict:
    return {"filename": filename, "canonical_collection_id": cid, "outcome": outcome}


def test_new_collection_plus_cleanup_elsewhere_is_not_blamed_on_the_upload():
    final = _frame([("old_a.json", "A", 90), ("old_b.json", "B", 50), ("new.zip", "N", 10)])
    replaced, elsewhere = _removed_rows_breakdown(
        final,
        pre_counts={"old_a.json": 100, "old_b.json": 50},
        pre_cids={"old_a.json": "A", "old_b.json": "B"},
        cid_remap={},
        per_file_summary=[_added("new.zip", "N")],
    )
    assert replaced == 0
    assert elsewhere == {"A": 10}


def test_rows_a_redonation_replaced_count_as_replaced():
    final = _frame([("old.json", "C", 70), ("new.json", "C", 40)])
    replaced, elsewhere = _removed_rows_breakdown(
        final,
        pre_counts={"old.json": 100},
        pre_cids={"old.json": "C"},
        cid_remap={},
        per_file_summary=[_added("new.json", "C", "merged_with_existing")],
    )
    assert replaced == 30
    assert elsewhere == {}


def test_fully_replaced_older_file_follows_the_cid_remap():
    # The older file lost every row, so only its pre-merge collection id and
    # the clustering's remap say it joined the new file's collection.
    final = _frame([("new.json", "NEW", 120)])
    replaced, elsewhere = _removed_rows_breakdown(
        final,
        pre_counts={"old.json": 100},
        pre_cids={"old.json": "OLD"},
        cid_remap={"OLD": "NEW"},
        per_file_summary=[_added("new.json", "NEW", "merged_with_existing")],
    )
    assert replaced == 100
    assert elsewhere == {}
