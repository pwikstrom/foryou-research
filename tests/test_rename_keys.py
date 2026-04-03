"""
Tests for rename_keys_and_columns.py

The key bug: rename_json_file_keys computes `changes` by zipping sorted
original keys with sorted new keys.  When a rename shifts a key's
alphabetical position the zip pairs the wrong keys, producing bogus
change reports.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rename_keys_and_columns import (
    apply_rename,
    detect_json_conflicts,
    detect_parquet_conflicts,
    extract_keys,
    rename_json_keys_recursive,
    rename_json_file_keys,
)


RENAME_MAP: dict[str, str] = {}
STRIP_PREFIXES: list[str] = ["G_", "T_", "D_", "S_", "B_"]


# ── helpers ──────────────────────────────────────────────────────────────────

def _write_json(data: dict, tmp_dir: Path) -> Path:
    p = tmp_dir / "test.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ── unit tests ────────────────────────────────────────────────────────────────

def test_apply_rename_strip_prefix():
    assert apply_rename("G_foo", {}, ["G_"]) == "foo"


def test_apply_rename_dict_wins_over_strip():
    """Explicit dict rename takes priority; no additional prefix strip."""
    assert apply_rename("D_watch_duration", {"D_watch_duration": "play_duration"}, ["D_"]) == "play_duration"


def test_extract_keys_flat():
    data = {"a": 1, "b": 2}
    assert extract_keys(data) == {"a", "b"}


def test_extract_keys_nested():
    data = {"a": {"b": {"c": 1}}}
    assert extract_keys(data) == {"a", "a.b", "a.b.c"}


def test_recursive_rename_strips_prefix():
    data = {"G_foo": {"G_bar": 1}, "G_baz": 2}
    result = rename_json_keys_recursive(data, {}, ["G_"])
    assert result == {"foo": {"bar": 1}, "baz": 2}


def test_recursive_rename_dict_map():
    data = {"D_donation_id": 42}
    result = rename_json_keys_recursive(data, {"D_donation_id": "collection_id"}, ["D_"])
    assert result == {"collection_id": 42}


# ── the core bug ──────────────────────────────────────────────────────────────

def test_changes_reporting_does_not_cross_pair_keys():
    """
    Reproduces the mis-pairing bug.

    Data has two top-level keys:
      - G_main_activity  (gets prefix stripped → main_activity)
      - desc_hashtags    (unchanged)

    After the rename, sorted new keys are ['desc_hashtags', 'main_activity'].
    Sorted original keys are ['G_main_activity', 'desc_hashtags']
    (uppercase G sorts before lowercase d in ASCII).

    The buggy zip pairs:
      G_main_activity  ↔ desc_hashtags   → reports G_main_activity → desc_hashtags  ✗
      desc_hashtags    ↔ main_activity   → reports desc_hashtags → main_activity     ✗

    The correct report should be:
      G_main_activity  → main_activity                                               ✓
    """
    data = {
        "G_main_activity": {"categories": [1, 2, 3]},
        "desc_hashtags": ["tag_a", "tag_b"],
    }

    with tempfile.TemporaryDirectory() as tmp:
        f = _write_json(data, Path(tmp))
        result = rename_json_file_keys(f, RENAME_MAP, STRIP_PREFIXES, dry_run=True)

    changes = result["changes"]
    print("  changes reported:", changes)

    assert changes == {
        "G_main_activity": "main_activity",
        "G_main_activity.categories": "main_activity.categories",
    }, (
        f"Wrong changes dict: {changes}\n"
        "Bug: zip(sorted(original), sorted(new)) cross-pairs keys when a rename "
        "shifts alphabetical order."
    )


def test_changes_reporting_full_example():
    """Mirrors the example from the bug report."""
    data = {
        "G_content_category": {
            "n_periods": 4,
            "start_offset": 0,
            "time_labels": ["a", "b"],
        },
        "G_main_activity": {
            "categories": {
                "anomalies": {"index": 0, "mean": 1.5}
            }
        },
        "desc_hashtags": ["tag"],
    }

    with tempfile.TemporaryDirectory() as tmp:
        f = _write_json(data, Path(tmp))
        result = rename_json_file_keys(f, RENAME_MAP, STRIP_PREFIXES, dry_run=True)

    changes = result["changes"]
    print("  changes reported:", changes)

    expected = {
        "G_content_category": "content_category",
        "G_content_category.n_periods": "content_category.n_periods",
        "G_content_category.start_offset": "content_category.start_offset",
        "G_content_category.time_labels": "content_category.time_labels",
        "G_main_activity": "main_activity",
        "G_main_activity.categories": "main_activity.categories",
        "G_main_activity.categories.anomalies": "main_activity.categories.anomalies",
        "G_main_activity.categories.anomalies.index": "main_activity.categories.anomalies.index",
        "G_main_activity.categories.anomalies.mean": "main_activity.categories.anomalies.mean",
    }
    assert changes == expected, f"Wrong changes dict:\n  got:      {changes}\n  expected: {expected}"


# ── conflict detection ────────────────────────────────────────────────────────

def test_parquet_no_conflict():
    cols = ["T_hello", "G_world"]
    assert detect_parquet_conflicts(cols, {}, STRIP_PREFIXES) == []


def test_parquet_conflict_two_prefixes():
    """T_hello and D_hello both strip to hello."""
    cols = ["T_hello", "D_hello", "G_other"]
    conflicts = detect_parquet_conflicts(cols, {}, STRIP_PREFIXES)
    assert len(conflicts) == 1
    assert conflicts[0]["new_name"] == "hello"
    assert set(conflicts[0]["originals"]) == {"T_hello", "D_hello"}


def test_parquet_conflict_via_rename_map():
    """D_watch_duration → play_duration, and there's already a T_play_duration."""
    cols = ["D_watch_duration", "T_play_duration"]
    conflicts = detect_parquet_conflicts(cols, {"D_watch_duration": "play_duration"}, STRIP_PREFIXES)
    assert len(conflicts) == 1
    assert conflicts[0]["new_name"] == "play_duration"
    assert set(conflicts[0]["originals"]) == {"D_watch_duration", "T_play_duration"}


def test_json_no_conflict():
    data = {"T_hello": 1, "G_world": 2}
    assert detect_json_conflicts(data, {}, STRIP_PREFIXES) == []


def test_json_conflict_top_level():
    """T_hello and D_hello at root both strip to hello."""
    data = {"T_hello": 1, "D_hello": 2, "G_other": 3}
    conflicts = detect_json_conflicts(data, {}, STRIP_PREFIXES)
    assert len(conflicts) == 1
    assert conflicts[0]["new_name"] == "hello"
    assert set(conflicts[0]["originals"]) == {"T_hello", "D_hello"}
    assert conflicts[0]["path"] == ""


def test_json_conflict_nested():
    """Conflict inside a nested dict is reported with the correct dot-path."""
    data = {"outer": {"T_hello": 1, "D_hello": 2}}
    conflicts = detect_json_conflicts(data, {}, STRIP_PREFIXES)
    assert len(conflicts) == 1
    assert conflicts[0]["path"] == "outer"
    assert conflicts[0]["new_name"] == "hello"


def test_json_conflict_in_list_items():
    """Each list item is a dict with conflicting keys."""
    data = {"items": [{"T_x": 1, "D_x": 2}, {"G_y": 3}]}
    conflicts = detect_json_conflicts(data, {}, STRIP_PREFIXES)
    assert len(conflicts) == 1
    assert conflicts[0]["new_name"] == "x"


def test_rename_json_file_keys_reports_conflicts():
    """rename_json_file_keys includes conflicts in its return value."""
    data = {"T_hello": 1, "D_hello": 2, "G_ok": 3}
    with tempfile.TemporaryDirectory() as tmp:
        f = _write_json(data, Path(tmp))
        result = rename_json_file_keys(f, RENAME_MAP, STRIP_PREFIXES, dry_run=True)
    assert result["conflicts"], "expected at least one conflict"
    assert result["conflicts"][0]["new_name"] == "hello"


def test_no_conflict_when_keys_are_distinct():
    data = {
        "G_content_category": {"n_periods": 4},
        "G_main_activity": {"categories": []},
        "desc_hashtags": [],
    }
    with tempfile.TemporaryDirectory() as tmp:
        f = _write_json(data, Path(tmp))
        result = rename_json_file_keys(f, RENAME_MAP, STRIP_PREFIXES, dry_run=True)
    assert result["conflicts"] == []


# ── runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_apply_rename_strip_prefix,
        test_apply_rename_dict_wins_over_strip,
        test_extract_keys_flat,
        test_extract_keys_nested,
        test_recursive_rename_strips_prefix,
        test_recursive_rename_dict_map,
        test_changes_reporting_does_not_cross_pair_keys,
        test_changes_reporting_full_example,
        test_parquet_no_conflict,
        test_parquet_conflict_two_prefixes,
        test_parquet_conflict_via_rename_map,
        test_json_no_conflict,
        test_json_conflict_top_level,
        test_json_conflict_nested,
        test_json_conflict_in_list_items,
        test_rename_json_file_keys_reports_conflicts,
        test_no_conflict_when_keys_are_distinct,
    ]

    passed = failed = 0
    for t in tests:
        try:
            print(f"  running {t.__name__} ...", end=" ")
            t()
            print("PASS")
            passed += 1
        except AssertionError as e:
            print(f"FAIL\n    {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
