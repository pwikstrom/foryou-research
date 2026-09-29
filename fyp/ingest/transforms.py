"""Row-level transforms applied while ingesting activity: time zones, local-time features, sessions and play durations.

Donor time-zone parsing and per-row UTC offsets, the weekday / day-segment
features, session ids (a new session after a 900 s gap by default), and
each play's duration derived from the next event (capped), with the
engagement token folded into the play row.
"""

import re
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

from fyp.core.activity_vocabulary import ACTIVITY_TYPE_MAP, RECEIVED_ACTIVITY_TYPES
from fyp.core.runtime import cf as _cf

WEEKDAY_MAPPER = {
    1: "monday",
    2: "tuesday",
    3: "wednesday",
    4: "thursday",
    5: "friday",
    6: "saturday",
    7: "sunday",
}

# Scratch column carrying the per-file donor timezone from the ingestion manifest
# (an IANA name like "Asia/Kolkata" or a fixed "+05:30" offset). Stamped in
# load_raw, consumed by the timezone resolvers, dropped by process()'s filter.
MANIFEST_TZ_COLUMN = "manifest_tz"

_FIXED_OFFSET_RE = re.compile(r"^([+-])(\d{1,2})(?::?(\d{2}))?$")


def infer_timezone_offset(timestamps: pd.Series) -> float:
    """
    Infers timezone offset by finding the 4-hour window with minimum activity.
    Assumes this quietest window centers around 03:00 local time.

    Args:
        timestamps: Series of UTC timestamps

    Returns:
        Offset in hours (float) from UTC. e.g. +10.0 for Brisbane.
    """
    if len(timestamps) < 10:
        return 0.0  # Not enough data to infer

    # Create a DataFrame to aggregate by hour
    df_ts = pd.DataFrame({"ts": timestamps})
    df_ts["hour"] = df_ts["ts"].dt.hour

    # Count activity per UTC hour (0-23)
    hourly_counts = df_ts.groupby("hour").size().reindex(range(24), fill_value=0)

    # We want a rolling 4-hour window sum.
    # To handle wrap-around (e.g. 23:00 -> 02:00), we concat the counts
    hourly_counts_ext = pd.concat([hourly_counts, hourly_counts.iloc[:3]], ignore_index=True)

    # Calculate rolling sum
    rolling_sum = hourly_counts_ext.rolling(window=4).sum()

    # The result has length 24 + 3 = 27. Indices 0-2 are NaN (window size 4);
    # index 3 covers hours [0,1,2,3] and index 26 covers [23,0,1,2].
    # Keep the 24 valid windows (indices 3..26), one per start hour 0..23.
    valid_sums = rolling_sum.iloc[3:].reset_index(drop=True)
    # Index k of valid_sums sums hours [k, k+1, k+2, k+3] (mod 24).

    min_val = valid_sums.min()
    min_indices = valid_sums[valid_sums == min_val].index.tolist()

    # Calculate circular mean of these indices
    # Convert hours (indices) to angles, mean vector, convert back
    angles = [2 * np.pi * idx / 24.0 for idx in min_indices]
    y = np.sum(np.sin(angles))
    x = np.sum(np.cos(angles))
    avg_angle = np.arctan2(y, x)
    avg_idx = avg_angle * 24.0 / (2 * np.pi)

    if avg_idx < 0:
        avg_idx += 24

    # avg_idx is the start hour k of the window. Its center is taken as k + 2.0
    # (e.g. window [2,3,4,5] -> center 4.0), and assumed to be 03:00 local.

    center_utc = avg_idx + 2.0
    if center_utc >= 24:
        center_utc -= 24

    # Offset = Local - UTC = 3.0 - Center
    offset = 3.0 - center_utc

    # Normalize to [-9, 15] to handle the date-line wrap (e.g. -11 maps to +13).
    # The range covers West Coast US (-8) to NZ (+12/13).
    while offset < -9:
        offset += 24
    while offset > 15:
        offset -= 24

    return round(offset)  # nearest hour: the inference is only a rough guess


def parse_donor_timezone(tz_str: str | None):
    """Parse a manifest timezone string to a ``tzinfo``, or ``None`` if unusable.

    Accepts an IANA zone name (``"Australia/Brisbane"``, ``"Asia/Kolkata"`` —
    preferred, since it carries DST history) or a fixed UTC offset
    (``"+05:30"``, ``"-8"``, ``"+1000"``).

    Args:
        tz_str: The manifest timezone value.

    Returns:
        A ``ZoneInfo`` or fixed-offset ``timezone``, or ``None`` when the value
        is empty or not a recognisable timezone.
    """
    if not tz_str or not isinstance(tz_str, str):
        return None
    tz_str = tz_str.strip()
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, ValueError):
        pass
    match = _FIXED_OFFSET_RE.match(tz_str)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    return None


def zone_offset_hours(utc_timestamps: pd.Series, tz) -> pd.Series:
    """Return the per-row UTC offset in hours of ``tz`` at each UTC instant.

    Vectorised and DST-correct: converts the tz-aware UTC series into ``tz`` and
    measures the wall-clock difference. ``NaT`` rows stay ``NaN``.
    """
    converted = utc_timestamps.dt.tz_convert(tz)
    return (converted.dt.tz_localize(None) - utc_timestamps.dt.tz_localize(None)) / pd.Timedelta(
        hours=1
    )


def _first_manifest_tz(df: pd.DataFrame):
    """Return the parsed manifest timezone for a per-file frame, or ``None``.

    A frame handled by ``process_single`` holds one raw file's rows, so the
    manifest timezone is constant; the first non-null value is taken.
    """
    if MANIFEST_TZ_COLUMN not in df.columns:
        return None
    values = df[MANIFEST_TZ_COLUMN].dropna()
    if len(values) == 0:
        return None
    return parse_donor_timezone(str(values.iloc[0]))


def _day_segment_from_hour(hour: int) -> str:
    if not (0 <= hour <= 23):
        raise ValueError(f"hour must be in 0..23, got {hour}")
    if hour <= 5:
        return "night"
    if hour <= 11:
        return "morning"
    if hour <= 17:
        return "afternoon"
    return "evening"


def assign_session_ids(df: pd.DataFrame, gap_threshold_s: int | None = None) -> pd.DataFrame:
    """Assign a persistent, globally-unique ``session_id`` to every activity.

    A *session* (a "phone sitting") is a maximal run of one collection's
    activities separated by gaps no larger than ``gap_threshold_s``. Every
    row of the donor's own activity gets the id of the sitting it belongs
    to, formatted as ``"{collection_id}__{n}"`` so ids are unique across
    collections. Rows of ``RECEIVED_ACTIVITY_TYPES`` (another account
    following the donor) are not the donor's activity: they neither open,
    extend nor join sittings, and their ``session_id`` is null. A replay of
    the TikTok corpus found them joining 156 otherwise separate sittings.

    Sessions are assigned to every donor row, viewing or not, so a login or
    a like from before the watch history begins forms a session of its
    own; counts of sessions belong on viewing rows.

    This is deliberately distinct from the transient, per-raw-file 180s grouping
    used inside ``TikTokDDPCollection.process_single`` for comment item_id
    backfill — that one is dropped immediately and never persisted. This
    session_id is computed on the full per-collection sequence (after migration,
    before any study sampling) and is meant to be used anywhere downstream.

    Args:
        df: Activity dataframe with ``collection_id`` and ``utc_timestamp``.
        gap_threshold_s: Maximum within-sitting gap in seconds. ``None`` reads
            ``[sessions] session_gap_s`` from the config (default 900 = 15 min).

    Returns:
        The same dataframe with a ``session_id`` column added; original row
        order is preserved.
    """
    if gap_threshold_s is None:
        gap_threshold_s = int(_cf().get("sessions", {}).get("session_gap_s", 900))
    if df.empty:
        df["session_id"] = pd.Series(dtype="string[pyarrow]")
        return df

    if "activity_type" in df.columns:
        received = df["activity_type"].astype("string").isin(RECEIVED_ACTIVITY_TYPES).fillna(False)
        donor = df[~received.to_numpy()]
    else:
        donor = df
    order = donor.sort_values(["collection_id", "utc_timestamp"], kind="mergesort").index
    ordered = donor.loc[order]
    gap = ordered.groupby("collection_id")["utc_timestamp"].diff().dt.total_seconds()
    session_break = gap.isna() | (gap > gap_threshold_s)
    session_num = session_break.groupby(ordered["collection_id"]).cumsum().astype("int64")
    session_id = ordered["collection_id"].astype(str) + "__" + session_num.astype(str)

    df["session_id"] = session_id.reindex(df.index)
    df["session_id"] = df["session_id"].convert_dtypes(dtype_backend="pyarrow")
    return df


def _engagement_token(atype: str, edata) -> str:
    """Build one folded ``extra_data`` token: ``"<atype>"`` or ``"<atype>:context"``."""
    if edata is not pd.NA and pd.notna(edata):
        edata_clean = re.sub(r"[\s,]+", " ", str(edata)).strip()
        if edata_clean:
            return f"{atype}:{edata_clean}"
    return str(atype)


def derive_play_duration(df: pd.DataFrame, cap_seconds: int = 600) -> pd.DataFrame:
    """Derive per-play dwell time from forward time-deltas between activities.

    ``play_duration`` is assigned only to ``play`` activities: the time elapsed
    until the *next* recorded event serves as a proxy for how long the user
    spent on the item. When a play is directly followed by other activities on
    the same ``item_id`` (e.g. a fave or comment on the same video), those
    deltas represent time spent on the same item and are attributed to the
    first play in the run; the non-lead activity types are folded into the lead
    play's ``extra_data``. Non-play activities always get NA, as does the last
    activity of the frame (no forward delta) and anything above ``cap_seconds``.

    Engagement activities (fave/comment/share/follow/save) that are *not*
    chronologically adjacent to a play of the same item still get linked: their
    token is folded into the nearest-in-time play row with the same ``item_id``
    anywhere in the frame. This matters on platforms whose exports log a view
    only once per item (e.g. Instagram's ``videos_watched``), so a later like
    of that item can be days away from its logged play. Only ``extra_data`` is
    affected — ``play_duration`` stays a strictly adjacency-based measure.

    Engagement from before the frame's first play is left unlinked by the
    fallback. An export's like and bookmark lists reach years further back
    than its watch history, so the viewing such an engagement belongs to is
    not in the data, and its nearest play of the same item is a later
    re-watch: in a replay of the TikTok corpus, 1,946 of the 2,004 bookmarks
    whose nearest play lay a day or more away were of this kind.

    Every play that received a folded token says how: ``link_method`` is
    ``"adjacent"``, ``"nearest_play"``, or ``"adjacent,nearest_play"`` when
    both folds contributed. Plays with no engagement keep the column null, and
    a value a platform parser wrote earlier (TikTok's ``"ffill_180s"`` on a
    comment row) is preserved. The inference is documented in the activity
    contract; this column is what lets an analysis exclude inferred links.

    Args:
        df: A single-donor activity frame in chronological order with
            ``utc_timestamp``, ``activity_type`` and ``item_id`` columns
            (every platform's ``process_single`` frame qualifies).
        cap_seconds: Durations above this are considered idle time → NA.

    Returns:
        The same frame with a ``play_duration`` [int64[pyarrow]] column added.
    """
    df = df.reset_index(drop=True)
    if "extra_data" not in df.columns:
        df["extra_data"] = pd.NA
    elif isinstance(df["extra_data"].dtype, pd.ArrowDtype) and df["extra_data"].isna().all():
        # An all-NA pyarrow column may carry the null type, which rejects the
        # string tokens the folds below write into it.
        df["extra_data"] = df["extra_data"].astype("string[pyarrow]")

    if "link_method" not in df.columns:
        df["link_method"] = pd.array([pd.NA] * len(df), dtype="string[pyarrow]")
    elif isinstance(df["link_method"].dtype, pd.ArrowDtype) and df["link_method"].isna().all():
        df["link_method"] = df["link_method"].astype("string[pyarrow]")

    if df.empty:
        df["play_duration"] = pd.Series([], dtype="int64[pyarrow]")
        return df

    # Which fold(s) put a token on each lead play; written to link_method at the end.
    link_methods: dict[int, list[str]] = {}

    # 1. Forward delta on the full frame: for each row, the time until the *next*
    # event. This is the correct attribution of dwell time to an activity.
    delta = df["utc_timestamp"].diff().dt.total_seconds()
    forward_delta = delta.shift(-1)

    # Default assignment: play activities get forward_delta, everything else gets NA.
    df["play_duration"] = forward_delta.where(df["activity_type"] == "play")

    # 2. Detect consecutive same-item_id runs of length > 1. A row is a non-first member
    # of a run when its item_id equals the previous row's item_id (and item_id is not null).
    # Such runs are vanishingly rare (~1/10,000 activities are non-play), so we iterate.
    is_continuation = df["item_id"].notna() & (df["item_id"] == df["item_id"].shift(1))

    # Non-lead rows whose token was folded into an adjacent lead play. Rows in
    # here are excluded from the same-item fallback fold below.
    folded_rows: set[int] = set()

    if is_continuation.any():
        # Walk each continuation backward to find the full run, then aggregate.
        continuation_idxs = df.index[is_continuation].tolist()
        visited: set[int] = set()
        for idx in continuation_idxs:
            if idx in visited:
                continue
            # Find the start of this run by walking back
            run_item = df.at[idx, "item_id"]
            run_start = idx
            while (
                run_start - 1 in df.index
                and pd.notna(df.at[run_start - 1, "item_id"])
                and df.at[run_start - 1, "item_id"] == run_item
            ):
                run_start -= 1
            # Find the end of the run by walking forward
            run_end = idx
            while (
                run_end + 1 in df.index
                and pd.notna(df.at[run_end + 1, "item_id"])
                and df.at[run_end + 1, "item_id"] == run_item
            ):
                run_end += 1
            run_slice = list(range(run_start, run_end + 1))
            visited.update(run_slice)

            # Find the first play activity in the run
            play_rows = [
                i
                for i in run_slice
                if df.at[i, "activity_type"] is not pd.NA and df.at[i, "activity_type"] == "play"
            ]
            if not play_rows:
                df.loc[run_slice, "play_duration"] = pd.NA
                continue

            # Sum forward_delta across all rows in the run using the full-df precomputed
            # series, so the last row's contribution (gap to the row after the run) is
            # correctly included — slicing before shifting would lose it.
            first_play = play_rows[0]
            total_delta = forward_delta.loc[run_slice].sum()
            df.loc[run_slice, "play_duration"] = pd.NA
            df.at[first_play, "play_duration"] = total_delta

            # Record the activity types of the non-lead rows in the run on the lead play's
            # extra_data column, as a comma-separated string (e.g. "fave" or "fave,comment").
            other_parts = []
            for i in run_slice:
                if i == first_play:
                    continue
                atype = df.at[i, "activity_type"]
                if atype is pd.NA:
                    continue
                other_parts.append(_engagement_token(atype, df.at[i, "extra_data"]))
                folded_rows.add(i)
            if other_parts:
                df.at[first_play, "extra_data"] = ",".join(other_parts)
                link_methods.setdefault(first_play, []).append("adjacent")

    # 3. Same-item fallback fold: engagement rows that did not fold via
    # adjacency but whose item was played somewhere in the frame get their
    # token appended to the nearest-in-time play of that item.
    is_engagement = df["activity_type"].isin(list(ACTIVITY_TYPE_MAP.keys()))
    is_play = df["activity_type"] == "play"
    first_play_ts = df.loc[is_play.fillna(False), "utc_timestamp"].min() if is_play.any() else None
    in_window = (
        (df["utc_timestamp"] >= first_play_ts).fillna(False)
        if first_play_ts is not None
        else pd.Series(False, index=df.index)
    )
    pending = df.index[
        is_engagement & df["item_id"].notna() & in_window & ~df.index.isin(list(folded_rows))
    ]
    if len(pending) > 0:
        plays = df.loc[is_play & df["item_id"].notna(), "item_id"]
        play_rows_by_item = {k: list(v) for k, v in plays.groupby(plays).groups.items()}
        for i in pending:
            candidates = play_rows_by_item.get(df.at[i, "item_id"], [])
            if not candidates:
                continue
            ts = df.at[i, "utc_timestamp"]
            target = min(candidates, key=lambda p: abs(df.at[p, "utc_timestamp"] - ts))
            token = _engagement_token(df.at[i, "activity_type"], df.at[i, "extra_data"])
            existing = df.at[target, "extra_data"]
            if existing is not pd.NA and pd.notna(existing):
                df.at[target, "extra_data"] = f"{existing},{token}"
            else:
                df.at[target, "extra_data"] = token
            methods = link_methods.setdefault(target, [])
            if "nearest_play" not in methods:
                methods.append("nearest_play")

    # 4. Record on each lead play which fold(s) linked engagement to it. A
    # value the platform parser wrote before the fold (TikTok's ffill_180s on
    # comment rows) sits on non-play rows and is left untouched.
    for idx, methods in link_methods.items():
        existing = df.at[idx, "link_method"]
        parts = [] if existing is pd.NA or pd.isna(existing) else str(existing).split(",")
        for m in methods:
            if m not in parts:
                parts.append(m)
        df.at[idx, "link_method"] = ",".join(parts)

    # 5. Cap play_duration at cap_seconds and cast to the project dtype.
    df["play_duration"] = (
        df["play_duration"]
        .map(lambda x: x if pd.notna(x) and x <= cap_seconds else pd.NA)
        .astype("int64[pyarrow]")
    )

    return df
