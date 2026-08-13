#!/usr/bin/env python3
"""Tests for the category carried on the failed-scrapes record.

The record used to be a flat list of item ids with no reason attached, which
made "the post was removed" indistinguishable from "this IP is blocked from
the post" — a verdict that holds only for the vantage point that reached it.
Each new entry now stores its scraper classification alongside the id. The
~102k legacy bare-id records predate this and must keep loading unchanged,
including through the consolidate-and-archive path.

Usage:
    python tests/unit/test_failed_scrape_records.py
    pytest tests/unit/test_failed_scrape_records.py
"""

import os
import sys
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import pytest

from fyp.scrape import scrape


LABEL = "scrape_failed_items"




def _fake_store(files: dict[str, list]):
    """Patch data_io so the scrape location serves ``files`` from memory.

    Returns the dict of saved payloads keyed by filename, so a test can assert
    on what consolidation wrote.
    """
    saved: dict[str, list] = {}

    def fake_listdir(storage_location=None, verbose=False):
        return list(files)

    def fake_load_json(storage_location=None, filename=None, verbose=False):
        return files.get(filename)

    def fake_save_json(data=None, storage_location=None, filename=None, verbose=False):
        saved[filename] = data
        return 0

    def fake_move(src_storage_location=None, dst_storage_location=None,
                  filename=None, verbose=False):
        files.pop(filename, None)

    ctx = [
        patch.object(scrape.data_io, "listdir", fake_listdir),
        patch.object(scrape.data_io, "load_json", fake_load_json),
        patch.object(scrape.data_io, "save_json", fake_save_json),
        patch.object(scrape.data_io, "move", fake_move),
        patch.object(scrape, "_failed_scrapes_label", return_value=LABEL),
    ]
    return saved, ctx




def _load(files: dict[str, list], detail: bool = False):
    """Load the failed-scrapes record against an in-memory store."""
    saved, ctx = _fake_store(files)
    for c in ctx:
        c.start()
    try:
        loader = scrape.load_failed_scrapes_detail if detail else scrape.load_failed_scrapes
        return loader(verbose=False), saved
    finally:
        for c in ctx:
            c.stop()




def test_legacy_bare_ids_still_load():
    """A pre-category record of bare id strings loads with no reason."""
    files = {f"{LABEL}_1.json": ["111", "222", 333]}

    ids, _ = _load(dict(files))
    assert set(ids) == {"111", "222", "333"}, ids

    detail, _ = _load(dict(files), detail=True)
    assert detail == {"111": None, "222": None, "333": None}, detail
    print("PASS: legacy bare ids still load")




def test_category_is_preserved():
    """A record written with categories round-trips them."""
    files = {f"{LABEL}_1.json": [
        {"item_id": "111", "category": "permanent:ip_blocked"},
        {"item_id": "222", "category": "permanent:removed"},
    ]}

    detail, _ = _load(dict(files), detail=True)
    assert detail == {"111": "permanent:ip_blocked", "222": "permanent:removed"}, detail
    print("PASS: category is preserved")




def test_known_category_wins_over_legacy_id():
    """Re-recording a legacy id with a reason upgrades it, in either order."""
    forward = {
        f"{LABEL}_1.json": ["111"],
        f"{LABEL}_2.json": [{"item_id": "111", "category": "permanent:ip_blocked"}],
    }
    reverse = {
        f"{LABEL}_1.json": [{"item_id": "111", "category": "permanent:ip_blocked"}],
        f"{LABEL}_2.json": ["111"],
    }

    for files in (forward, reverse):
        detail, _ = _load(dict(files), detail=True)
        assert detail == {"111": "permanent:ip_blocked"}, detail
    print("PASS: known category wins over legacy id")




def test_load_failed_scrapes_contract_unchanged():
    """Callers still get a flat list of id strings, mixed shapes included."""
    files = {f"{LABEL}_1.json": [
        "111",
        {"item_id": "222", "category": "permanent:ip_blocked"},
    ]}

    ids, _ = _load(dict(files))
    assert isinstance(ids, list)
    assert all(isinstance(one_id, str) for one_id in ids), ids
    assert set(ids) == {"111", "222"}, ids
    print("PASS: load_failed_scrapes contract unchanged")




def test_consolidation_preserves_legacy_ids_and_categories():
    """Consolidating mixed records keeps every id and every known reason."""
    files = {
        f"{LABEL}_1.json": ["111", "222"],
        f"{LABEL}_2.json": [{"item_id": "333", "category": "permanent:ip_blocked"}],
    }

    detail, saved = _load(files, detail=True)

    assert detail == {"111": None, "222": None, "333": "permanent:ip_blocked"}, detail
    assert len(saved) == 1, saved
    payload = next(iter(saved.values()))
    assert {entry["item_id"] for entry in payload} == {"111", "222", "333"}
    by_id = {entry["item_id"]: entry["category"] for entry in payload}
    assert by_id["333"] == "permanent:ip_blocked"
    assert by_id["111"] is None
    print("PASS: consolidation preserves legacy ids and categories")




def test_consolidated_output_reloads_identically():
    """The consolidated file is itself loadable — no one-way shape change."""
    files = {
        f"{LABEL}_1.json": ["111"],
        f"{LABEL}_2.json": [{"item_id": "222", "category": "permanent:removed"}],
    }

    first, saved = _load(files, detail=True)
    second, _ = _load({name: payload for name, payload in saved.items()}, detail=True)

    assert first == second == {"111": None, "222": "permanent:removed"}, (first, second)
    print("PASS: consolidated output reloads identically")




def _failure(category: str) -> pd.DataFrame:
    """An empty fetch result carrying a failure category, like a real miss."""
    empty = pd.DataFrame()
    empty.attrs["error_type"] = category
    return empty




def _metadata_row(item_id: str) -> pd.DataFrame:
    """A >10-column single-row frame like a real fetch result."""
    return pd.DataFrame([{
        "item_id": item_id, "desc": "x", "create_time_raw": pd.Timestamp("2026-01-01"),
        "duration_raw": 30, "author_id": "a", "author_handle": "@a",
        "author_name_raw": "A", "play_count_raw": 1, "fave_count_raw": 0,
        "comment_count_raw": 0, "share_count_raw": 0, "video_downloaded": True,
    }])




def _run_and_capture(ids, fake_dl, **patches):
    """Run a real (non-dry) batch and capture what lands on the failed record.

    A batch with zero successful rows returns before the failed-record save, so
    callers must include at least one success for the record to be written at
    all. The recode/save of those successful rows is stubbed out.
    """
    captured: dict[str, list] = {}

    def fake_save_json(data=None, storage_location=None, filename=None, verbose=False):
        captured["payload"] = data
        return 0

    with ExitStack() as stack:
        stack.enter_context(patch.object(scrape, "download_single_video", side_effect=fake_dl))
        stack.enter_context(patch.object(scrape, "_canonicalize_recode_save",
                                         side_effect=lambda results, *a, **kw: results))
        stack.enter_context(patch.object(scrape.data_io, "save_json", fake_save_json))
        stack.enter_context(patch.object(scrape.scrape_versioning,
                                         "ensure_active_version_registered", lambda: None))
        for name, value in patches.items():
            stack.enter_context(patch.object(scrape, name, return_value=value))
        _, perm, trans = scrape.download_video_threads(
            interesting_videos=ids, max_workers=1,
            dry_run=False, platform="tiktok")

    return captured, perm, trans




@pytest.mark.parametrize("category,expected", [
    ("ip_blocked", "permanent:ip_blocked"),
    ("removed", "permanent:removed"),
    ("network", "transient:network"),
])
def test_writer_records_the_category(category, expected):
    """Each recorded failure carries the scraper's own classification."""
    def fake_dl(video_id=None, **kwargs):
        return _metadata_row(video_id) if video_id == "ok1" else _failure(category)

    captured, _, _ = _run_and_capture(["ok1", "v1"], fake_dl)

    assert captured["payload"] == [{"item_id": "v1", "category": expected}], captured
    print(f"PASS: writer records the category ({category})")




def test_storm_verdict_never_reaches_the_record():
    """A suspect storm classification is never written as a reason.

    The batch aborts, so items it never reached are still recorded — as the
    transient ``unknown`` they actually are. What must not appear is the
    storm's own permanent verdict, which is the session's fault, not the item's.
    """
    ids = ["ok1"] + [f"v{i}" for i in range(10)]

    def fake_dl(video_id=None, **kwargs):
        return _metadata_row(video_id) if video_id == "ok1" else _failure("removed")

    captured, perm, trans = _run_and_capture(
        ids, fake_dl, _permanent_storm_threshold=3)

    assert perm == [], f"storm ids must not be marked permanent: {perm}"
    assert set(trans) == set(ids) - {"ok1"}, "storm ids must stay queued"
    categories = {entry["category"] for entry in captured.get("payload", [])}
    assert "permanent:removed" not in categories, categories
    assert categories <= {"transient:batch_aborted"}, categories
    print("PASS: storm verdict never reaches the record")




if __name__ == "__main__":
    test_legacy_bare_ids_still_load()
    test_category_is_preserved()
    test_known_category_wins_over_legacy_id()
    test_load_failed_scrapes_contract_unchanged()
    test_consolidation_preserves_legacy_ids_and_categories()
    test_consolidated_output_reloads_identically()
    for _cat, _exp in [("ip_blocked", "permanent:ip_blocked"),
                       ("removed", "permanent:removed"),
                       ("network", "transient:network")]:
        test_writer_records_the_category(_cat, _exp)
    test_storm_verdict_never_reaches_the_record()
    print("All failed-scrape record tests passed.")
