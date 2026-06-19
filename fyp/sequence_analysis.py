"""Sequence-windowing analysis for feed data.

Treats each participant's feed as an ordered sequence of viewing events and asks
whether dwell behaviour (``play_duration``) in one window of the feed predicts
the *kind* of content served in a later window. The "kind" is any annotation
feature: content category, demographic representation (gender/ethnicity/age),
provenance (AI-generated/ad/trend), or virality/recency (engagement counts).

This module is the exploratory (Stage A) core: pure dataframe-in / artifact-out
functions with no I/O and no Flask coupling, mirroring ``fyp/timeline_analysis.py``.
The background worker (``web_interface/run_sequence_refresh.py``) loads study data,
calls into here, and persists the results.

Dwell signal (``play_duration``) is a forward-delta proxy available on TikTok
DDP/AIO donations only; Zeeschuimer ``observe`` rows carry none. Participants are
gated on dwell coverage so the analysis never silently mixes the two.

Target features are classified into two computational kinds:
  * ``share``  — multi-label lists, single-label categoricals, and Y/N
                 dichotomies all reduce to "fraction of the window's videos
                 carrying value V". Reported as lift (share ÷ baseline share).
  * ``scalar`` — numeric scores reduced to the window mean. Reported as
                 mean-by-dwell-bin.
"""

import math
from typing import Any

import numpy as np
import pandas as pd

# --- Tuning constants -----------------------------------------------------

# Viewing activity types. Mirrors the filter in organize_datasets.py:672.
VIEWING_ACTIVITY_TYPES = ("play", "observe")

# Session boundary (seconds). Matches the 180s rule used during ingest
# (ingest.py:1323), exposed here so window/session definition stays tunable.
SESSION_GAP_S = 180

# Default number of consecutive viewing events per window.
DEFAULT_WINDOW_N = 10

# Horizons (windows ahead) computed into the summary grid. All stay within a
# session by construction (the join is keyed on session_id).
DEFAULT_HORIZONS = (1, 2, 3)

# Participant must have at least this fraction of viewing events carrying a
# non-null play_duration to be dwell-eligible (filters Zeeschuimer-heavy users).
MIN_DWELL_COVERAGE = 0.5

# Participant must yield at least this many full windows to be analysed.
MIN_WINDOWS = 4

# An aggregate lift/transition cell needs at least this many participants.
MIN_PARTICIPANTS_FOR_AGGREGATE = 3

# Per-participant dwell-bin labels, ordered low→high (own-distribution tertiles).
DWELL_BIN_LABELS = ("Short", "Medium", "Long")

# A share-target value must appear in at least this fraction of windowed videos
# to be carried, and at most this many values are kept per target (most
# prevalent first) — keeps high-cardinality targets (objects, hashtags) bounded.
MIN_VALUE_PREVALENCE = 0.005
TOP_K_TARGET_VALUES = 25

# Column-name scheme in the per-window frame.
SHARE_SEP = "::"
SHARE_PREFIX = "sh"
MEAN_PREFIX = "mn"
NEXT_SUFFIX = "__next"

# Columns that can never be prediction targets: the dwell predictor itself and
# anything derived from it (would leak the predictor into the target).
BARRED_TARGET_COLUMNS = frozenset({
    "play_duration", "completion_rate", "dwell_mean", "dwell_median",
    "dwell_p90", "completion_mean", "inter_event_gap_s",
})

# Curated default targets: (column, extract, family). ``extract`` is how a cell
# becomes value(s): "list" (multi-label), "single" (one categorical/Y-N value),
# or "numeric" (scalar mean). Studies missing a column simply skip it.
DEFAULT_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("main_gender", "single", "Representation & identity"),
    ("main_ethnicity", "single", "Representation & identity"),
    ("faces_age_estimate", "numeric", "Representation & identity"),
    ("content_category", "list", "Narrative, content & tone"),
    ("type_of_story", "single", "Narrative, content & tone"),
    ("objects", "list", "Narrative, content & tone"),
    ("political_score", "numeric", "Narrative, content & tone"),
    ("sensitivity_score", "numeric", "Narrative, content & tone"),
    ("aigc", "single", "Provenance & intent"),
    ("advertising", "single", "Provenance & intent"),
    ("trend", "single", "Provenance & intent"),
    ("tiktok_native", "single", "Provenance & intent"),
    ("australian_relevance", "single", "Provenance & intent"),
    ("stats_playCount", "numeric", "Virality & recency"),
    ("stats_diggCount", "numeric", "Virality & recency"),
    ("stats_shareCount", "numeric", "Virality & recency"),
    ("plays_per_day", "numeric", "Virality & recency"),
    ("days_since_created", "numeric", "Virality & recency"),
)





def _to_value_lists(series: pd.Series, extract: str) -> pd.Series:
    """Coerce a target column to an object Series of per-row value lists.

    Args:
        series: The raw target column.
        extract: "list" (multi-label) or "single" (one categorical/Y-N value).

    Returns:
        Object Series where each cell is a (possibly empty) list of lowercase
        string values. ``"unable to detect"`` is preserved as a real value;
        nulls/blanks become empty lists.
    """
    def _coerce(value: Any) -> list[str]:
        if value is None or value is pd.NA:
            return []
        if extract == "single":
            try:
                if pd.isna(value):
                    return []
            except (TypeError, ValueError):
                pass
            text = str(value).strip().lower()
            return [text] if text and text != "nan" else []
        # extract == "list"
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, (list, tuple, np.ndarray)):
            items = list(value)
        else:
            try:
                items = list(value)
            except TypeError:
                return []
        out: list[str] = []
        for item in items:
            if item is None:
                continue
            text = str(item).strip().lower()
            if text:
                out.append(text)
        return out

    return series.map(_coerce)





def classify_targets(
    df: pd.DataFrame, requested: list[str] | None = None
) -> list[dict[str, str]]:
    """Resolve the target specs available in a dataframe.

    Args:
        df: Recoded event dataframe.
        requested: Specific target column names to use; defaults to the curated
            :data:`DEFAULT_TARGETS` list. Barred (dwell-derived) columns and
            columns absent from ``df`` are dropped.

    Returns:
        List of specs ``{name, extract, kind, family}`` where ``kind`` is
        ``"scalar"`` (numeric → window mean) or ``"share"`` (else).
    """
    if requested is None:
        catalogue = DEFAULT_TARGETS
    else:
        wanted = set(requested)
        known = {c: (e, f) for c, e, f in DEFAULT_TARGETS}
        catalogue = tuple(
            (c, *known.get(c, ("single", "Custom"))) for c in requested if c in wanted
        )

    specs: list[dict[str, str]] = []
    for column, extract, family in catalogue:
        if column in BARRED_TARGET_COLUMNS or column not in df.columns:
            continue
        kind = "scalar" if extract == "numeric" else "share"
        specs.append({"name": column, "extract": extract, "kind": kind, "family": family})
    return specs





def add_sequence_index(df: pd.DataFrame, session_gap_s: int = SESSION_GAP_S) -> pd.DataFrame:
    """Add feed-position and session indices to a recoded event dataframe.

    Filters to viewing events, sorts each participant's events chronologically,
    and derives the ordering columns the windowing layer needs.

    Args:
        df: Recoded event dataframe with ``collection_id``, ``utc_timestamp``,
            ``activity_type``, ``play_duration``.
        session_gap_s: Inter-event gap (seconds) above which a new session starts.

    Returns:
        Viewing events only, sorted by ``(collection_id, utc_timestamp)``, with
        ``feed_position``, ``inter_event_gap_s``, ``session_id``,
        ``session_position`` added.
    """
    viewing = df[df["activity_type"].isin(VIEWING_ACTIVITY_TYPES)].copy()
    if viewing.empty:
        for col in ("feed_position", "inter_event_gap_s", "session_id", "session_position"):
            viewing[col] = pd.Series(dtype="float64")
        return viewing

    viewing["utc_timestamp"] = pd.to_datetime(viewing["utc_timestamp"])
    viewing.sort_values(["collection_id", "utc_timestamp"], kind="mergesort", inplace=True)
    viewing.reset_index(drop=True, inplace=True)

    grouped = viewing.groupby("collection_id", sort=False)
    viewing["feed_position"] = grouped.cumcount()

    gap = grouped["utc_timestamp"].diff().dt.total_seconds()
    viewing["inter_event_gap_s"] = gap

    session_break = gap.isna() | (gap > session_gap_s)
    viewing["session_id"] = session_break.groupby(viewing["collection_id"]).cumsum().astype("int64") - 1
    viewing["session_position"] = viewing.groupby(
        ["collection_id", "session_id"], sort=False
    ).cumcount()

    return viewing





def _build_target_vocabulary(value_lists: pd.Series) -> list[str]:
    """Return the most prevalent values for a share target (capped, floored)."""
    exploded = value_lists.explode().dropna()
    if exploded.empty:
        return []
    n_rows = len(value_lists)
    prevalence = exploded.value_counts() / n_rows
    kept = prevalence[prevalence >= MIN_VALUE_PREVALENCE]
    return kept.head(TOP_K_TARGET_VALUES).index.tolist()





def _share_col(target: str, value: str) -> str:
    safe = str(value).replace(SHARE_SEP, ":")
    return f"{SHARE_PREFIX}{SHARE_SEP}{target}{SHARE_SEP}{safe}"





def _mean_col(target: str) -> str:
    return f"{MEAN_PREFIX}{SHARE_SEP}{target}"





def build_windows(
    indexed: pd.DataFrame,
    target_specs: list[dict[str, str]],
    window_n: int = DEFAULT_WINDOW_N,
    target_index: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Aggregate sequenced events into fixed-N, session-bounded windows.

    A window is ``window_n`` consecutive viewing events within one session.
    Trailing partial windows are dropped. For each window the function emits
    dwell predictor features and, per target, either share columns (one per
    retained value) or a mean column.

    Args:
        indexed: Output of :func:`add_sequence_index`.
        target_specs: Output of :func:`classify_targets`.
        window_n: Events per window.
        target_index: Precomputed value vocabularies to reuse (so all studies /
            horizons share the same columns); built from the data when ``None``.

    Returns:
        ``(windows, target_index)``. ``windows`` has one row per
        ``(collection_id, session_id, window_idx)``. ``target_index`` maps each
        target name to ``{kind, family, values, value_columns, mean_column}``.
    """
    empty_index: dict[str, Any] = target_index or {}
    if indexed.empty:
        return indexed.iloc[0:0].copy(), empty_index

    work = indexed.copy()
    work["window_idx"] = (work["session_position"] // window_n).astype("int64")

    keys = ["collection_id", "session_id", "window_idx"]
    sizes = work.groupby(keys, sort=False).transform("size")
    work = work[sizes == window_n].copy()
    if work.empty:
        return work.iloc[0:0].copy(), empty_index

    # Dwell predictor features (always present).
    work["_dwell"] = work["play_duration"].astype("float64")
    work["_completion"] = (
        work["completion_rate"].astype("float64") if "completion_rate" in work.columns else np.nan
    )
    agg: dict[str, Any] = {
        "n_videos": ("_dwell", "size"),
        "dwell_mean": ("_dwell", "mean"),
        "dwell_median": ("_dwell", "median"),
        "dwell_p90": ("_dwell", lambda s: float(np.nanpercentile(s, 90)) if s.notna().any() else np.nan),
        "completion_mean": ("_completion", "mean"),
        "ts_start": ("utc_timestamp", "min"),
    }

    new_index: dict[str, Any] = {}
    for spec in target_specs:
        name = spec["name"]
        if spec["kind"] == "scalar":
            mcol = _mean_col(name)
            work[mcol] = pd.to_numeric(work[name], errors="coerce").astype("float64")
            agg[mcol] = (mcol, "mean")
            new_index[name] = {
                "kind": "scalar", "family": spec["family"],
                "mean_column": mcol, "values": [], "value_columns": [],
            }
        else:
            value_lists = _to_value_lists(work[name], spec["extract"])
            if target_index and name in target_index:
                values = target_index[name]["values"]
            else:
                values = _build_target_vocabulary(value_lists)
            value_columns = []
            for value in values:
                scol = _share_col(name, value)
                work[scol] = value_lists.map(lambda lst, v=value: 1.0 if v in lst else 0.0)
                agg[scol] = (scol, "mean")
                value_columns.append(scol)
            new_index[name] = {
                "kind": "share", "family": spec["family"],
                "values": values, "value_columns": value_columns, "mean_column": None,
            }

    windows = work.groupby(keys, sort=True).agg(**agg).reset_index()
    return windows, (target_index or new_index)





def assign_dwell_bins(windows: pd.DataFrame) -> pd.DataFrame:
    """Add a per-participant ``dwell_bin`` (Short/Medium/Long) column.

    Bins are tertiles of each participant's own per-window mean dwell, assigned
    by rank percentile so ties and small window counts never collapse the bins.
    """
    if windows.empty:
        windows["dwell_bin"] = pd.Series(dtype="object")
        return windows

    def _bin(values: pd.Series) -> pd.Series:
        ranks = values.rank(method="first", pct=True)
        return pd.cut(
            ranks, bins=[0.0, 1 / 3, 2 / 3, 1.0],
            labels=list(DWELL_BIN_LABELS), include_lowest=True,
        )

    windows = windows.copy()
    windows["dwell_bin"] = (
        windows.groupby("collection_id", sort=False)["dwell_mean"]
        .transform(lambda s: _bin(s).astype(object))
    )
    windows["dwell_bin"] = pd.Categorical(
        windows["dwell_bin"], categories=list(DWELL_BIN_LABELS), ordered=True
    )
    return windows





def compute_participant_eligibility(
    indexed: pd.DataFrame, windows: pd.DataFrame
) -> pd.DataFrame:
    """Summarise per-participant dwell coverage and window count.

    Returns one row per participant with ``n_viewing_events``,
    ``dwell_coverage``, ``n_windows``, and a boolean ``eligible`` flag.
    """
    cov = indexed.groupby("collection_id").agg(
        n_viewing_events=("play_duration", "size"),
        n_with_dwell=("play_duration", lambda s: int(s.notna().sum())),
    )
    cov["dwell_coverage"] = cov["n_with_dwell"] / cov["n_viewing_events"].replace(0, np.nan)

    if windows.empty:
        cov["n_windows"] = 0
    else:
        wcount = windows.groupby("collection_id").size().rename("n_windows")
        cov = cov.join(wcount, how="left")
        cov["n_windows"] = cov["n_windows"].fillna(0).astype("int64")

    cov["eligible"] = (cov["dwell_coverage"] >= MIN_DWELL_COVERAGE) & (
        cov["n_windows"] >= MIN_WINDOWS
    )
    return cov.reset_index()





def _join_horizon(windows: pd.DataFrame, horizon: int, value_cols: list[str]) -> pd.DataFrame:
    """Join each window k to the values of window k+horizon within the same session."""
    keys = ["collection_id", "session_id", "window_idx"]
    nxt = windows[keys + value_cols].copy()
    nxt["window_idx"] = nxt["window_idx"] - horizon
    nxt = nxt.rename(columns={c: f"{c}{NEXT_SUFFIX}" for c in value_cols})
    return windows.merge(nxt, on=keys, how="inner")





def compute_share_transition(
    windows: pd.DataFrame,
    value_columns: list[str],
    value_labels: list[str],
    horizon: int = 1,
) -> dict[str, Any]:
    """Compute dwell-bin → next-window share lift for one share target.

    For each participant, the mean next-window share of each value is computed
    within each current-window dwell bin, then averaged across participants
    (participant as the unit). **Lift** is the per-bin mean share divided by the
    overall (marginal) mean next-window share — >1 means that dwell level is
    followed by more of that value than baseline.

    Returns dict with ``lift``/``prob`` (bin→value→float), ``marginal``
    (value→float), and ``n_participants`` (bin→int).
    """
    bins = list(DWELL_BIN_LABELS)
    empty = {"values": value_labels, "bins": bins, "lift": {}, "prob": {},
             "marginal": {}, "n_participants": {}}
    if windows.empty or not value_columns:
        return empty

    joined = _join_horizon(windows, horizon, value_columns)
    if joined.empty:
        return empty

    next_cols = [f"{c}{NEXT_SUFFIX}" for c in value_columns]
    marginal = joined.groupby("collection_id")[next_cols].mean().mean()
    per_part_bin = joined.groupby(["collection_id", "dwell_bin"], observed=True)[next_cols].mean()
    bin_mean = per_part_bin.groupby("dwell_bin", observed=True).mean()
    bin_n = per_part_bin.groupby("dwell_bin", observed=True).size()

    lift: dict[str, dict[str, float | None]] = {}
    prob: dict[str, dict[str, float]] = {}
    n_part: dict[str, int] = {}
    for b in bins:
        if b not in bin_mean.index:
            continue
        n_contrib = int(bin_n.get(b, 0))
        n_part[b] = n_contrib
        lift[b], prob[b] = {}, {}
        for label, ncol in zip(value_labels, next_cols):
            base = float(marginal[ncol])
            value = float(bin_mean.loc[b, ncol])
            prob[b][label] = round(value, 4)
            if n_contrib < MIN_PARTICIPANTS_FOR_AGGREGATE or base <= 0 or math.isnan(value):
                lift[b][label] = None
            else:
                lift[b][label] = round(value / base, 3)

    return {
        "values": value_labels, "bins": bins, "lift": lift, "prob": prob,
        "marginal": {lab: round(float(marginal[nc]), 4) for lab, nc in zip(value_labels, next_cols)},
        "n_participants": n_part,
    }





def compute_scalar_transition(
    windows: pd.DataFrame, mean_column: str, horizon: int = 1
) -> dict[str, Any]:
    """Compute dwell-bin → next-window mean for one scalar target.

    Returns dict with ``by_bin`` (bin→mean), ``overall`` (float), and
    ``n_participants`` (bin→int).
    """
    bins = list(DWELL_BIN_LABELS)
    if windows.empty or mean_column not in windows.columns:
        return {"bins": bins, "by_bin": {}, "overall": None, "n_participants": {}}

    joined = _join_horizon(windows, horizon, [mean_column])
    ncol = f"{mean_column}{NEXT_SUFFIX}"
    per_part_bin = joined.groupby(["collection_id", "dwell_bin"], observed=True)[ncol].mean()
    bin_mean = per_part_bin.groupby("dwell_bin", observed=True).mean()
    bin_n = per_part_bin.groupby("dwell_bin", observed=True).size()
    overall = float(joined.groupby("collection_id")[ncol].mean().mean())

    by_bin: dict[str, float | None] = {}
    n_by_bin: dict[str, int] = {}
    for b in bins:
        if b not in bin_mean.index:
            continue
        n_contrib = int(bin_n.get(b, 0))
        value = float(bin_mean.loc[b])
        n_by_bin[b] = n_contrib
        by_bin[b] = (
            round(value, 3)
            if n_contrib >= MIN_PARTICIPANTS_FOR_AGGREGATE and not math.isnan(value)
            else None
        )
    return {
        "bins": bins, "by_bin": by_bin,
        "overall": round(overall, 3) if not math.isnan(overall) else None,
        "n_participants": n_by_bin,
    }





def prepare_window_table(
    df: pd.DataFrame,
    window_n: int = DEFAULT_WINDOW_N,
    session_gap_s: int = SESSION_GAP_S,
    requested_targets: list[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    """Run the full Stage-A pipeline up to the per-window table.

    Returns ``(windows, target_index, eligibility)``. ``windows`` is restricted
    to eligible participants and carries a ``dwell_bin`` column; ``eligibility``
    covers all participants.
    """
    indexed = add_sequence_index(df, session_gap_s=session_gap_s)
    target_specs = classify_targets(df, requested=requested_targets)
    windows, target_index = build_windows(indexed, target_specs, window_n=window_n)
    eligibility = compute_participant_eligibility(indexed, windows)

    eligible_ids = set(eligibility.loc[eligibility["eligible"], "collection_id"])
    windows = windows[windows["collection_id"].isin(eligible_ids)].copy()
    windows = assign_dwell_bins(windows)
    return windows, target_index, eligibility





def compute_summary(
    windows: pd.DataFrame,
    target_index: dict[str, Any],
    eligibility: pd.DataFrame,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    window_n: int = DEFAULT_WINDOW_N,
    session_gap_s: int = SESSION_GAP_S,
) -> dict[str, Any]:
    """Build the JSON-serialisable summary grid for a study.

    Produces, per horizon and per target, a share-lift block (share targets) or
    a scalar-transition block (scalar targets), plus metadata and the
    participant eligibility table.
    """
    n_eligible = int(eligibility["eligible"].sum()) if not eligibility.empty else 0
    targets_meta = {
        name: {"kind": spec["kind"], "family": spec["family"],
               "values": spec.get("values", [])}
        for name, spec in target_index.items()
    }
    summary: dict[str, Any] = {
        "metadata": {
            "window_n": window_n,
            "session_gap_s": session_gap_s,
            "horizons": list(horizons),
            "dwell_bins": list(DWELL_BIN_LABELS),
            "n_participants_total": int(len(eligibility)),
            "n_participants_eligible": n_eligible,
            "n_windows": int(len(windows)),
            "targets": targets_meta,
            "min_participants_for_aggregate": MIN_PARTICIPANTS_FOR_AGGREGATE,
        },
        "eligibility": eligibility.to_dict(orient="records"),
        "horizons": {},
    }
    for horizon in horizons:
        block: dict[str, Any] = {}
        for name, spec in target_index.items():
            if spec["kind"] == "share":
                result = compute_share_transition(
                    windows, spec["value_columns"], spec["values"], horizon=horizon
                )
            else:
                result = compute_scalar_transition(windows, spec["mean_column"], horizon=horizon)
            result["kind"] = spec["kind"]
            result["family"] = spec["family"]
            block[name] = result
        summary["horizons"][str(horizon)] = block
    return summary
