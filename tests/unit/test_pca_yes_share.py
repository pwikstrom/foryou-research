"""Unit tests for the yes/no(/unclear) PCA bypass (fyp/analysis/pca.py).

Yes/No/Unclear categoricals emit a plain per-group share-of-"yes" column
(``{var}_share_of_feed``) instead of PCA components + entropy. These tests pin
the two pure helpers:

  * ``is_yes_no_counts`` — detection on the post-crosstab counts frame:
    case-insensitive, sentinel-tolerant, and False for real multi-category
    variables or sentinel-only frames;
  * ``yes_share_from_counts`` — share math: sentinels excluded from numerator
    AND denominator, missing "yes" column → 0.0, zero denominator → NaN.

Synthetic frames only — no config, no files.

Usage:
    pytest tests/unit/test_pca_yes_share.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd
import pytest

from fyp.analysis import pca as fpca






def _counts(data: dict, index=None) -> pd.DataFrame:
    if index is None:
        index = pd.MultiIndex.from_tuples(
            [("coll_a", "2026-01-01"), ("coll_a", "2026-01-02"),
             ("coll_b", "2026-01-01")],
            names=["collection_id", "local_date"])
    return pd.DataFrame(data, index=index, dtype="float64")






def test_detection_true_cases():
    assert fpca.is_yes_no_counts(_counts({"yes": [1, 2, 3], "no": [4, 5, 6]}))
    assert fpca.is_yes_no_counts(
        _counts({"yes": [1, 0, 0], "no": [1, 1, 1], "unclear": [0, 2, 0]}))
    # Case variants (recode lowercases, but detection must not depend on it).
    assert fpca.is_yes_no_counts(_counts({"Yes": [1, 1, 1], "No": [2, 2, 2]}))
    # Subset: an all-"no" variable is still a yes/no variable.
    assert fpca.is_yes_no_counts(_counts({"no": [3, 3, 3]}))
    # Sentinel columns alongside are tolerated.
    assert fpca.is_yes_no_counts(
        _counts({"yes": [1, 1, 1], "no": [2, 0, 1], "not coded": [0, 1, 0],
                 "unable to detect": [1, 0, 0]}))






def test_detection_false_cases():
    # A real multi-category variable.
    assert not fpca.is_yes_no_counts(
        _counts({"comedy": [1, 2, 0], "news": [0, 1, 2], "sports": [1, 0, 0]}))
    # yes/no plus a substantive non-yes/no category.
    assert not fpca.is_yes_no_counts(
        _counts({"yes": [1, 1, 1], "no": [1, 1, 1], "maybe": [1, 0, 0]}))
    # Sentinel-only frames carry no substantive answers.
    assert not fpca.is_yes_no_counts(
        _counts({"not coded": [1, 1, 1], "unable to detect": [2, 0, 0]}))
    # Empty frame.
    assert not fpca.is_yes_no_counts(_counts({}))






def test_share_math_basic():
    counts = _counts({"yes": [1.0, 0.0, 3.0], "no": [3.0, 2.0, 0.0],
                      "unclear": [0.0, 2.0, 1.0]})
    share = fpca.yes_share_from_counts(counts)
    assert share.tolist() == pytest.approx([0.25, 0.0, 0.75])
    assert str(share.dtype) == "float64"
    assert (share.index == counts.index).all()






def test_share_excludes_sentinels_from_both_sides():
    counts = _counts({"yes": [1.0, 1.0, 1.0], "no": [1.0, 3.0, 0.0],
                      "not coded": [10.0, 10.0, 10.0]})
    share = fpca.yes_share_from_counts(counts)
    # Sentinels inflate neither numerator nor denominator.
    assert share.tolist() == pytest.approx([0.5, 0.25, 1.0])






def test_share_missing_yes_column_is_zero():
    counts = _counts({"no": [2.0, 1.0, 3.0], "unclear": [0.0, 1.0, 0.0]})
    share = fpca.yes_share_from_counts(counts)
    assert share.tolist() == pytest.approx([0.0, 0.0, 0.0])






def test_share_zero_denominator_is_nan():
    counts = _counts({"yes": [1.0, 0.0, 2.0], "no": [1.0, 0.0, 0.0],
                      "not coded": [0.0, 5.0, 0.0]})
    share = fpca.yes_share_from_counts(counts)
    assert share.iloc[0] == pytest.approx(0.5)
    assert np.isnan(share.iloc[1])  # only sentinel answers that day
    assert share.iloc[2] == pytest.approx(1.0)






def test_share_suffix_never_matches_component_regex():
    """The read-side component cap must treat share columns as non-PCA."""
    import re

    col = f"advertising{fpca.SHARE_OF_FEED_SUFFIX}"
    assert re.search(r"_C\d+$", col) is None
    assert col == "advertising_share_of_feed"
