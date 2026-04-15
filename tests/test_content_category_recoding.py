"""Sanity check for content_category / type_of_story recoding.

Before the fix in clean_up_machine_annotations, values from Gemini ended up as
'other category' in the refined parquet because:
  1. `" | "`-joined values were split on "|" and the leading/trailing spaces were
     never stripped in recode_stringified_list.
  2. The fuzzy-match step ran only on the exploded valid_items, while the final
     _fast_replace iterated the ORIGINAL series, so any value that needed fuzzy
     matching failed the keep_set membership check and was collapsed to
     OTHER_THINGS.

This script feeds a synthetic dataframe through clean_up_machine_annotations and
asserts that:
  - All valid category values survive (no surprise 'other category').
  - NA values in type_of_story remain NA (so annotated_ok / annotated_fail still
    work downstream).
  - The 'Technology & Design & Reviews' category (which had a prompt/schema
    mismatch) also survives now that the prompt was aligned with the schema.

Run with:
    source .fypenv314/bin/activate
    python tests/test_content_category_recoding.py
"""

import sys
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))

import numpy as np
import pandas as pd

from fyp import fyp_config
fyp_config.initialize()
from fyp.fyp_config import fyp_cf
from fyp.machine_annotation import clean_up_machine_annotations


OTHER = fyp_cf["labels"]["OTHER_THINGS"]


def _flatten(values):
    """Flatten a list-of-lists (with possible NAs) to a list of strings."""
    out = []
    for v in values:
        if isinstance(v, (list, np.ndarray)):
            out.extend([x for x in v if isinstance(x, str)])
        elif isinstance(v, str):
            out.append(v)
    return out


def _assert_no_other(values, label):
    leaked = [v for v in values if v == OTHER]
    if leaked:
        raise AssertionError(
            f"[{label}] {len(leaked)} value(s) were collapsed to '{OTHER}' "
            f"(expected zero). Sample: {leaked[:5]}"
        )


def main() -> None:
    # Post-recode_stringified_list state. Note the leading/trailing spaces on
    # the content_category entries — that is exactly what " | ".split("|") used
    # to produce before the fix in recode_variables.py. The fix strips them,
    # but the test deliberately keeps a few here to verify that the downstream
    # fuzzy-match + consolidation step is also robust.
    df = pd.DataFrame(
        {
            "item_id": ["v1", "v2", "v3", "v4", "v5", "v6", "v7"],
            "content_category": [
                ["comedy", " travel"],
                ["film  and  tv"],
                ["sports"],
                ["technology  and  design  and  reviews"],
                ["comedy ", " fashion  and  beauty"],
                ["interpersonal relationships"],
                ["anime  and  comics", "games"],
            ],
            "type_of_story": [
                "issue-based",
                "event-based",
                "human-interest",
                "descriptive",
                pd.NA,
                "issue-based",
                "event-based",
            ],
        }
    )

    cleaned = clean_up_machine_annotations(df, verbose=True)

    cc_values = _flatten(cleaned["content_category"].tolist())
    ts_values = cleaned["type_of_story"].tolist()

    print("\n--- content_category after cleanup ---")
    for row in cleaned["content_category"].tolist():
        print(f"  {row}")
    print("\n--- type_of_story after cleanup ---")
    for row in ts_values:
        print(f"  {row}")

    # 1. No content_category value should have collapsed to 'other category'.
    _assert_no_other(cc_values, "content_category")

    # 2. Every type_of_story value should either match an accepted label or
    #    stay NA. It should never become 'other category'.
    ts_non_na = [v for v in ts_values if not (isinstance(v, float) and np.isnan(v)) and not pd.isna(v)]
    _assert_no_other(ts_non_na, "type_of_story")

    # 3. The NA in type_of_story must survive so annotated_ok / annotated_fail
    #    detection still works.
    if not pd.isna(ts_values[4]):
        raise AssertionError(
            f"type_of_story[4] should be NA after cleanup, got: {ts_values[4]!r}"
        )

    # 4. Every canonical category from the prompt should round-trip.
    expected_canonical = {
        "comedy",
        "travel",
        "film  and  tv",
        "sports",
        "technology  and  design  and  reviews",
        "fashion  and  beauty",
        "interpersonal relationships",
        "anime  and  comics",
        "games",
    }
    missing = expected_canonical - set(cc_values)
    if missing:
        raise AssertionError(
            f"Missing canonical categories after cleanup: {sorted(missing)}. "
            f"Got: {sorted(set(cc_values))}"
        )

    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()
