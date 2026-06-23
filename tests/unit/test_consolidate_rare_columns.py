"""Unit tests for consolidate_rare_columns_from_gemini_output.

Pins the similarity-guarded rare-column merge in
``fyp/machine_annotation.py``. Genuine stray-key variants (similar names) are
still merged back into their dominant column, but rare REAL columns are never
merged into an unrelated dominant (e.g. item_id) just because a batch is mostly
failed — the bug that previously collapsed such batches to item_id alone and
silently destroyed every good annotation.

No Gemini API calls — operates on small hand-built DataFrames. Instant and free.

Usage:
    python tests/unit/test_consolidate_rare_columns.py
Exit 0 iff all checks pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

import fyp.machine_annotation as ma


def _mostly_failed_batch(n_total: int = 100, n_good: int = 5) -> pd.DataFrame:
    """item_id populated for all rows; real columns only for the first n_good."""
    rows = []
    for i in range(n_total):
        good = i < n_good
        rows.append({
            "item_id": f"id_{i}",
            "type_of_story": "descriptive" if good else None,
            "objects": "['cat']" if good else None,
            "main_gender": "female" if good else None,
        })
    return pd.DataFrame(rows)


def test_mostly_failed_batch_preserves_real_columns():
    df = _mostly_failed_batch(n_total=100, n_good=5)
    out = ma.consolidate_rare_columns_from_gemini_output(df)
    # The bug collapsed this to just item_id. The fix must keep the real columns
    # AND their 5 good values.
    for col in ("type_of_story", "objects", "main_gender"):
        assert col in out.columns, f"{col} was dropped (regression)"
        assert out[col].notna().sum() == 5, f"{col} lost good values"
    assert out["item_id"].notna().sum() == 100


def test_similar_stray_key_is_consumed_but_dissimilar_is_retained():
    """The guard merges a similar-named stray key but spares a dissimilar column.

    Both rare columns are <10% populated. ``type_of_stroy`` (a misspelling, very
    similar to the dominant ``type_of_story``) is folded in and dropped, while
    ``objects`` (dissimilar to any dominant) is preserved — the exact distinction
    the similarity guard exists to make.
    """
    rows = []
    for i in range(100):
        row = {"item_id": f"id_{i}", "type_of_story": "descriptive"}
        if i < 3:
            row["type_of_stroy"] = "human-interest"   # similar -> should merge
            row["objects"] = "['cat']"                 # dissimilar -> should stay
        rows.append(row)
    df = pd.DataFrame(rows)
    out = ma.consolidate_rare_columns_from_gemini_output(df)
    assert "type_of_stroy" not in out.columns, "similar stray key was not merged"
    assert "objects" in out.columns, "dissimilar rare column was wrongly dropped"
    assert out["objects"].notna().sum() == 3, "dissimilar rare values lost"


def test_similarity_threshold_separates_real_from_unrelated():
    """The helper cleanly separates stray-variants from unrelated column names."""
    from fyp import utils as fyp_utils

    _, bad = fyp_utils.best_similarity_match("type_of_story", ["item_id"])
    _, good = fyp_utils.best_similarity_match("type_of_stroy", ["type_of_story"])
    assert bad < ma.RARE_COLUMN_MERGE_MIN_SIMILARITY <= good, (bad, good)
    assert fyp_utils.best_similarity_match("x", []) == (None, 0.0)


def test_normal_batch_unchanged():
    """A healthy batch (all columns well populated) is returned intact."""
    df = pd.DataFrame({
        "item_id": [f"id_{i}" for i in range(50)],
        "type_of_story": ["descriptive"] * 50,
        "objects": ["['cat']"] * 50,
    })
    out = ma.consolidate_rare_columns_from_gemini_output(df)
    assert set(out.columns) == {"item_id", "type_of_story", "objects"}
    assert len(out) == 50


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception:
            failures += 1
            import traceback

            print(f"ERROR {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
