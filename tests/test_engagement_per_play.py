"""Unit test for fyp.scrape._engagement_per_play and the consolidation ratio columns."""

import math

import pandas as pd

from fyp.scrape import _engagement_per_play, ENGAGEMENT_PER_PLAY_COLUMNS




def test_engagement_per_play() -> None:
    """Verify per-play division handles the -1 sentinel, zero plays, and dtype."""
    plays = pd.Series([1000, 0, -1, 500, 200], dtype="int64[pyarrow]")
    numerator = pd.Series([10, 5, 5, -1, 50], dtype="int64[pyarrow]")

    result = _engagement_per_play(numerator, plays)

    assert str(result.dtype) == "double[pyarrow]", f"unexpected dtype {result.dtype}"

    # Row 0: 10 / 1000 = 0.01 (normal)
    assert math.isclose(float(result.iloc[0]), 0.01), result.iloc[0]
    # Row 1: plays == 0 -> NA
    assert pd.isna(result.iloc[1]), "zero plays should be NA"
    # Row 2: plays == -1 (sentinel) -> NA
    assert pd.isna(result.iloc[2]), "sentinel plays should be NA"
    # Row 3: numerator == -1 (sentinel) -> NA even though plays valid
    assert pd.isna(result.iloc[3]), "sentinel numerator should be NA"
    # Row 4: 50 / 200 = 0.25 (normal)
    assert math.isclose(float(result.iloc[4]), 0.25), result.iloc[4]

    # All non-NA proportions must be in [0, 1] for these inputs.
    valid = result.dropna()
    assert ((valid >= 0) & (valid <= 1)).all(), f"out-of-range proportions: {valid.tolist()}"

    print("test_engagement_per_play PASSED")




def test_spec_mapping() -> None:
    """The derived columns map to the expected source counts."""
    assert ENGAGEMENT_PER_PLAY_COLUMNS == {
        "comments_per_play": "stats_commentCount",
        "faves_per_play": "stats_diggCount",
        "shares_per_play": "stats_shareCount",
        "saves_per_play": "stats_collectCount",
    }
    print("test_spec_mapping PASSED")




if __name__ == "__main__":
    test_engagement_per_play()
    test_spec_mapping()
    print("All tests passed.")
