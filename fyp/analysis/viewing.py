"""Per-play viewing measures shared by the datasets build and analyses.

Pure numpy/pandas (no config or storage access), so analysis code outside the
Hub imports the same definitions the Hub uses.
"""

import numpy as np
import pandas as pd


def completion(
    viewing_s,
    length_s,
    cap: float = 1.0,
    min_length_s: float | None = None,
) -> pd.Series:
    """Viewing time divided by the video's length, capped at ``cap``.

    A value above 1 means the video looped. The Hub's ``completion_rate``
    uses ``cap=1`` (looping is reported separately as ``rewatched``);
    analyses that need looping keep it with a higher cap (e.g. 3: a few very
    short videos left on screen for minutes otherwise reach ratios above 50
    and dominate means).

    Args:
        viewing_s: Viewing time in seconds (array-like; missing allowed).
        length_s: Video length in seconds (array-like, aligned).
        cap: Upper bound applied to the ratio. Values below 0 become 0.
        min_length_s: When given, lengths below it give a missing value
            instead of a ratio (very short or zero lengths are unreliable).

    Returns:
        A float Series (missing where either input is missing).
    """
    v = pd.to_numeric(pd.Series(viewing_s), errors="coerce").astype("float64")
    n = pd.to_numeric(pd.Series(length_s), errors="coerce").astype("float64")
    v.index = n.index
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = v / n
    if min_length_s is not None:
        ratio = ratio.where(n >= min_length_s)
    return ratio.clip(lower=0, upper=cap)
