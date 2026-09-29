"""Session segmentation: focus episodes, low-entropy windows and the per-session records.

The per-session segmenter (grow an episode while the next distinct video
stays within ``CUT`` of the recent-members centroid, tolerating up to
``MAX_SKIP`` off-theme videos), the episode / window / session records built
from it, and the forked process pool that segments collections in parallel
(``_FORK_CTX`` is the state a forked child inherits).
"""

import multiprocessing
import os
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd
import pyarrow as pa

from fyp.analysis import entropy_metrics
from fyp.analysis.sessions import inputs
from fyp.core.logging_setup import get_logger

logger = get_logger(__name__)

# The batch context forked workers inherit (copy-on-write) — vectors,
# features, prepared play frames. Task payloads are unit indices only, so
# nothing large is ever pickled. Written by _segment_units right before the
# pool is created; a child must never write to storage, log, or touch the
# reporter (the parent is a multi-threaded gunicorn process and only pure
# compute is fork-safe).
_FORK_CTX: dict = {}


def resolve_workers(requested=None) -> int:
    """Worker-process count for segmentation.

    Args:
        requested: An explicit count, ``"auto"``, or None (read the
            ``[sessions] workers`` config key, default ``"auto"``).

    Returns:
        ``1`` (serial) wherever the ``fork`` start method is unavailable
        (Windows); otherwise the requested count, with ``auto`` resolving
        to one fewer than the machine's cores.
    """
    if requested is None:
        from fyp.core.fyp_config import fyp_cf

        cfg = fyp_cf.get("sessions", {})
        requested = cfg.get("workers", "auto") if isinstance(cfg, dict) else "auto"
    if "fork" not in multiprocessing.get_all_start_methods():
        return 1
    auto = max(1, (os.cpu_count() or 1) - 1)
    if isinstance(requested, str) and requested.strip().lower() in ("", "auto"):
        return auto
    try:
        return max(1, int(requested))
    except (TypeError, ValueError):
        return auto


def _child_init() -> None:
    """Pool-worker initialiser: one BLAS thread per process.

    Seven workers each spinning up an OpenBLAS thread pool would oversubscribe
    the cores, and OpenBLAS thread pools inherited across a fork are a known
    hang. The per-episode eigen decompositions are tiny anyway.
    """
    try:
        from threadpoolctl import threadpool_limits

        threadpool_limits(1)
    except Exception:
        pass


def _run_unit(i: int) -> tuple[int, list[dict], list[dict], list[dict], float]:
    """Segment one work unit of the batch context (runs in parent or child).

    Returns the unit's rows plus its wall seconds, so the batch can report
    the slowest unit — the floor a link cannot go under however many workers
    it has.
    """
    _t = time.perf_counter()
    ctx = _FORK_CTX
    cid, lo, hi = ctx["units"][i]
    codes = ctx["codes"][cid]
    frame = ctx["plays"][cid]
    chunk = frame[(codes >= lo) & (codes < hi)]
    srows, erows, wrows = build_session_group(
        cid,
        chunk,
        ctx["play_ts"][cid],
        ctx["id2idx"],
        ctx["U"],
        ctx["feat"],
        ctx["id_sets"],
        ctx["params"],
        trend_cols=ctx["trend_cols"],
        stories=ctx["stories"],
    )
    return i, srows, erows, wrows, time.perf_counter() - _t


def _prepare_collection(plays: pd.DataFrame, id2idx: dict) -> tuple[pd.DataFrame, np.ndarray]:
    """Time-sort one collection's plays and attach the session/embedded keys.

    Returns:
        ``(plays, play_ts)`` — the sorted frame with ``_sess`` (stable
        session key) and ``_emb`` (membership in ``id2idx``) columns, and the
        sorted int64 timestamps of ALL its plays (``episode_record`` counts
        unembedded plays interleaved inside an episode's span against them).
    """
    plays = plays.sort_values("_ts")
    play_ts = plays["_ts"].astype("int64").to_numpy()

    # Stable session key (isolate rows with no session_id rather than merging them).
    sess = plays["session_id"].astype("string")
    if sess.isna().any():
        # Only build the row-indexed fallback when needed — unconditionally it
        # allocated a full-length Python-string Series per collection that
        # .where() then discarded (session_id is non-null in real data).
        sess = sess.where(
            sess.notna(), "na_" + pd.Series(plays.index, index=plays.index).astype("string")
        )
    plays = plays.assign(_sess=sess)

    # One vectorised membership pass per collection; the per-session loop must
    # never call Series.isin against the whole embedded-id set (see
    # session_record's note on why).
    if "_emb" not in plays.columns:
        plays = plays.assign(_emb=plays["item_id"].isin(list(id2idx)))
    return plays, play_ts


def _session_chunks(codes: np.ndarray, target_plays: int) -> list[tuple[int, int]]:
    """Cut first-appearance-ordered session codes into ``[lo, hi)`` code ranges.

    Each range holds whole sessions totalling at least ``target_plays`` plays
    (except the last), in the order ``groupby(sort=False)`` would visit them.
    """
    if len(codes) == 0:
        return []
    counts = np.bincount(codes)
    ranges: list[tuple[int, int]] = []
    lo, acc = 0, 0
    for code, n in enumerate(counts):
        acc += int(n)
        if acc >= target_plays:
            ranges.append((lo, code + 1))
            lo, acc = code + 1, 0
    if lo < len(counts):
        ranges.append((lo, len(counts)))
    return ranges


def _segment_units(
    units: list[tuple[str, int, int]], ctx: dict, workers: int, reporter=None, log=None
):
    """Run the batch's work units, on a forked pool when ``workers > 1``.

    Any pool failure (a broken pool, a child that died, an unpicklable row)
    is logged and the outstanding units are finished in-process: the pool is
    an accelerator, never a way for a run to fail. Rows are concatenated in
    unit order regardless of completion order.

    Returns:
        ``(session_rows, episode_rows, window_rows, unit_seconds)`` — the
        last a list of per-unit wall seconds — or None when the reporter
        reports a cancellation.
    """
    emit = log if log is not None else logger.info
    _FORK_CTX.clear()
    _FORK_CTX.update(ctx)
    _FORK_CTX["units"] = units
    results: dict[int, tuple[list, list, list]] = {}
    unit_seconds: dict[int, float] = {}
    if reporter is not None and reporter.check_cancelled():
        return None
    if workers > 1 and len(units) > 1:
        try:
            with warnings.catch_warnings():
                # Python 3.12 warns that forking a multi-threaded process may
                # deadlock the child: the children here do pure compute on
                # inherited memory and take no locks, which is the safe case.
                warnings.filterwarnings("ignore", message=".*fork.*", category=DeprecationWarning)
                mp_ctx = multiprocessing.get_context("fork")
                with ProcessPoolExecutor(
                    max_workers=min(workers, len(units)), mp_context=mp_ctx, initializer=_child_init
                ) as ex:
                    futures = [ex.submit(_run_unit, i) for i in range(len(units))]
                    for n_done, fut in enumerate(as_completed(futures), start=1):
                        i, srows, erows, wrows, secs = fut.result()
                        results[i] = (srows, erows, wrows)
                        unit_seconds[i] = secs
                        if (
                            reporter is not None
                            and n_done % inputs._CANCEL_CHECK_EVERY == 0
                            and reporter.check_cancelled()
                        ):
                            ex.shutdown(wait=False, cancel_futures=True)
                            return None
        except Exception as e:
            emit(
                f"[SESSIONS] worker pool failed ({type(e).__name__}: {e}) "
                f"— finishing this batch serially"
            )
    # The in-process path runs under the same one-thread BLAS as the pool
    # children: the episode geometry takes the max of a float32 U @ U.T, and
    # multi-threaded kernels sum in a different order, which is enough to flip
    # an episode's 4-decimal `diameter` between a workers=1 and a pooled build.
    # Same kernel everywhere → bit-identical rows.
    pending = [i for i in range(len(units)) if i not in results]
    if pending:
        try:
            from threadpoolctl import threadpool_limits
        except Exception:
            from contextlib import nullcontext as threadpool_limits  # type: ignore
        with threadpool_limits(1):
            for i in pending:
                if reporter is not None and reporter.check_cancelled():
                    return None
                _, srows, erows, wrows, secs = _run_unit(i)
                results[i] = (srows, erows, wrows)
                unit_seconds[i] = secs
    session_rows: list[dict] = []
    episode_rows: list[dict] = []
    window_rows: list[dict] = []
    for i in range(len(units)):
        srows, erows, wrows = results[i]
        session_rows.extend(srows)
        episode_rows.extend(erows)
        window_rows.extend(wrows)
    return (session_rows, episode_rows, window_rows, [unit_seconds[i] for i in range(len(units))])


def _segment_collections(
    cids: list[str],
    plays: pd.DataFrame,
    id2idx: dict,
    U: np.ndarray,
    feat: pd.DataFrame,
    id_sets: dict,
    params: dict,
    trend_cols: list[str] | None,
    stories: dict[str, str] | None,
    workers: int,
    reporter=None,
    log=None,
):
    """Segment ``cids`` against one vector context, as parallel work units.

    Returns:
        ``(session_rows, episode_rows, window_rows, unit_seconds)`` or None
        on cancellation.
    """
    ctx: dict = {
        "plays": {},
        "play_ts": {},
        "codes": {},
        "id2idx": id2idx,
        "U": U,
        "feat": feat,
        "id_sets": id_sets,
        "params": params,
        "trend_cols": trend_cols,
        "stories": stories,
    }
    units: list[tuple[str, int, int]] = []
    for cid in cids:
        prepared, play_ts = _prepare_collection(plays[plays["collection_id"] == cid], id2idx)
        codes, _ = pd.factorize(prepared["_sess"])
        ctx["plays"][cid] = prepared
        ctx["play_ts"][cid] = play_ts
        ctx["codes"][cid] = np.asarray(codes)
        units.extend((cid, lo, hi) for lo, hi in _session_chunks(codes, inputs.SESSION_CHUNK_PLAYS))
    return _segment_units(units, ctx, workers, reporter=reporter, log=log)


def segment_session(
    seq: list[tuple],
    U: np.ndarray,
    cut: float,
    mem: int,
    min_videos: int,
    min_minutes: float,
    max_skip: int = inputs.MAX_SKIP,
    flick_seconds: float = inputs.FLICK_SECONDS,
) -> list[dict]:
    """Grow focus episodes within one session's embedded plays.

    A run survives up to ``max_skip`` CONSECUTIVE off-theme videos. They are
    tolerated, not absorbed: a skipped video is never a member, never enters
    the centroid, and never extends the span — it is only counted, as
    ``n_skipped``. An ad break in the middle of a binge is the motivating case.

    An off-theme video the viewer merely flicked past (dwell under
    ``flick_seconds``) does not count toward ``max_skip`` at all — a video
    dismissed in under a couple of seconds is feed noise the viewer rejected,
    not a departure from the theme. Only off-theme videos the viewer actually
    watched spend the skip budget. Flicked videos are still tolerated, never
    members, and still count in ``n_skipped`` when the run resumes.
    ``flick_seconds = 0`` disables the rule (every off-theme play counts).

    ``max_skip = 0`` restores the original no-tolerance behaviour, where one off-theme
    video ended the run AND became the first member of the next one. That
    second effect was the damaging one: the theme then had to re-accumulate
    from an anchor that was not the theme, which is why long on-theme stretches
    fragmented into runs too small to keep (99.4% of candidate runs on the
    production corpus ended with fewer than ``min_videos`` videos).

    When the tolerance IS exhausted, the run ends and the scan rewinds to the
    first tolerated video, so the videos that ended one binge are available to
    open the next — they are never silently dropped.

    Args:
        seq: Time-ordered ``(item_id, row_idx, ts, dur)`` tuples for the
            session's embedded plays.
        U: Directional vector store.
        cut: Focus threshold on mean cosine distance to the recent centroid.
        mem: Number of recent members the centroid is taken over.
        min_videos: Minimum distinct videos to keep an episode.
        min_minutes: Minimum span (minutes) to keep an episode.
        max_skip: Consecutive off-theme videos a run tolerates.
        flick_seconds: Dwell (seconds) under which an off-theme video does not
            count toward ``max_skip``. 0 disables.

    Returns:
        A list of episode dicts (raw members + span; geometry/content are
        attributed later by :func:`episode_record`).
    """
    episodes: list[dict] = []
    cur: dict | None = None
    pending: list[int] = []
    pending_counted = 0

    def close(c: dict | None) -> None:
        if c is None or len(c["idx"]) < min_videos:
            return
        if (c["end_ts"] - c["start_ts"]).total_seconds() / 60.0 >= min_minutes:
            episodes.append(c)

    def fresh(iid, ridx, ts, dur) -> dict:
        return {
            "ids": [iid],
            "idx": [ridx],
            "seen": {iid},
            "m_ts": [ts],
            "m_dur": [dur],
            "start_ts": ts,
            "end_ts": ts,
            "n_plays": 1,
            "n_skipped": 0,
        }

    i = 0
    while i < len(seq):
        iid, ridx, ts, dur = seq[i]
        if cur is None:
            cur = fresh(iid, ridx, ts, dur)
            pending = []
            pending_counted = 0
            i += 1
            continue
        if iid in cur["seen"]:
            # A rewatch extends the span but is not a new member — otherwise a
            # repeat loop collapses the effective rank and fakes a binge.
            cur["n_plays"] += 1
            cur["end_ts"] = ts
            i += 1
            continue
        centroid = U[cur["idx"][-mem:]].mean(axis=0)
        dist = 1.0 - float(U[ridx] @ centroid)
        if dist <= cut:
            cur["ids"].append(iid)
            cur["idx"].append(ridx)
            cur["seen"].add(iid)
            cur["m_ts"].append(ts)
            cur["m_dur"].append(dur)
            cur["n_plays"] += 1
            cur["end_ts"] = ts
            # Only now are the tolerated videos INSIDE the binge — a run that
            # ends on an interruption never counts it.
            cur["n_skipped"] += len(pending)
            pending = []
            pending_counted = 0
            i += 1
            continue

        pending.append(i)
        # An unknown dwell cannot prove a flick, so it spends the budget.
        dwell = _num(dur)
        if not (flick_seconds > 0 and dwell is not None and dwell < flick_seconds):
            pending_counted += 1
        if pending_counted <= max_skip:
            i += 1
            continue
        close(cur)
        # Rewind so the tolerated videos get a fair chance to open the next
        # run; the restart point always advances, so this terminates.
        i = pending[0]
        cur = None
        pending = []
        pending_counted = 0
    close(cur)
    return episodes


def _num(value, ndigits: int | None = None) -> float | None:
    """Return ``value`` as a float (optionally rounded), or None when missing.

    PyArrow-backed frames yield ``pd.NA`` from reductions like ``mean()`` /
    ``median()`` when the inputs are all-null; ``float(pd.NA)`` raises, so
    every scalar destined for an artifact row goes through this guard.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    out = float(value)
    if not np.isfinite(out):
        return None
    return round(out, ndigits) if ndigits is not None else out


def _dominant(series: pd.Series) -> tuple[object, float]:
    """Return the modal value of a series and its share."""
    s = series.dropna()
    if s.empty:
        return None, 0.0
    vc = s.value_counts()
    return vc.index[0], round(float(vc.iloc[0]) / float(len(s)), 3)


def _rolling_cosdists(idx: list[int], U: np.ndarray, mem: int) -> list[float | None]:
    """Per-member mean cosine distance to the centroid of the previous members.

    Element ``i`` (``i ≥ 1``) is the distance of member ``i`` to the centroid
    of the previous ``min(i, mem)`` members — the exact quantity the segmenter
    thresholds against ``cut``, so the UI sparkline shows *why* the episode
    held together. Element 0 is None.

    Args:
        idx: Episode members' row indices into ``U``, in time order.
        U: Directional vector store.
        mem: Centroid memory (same as the segmenter's).

    Returns:
        A list aligned to ``idx``.
    """
    out: list[float | None] = [None]
    for i in range(1, len(idx)):
        centroid = U[idx[max(0, i - mem) : i]].mean(axis=0)
        out.append(round(1.0 - float(U[idx[i]] @ centroid), 4))
    return out


def episode_record(
    ep: dict,
    cid: str,
    sess: object,
    U: np.ndarray,
    feat: pd.DataFrame,
    play_ts: np.ndarray,
    mem: int = inputs.MEM,
) -> dict:
    """Reduce one raw episode to a fully-attributed table row.

    Args:
        ep: A raw episode dict from :func:`segment_session`.
        cid: The collection id.
        sess: The session key.
        U: Directional vector store.
        feat: Per-video features indexed by item_id (:func:`load_video_features`).
        play_ts: Sorted int64 timestamps of ALL the collection's plays (used to
            count unembedded plays interleaved inside the episode's span).
        mem: Centroid memory for the rolling-distance series.

    Returns:
        One episode row (JSON/parquet-friendly scalars + member lists).
    """
    idx = np.asarray(ep["idx"])
    Uep = U[idx]
    k = len(idx)
    span_min = round((ep["end_ts"] - ep["start_ts"]).total_seconds() / 60.0, 2)

    geo = entropy_metrics.trajectory_geometry(Uep)
    ent_bits, eff_rank = entropy_metrics.spectral_entropy(Uep)
    focus = entropy_metrics.mean_pairwise_cosine_distance(Uep)

    # Plays of any kind (incl. unembedded) inside the episode's span — measures
    # how much off-corpus content interleaved the focused run.
    lo, hi = np.searchsorted(play_ts, [ep["start_ts"].value, ep["end_ts"].value + 1])
    n_in_span = int(hi - lo)

    f = feat.reindex(ep["ids"])
    niche, niche_share = _dominant(f["niche_name"])
    author, author_share = _dominant(f["author"])
    adv, adv_share = _dominant(f["advertising"])

    dwell = [_num(v, 1) for v in ep["m_dur"]]
    return {
        "collection_id": cid,
        "session_id": str(sess),
        "start_ts": ep["start_ts"].isoformat(),
        "end_ts": ep["end_ts"].isoformat(),
        "duration_min": span_min,
        "n_plays": int(ep["n_plays"]),
        "n_distinct": k,
        "repeat_rate": round(ep["n_plays"] / k, 2),
        "n_interleaved": max(n_in_span - int(ep["n_plays"]), 0),
        # Off-theme videos the binge survived (see segment_session's max_skip).
        # Reported so a long binge cannot hide how much it tolerated.
        "n_skipped": int(ep.get("n_skipped", 0)),
        "focus": _num(focus, 4),
        "diameter": _num(geo["diameter"], 4),
        "step_mean": _num(geo["step_mean"], 4),
        "straightness": _num(geo["straightness"], 4),
        "spectral_entropy_bits": _num(ent_bits, 4),
        "effective_rank": _num(eff_rank, 3),
        "direction_p": _num(entropy_metrics.direction_permutation_p(Uep), 4),
        "dominant_niche": niche,
        "dominant_niche_share": niche_share,
        "n_niches": int(f["niche_name"].nunique()),
        "n_authors": int(f["author"].nunique()),
        "dominant_author_share": author_share,
        "advertising": None if adv is None or pd.isna(adv) else str(adv),
        "advertising_share": adv_share,
        "mean_political": _num(pd.to_numeric(f["political_score"], errors="coerce").mean(), 4),
        "mean_sensitivity": _num(pd.to_numeric(f["sensitivity_score"], errors="coerce").mean(), 4),
        "member_item_ids": [str(i) for i in ep["ids"]],
        "member_ts": [t.isoformat() for t in ep["m_ts"]],
        "member_dwell_s": dwell,
        "member_rolling_cosdist": _rolling_cosdists(list(ep["idx"]), U, mem),
    }


def low_entropy_windows(
    emb_seq: list[tuple], U: np.ndarray, window_n: int, max_windows: int = 3
) -> list[dict]:
    """The session's lowest-distance ("low-entropy") sliding windows.

    Slides a window of ``window_n`` consecutive *distinct* embedded videos
    (first-occurrence order — repeats are already deduped upstream) across the
    session, scores each window by its mean pairwise cosine distance, and
    greedily keeps up to ``max_windows`` **non-overlapping** windows in
    ascending score order. Because every window has the same size, the
    normalised spectral entropy reported alongside is directly comparable
    across windows too; the distance stays the rank key (the study found it
    the more sensitive of the two).

    Args:
        emb_seq: Time-ordered ``(item_id, row_idx, ts, dwell)`` tuples for the
            session's distinct embedded videos (first play of each).
        U: Directional vector store.
        window_n: Window width (distinct videos).
        max_windows: Maximum number of non-overlapping windows to keep.

    Returns:
        A list of window dicts (ascending distance; may be empty when the
        session has fewer than ``window_n`` distinct embedded videos), each
        with ``mean_cosdist``/``entropy_norm``/member lists.
    """
    n = len(emb_seq)
    if n < window_n:
        return []
    idx = [row for _, row, _, _ in emb_seq]
    scored: list[tuple[float, int]] = []
    for i in range(0, n - window_n + 1):
        d = entropy_metrics.mean_pairwise_cosine_distance(U[idx[i : i + window_n]])
        if np.isfinite(d):
            scored.append((float(d), i))
    scored.sort()

    chosen: list[tuple[float, int]] = []
    taken: list[tuple[int, int]] = []
    for d, i in scored:
        span = (i, i + window_n - 1)
        if any(span[0] <= hi and span[1] >= lo for lo, hi in taken):
            continue
        chosen.append((d, i))
        taken.append(span)
        if len(chosen) >= max_windows:
            break

    out: list[dict] = []
    for d, i in chosen:
        members = emb_seq[i : i + window_n]
        ent_bits, _ = entropy_metrics.spectral_entropy(U[idx[i : i + window_n]])
        ent_norm = float(ent_bits / np.log2(window_n)) if np.isfinite(ent_bits) else None
        start_ts, end_ts = members[0][2], members[-1][2]
        out.append(
            {
                "start_ts": start_ts.isoformat(),
                "end_ts": end_ts.isoformat(),
                "duration_min": round((end_ts - start_ts).total_seconds() / 60.0, 2),
                "n_distinct": window_n,
                "mean_cosdist": round(d, 4),
                "entropy_norm": round(ent_norm, 4) if ent_norm is not None else None,
                "member_item_ids": [str(m[0]) for m in members],
                "member_ts": [m[2].isoformat() for m in members],
                "member_dwell_s": [_num(m[3], 1) for m in members],
            }
        )
    return out


def _search_text(distinct_list: list[str], feat: pd.DataFrame, stories: dict[str, str]) -> str:
    """Build one session's searchable text blob (lowercased, deduped, capped).

    Concatenates the text the detail panel displays — niche names, categories,
    creator handles, video descriptions + hashtags, and the AI story summaries
    — so the overview's free-text search matches exactly what a researcher
    sees when they open the session.
    """
    frags: set[str] = set()
    sub = feat.reindex(distinct_list)
    for col in ("niche_name", "category", "author"):
        if col not in sub.columns:
            continue
        for value in sub[col].dropna().unique():
            text = str(value).strip()
            if text:
                frags.add(text[: inputs._SEARCH_FRAGMENT_CAP])
    for col in ("desc", "desc_hashtags"):
        if col not in sub.columns:
            continue
        for value in sub[col].dropna():
            # desc_hashtags is a LIST column — a cell can be an array of tags.
            if isinstance(value, (list, tuple, np.ndarray)):
                text = " ".join(str(v).strip() for v in value if v is not None and str(v).strip())
            else:
                text = str(value).strip()
            if text:
                frags.add(text[: inputs._SEARCH_FRAGMENT_CAP])
    for iid in distinct_list:
        story = stories.get(iid)
        if story:
            frags.add(story[: inputs._SEARCH_FRAGMENT_CAP])
    return "\n".join(sorted(frags)).lower()[: inputs._SEARCH_TEXT_CAP]


def session_record(
    cid: str,
    sess: object,
    g: pd.DataFrame,
    id2idx: dict,
    U: np.ndarray,
    feat: pd.DataFrame,
    id_sets: dict,
    episodes: list[dict],
    window_n: int = inputs.WINDOW_N,
    max_windows: int = inputs.MAX_WINDOWS,
    trend_cols: list[str] | None = None,
    stories: dict[str, str] | None = None,
) -> tuple[dict, list[dict]]:
    """Reduce one session's plays to a quality/entropy row + its low-entropy windows.

    Args:
        cid: The collection id.
        sess: The session key.
        g: The session's play rows, time-sorted, with ``_ts``/``item_id``/
            ``play_duration``.
        id2idx: item_id → row map into ``U`` (the embedded set).
        U: Directional vector store.
        feat: Per-video features indexed by item_id.
        id_sets: Enrichment id sets from :func:`enrichment_id_sets`.
        episodes: The session's attributed episode rows.
        window_n: Sliding-window width for the low-entropy windows.
        trend_cols: Numeric feature columns to emit ``vmin_``/``vmax_``
            session-extreme columns for (see :func:`trend_numeric_columns`).
        stories: item_id → story text for the search blob (see
            :func:`load_story_texts`).

    Returns:
        ``(session_row, window_rows)`` — the row for ``sessions_index.parquet``
        and up to :data:`MAX_WINDOWS` attributed low-entropy-window rows.
    """
    n_plays = int(len(g))
    # Plain-Python membership throughout: ``Series.isin(<set>)`` re-hashes the
    # whole (100k+-id) set on every call, which at ~10^5 sessions per corpus
    # turns the build from minutes into hours.
    items_list = [str(i) for i in g["item_id"]]
    seen: set[str] = set()
    distinct_list = [i for i in items_list if not (i in seen or seen.add(i))]
    n_distinct = len(distinct_list)
    start_ts, end_ts = g["_ts"].iloc[0], g["_ts"].iloc[-1]
    dur = pd.to_numeric(g["play_duration"], errors="coerce")

    n_scraped = sum(1 for i in distinct_list if i in id_sets["scraped"])
    n_annotated = sum(1 for i in distinct_list if i in id_sets["annotated"])
    embedded = id_sets["embedded"]
    emb_distinct = [i for i in distinct_list if i in embedded]
    n_embedded = len(emb_distinct)
    emb_plays = sum(1 for i in items_list if i in embedded)

    # Distinct embedded videos in first-play order (rewatches deduped per the
    # study guardrail), each with its first play's timestamp and dwell — the
    # sequence the low-entropy window slides over.
    emb_seq: list[tuple] = []
    seq_seen: set[str] = set()
    for iid, ts, du in zip(items_list, g["_ts"], g["play_duration"]):
        if iid in seq_seen or iid not in id2idx:
            continue
        seq_seen.add(iid)
        emb_seq.append((iid, id2idx[iid], ts, du))
    windows = low_entropy_windows(emb_seq, U, window_n, max_windows=max_windows)
    for w_idx, w in enumerate(windows):
        w["collection_id"] = cid
        w["session_id"] = str(sess)
        w["window_idx"] = w_idx
        w["dominant_niche"], _ = _dominant(feat.reindex(w["member_item_ids"])["niche_name"])
    min_cosdist = windows[0]["mean_cosdist"] if windows else None
    ent_norm = windows[0]["entropy_norm"] if windows else None

    emb_feat = feat.reindex(emb_distinct)
    niche, _ = _dominant(emb_feat["niche_name"]) if len(emb_feat) else (None, 0.0)

    ep_plays = int(sum(e["n_plays"] for e in episodes))
    med_dwell = _num(dur.median(), 1)

    # Session-extreme values of the numeric trend variables (over distinct
    # items) + dwell (per-play), so the overview can filter on "session max of
    # <variable>" without touching the map at request time.
    all_feat = feat.reindex(distinct_list)
    extremes: dict[str, float | None] = {}
    for col in trend_cols or []:
        vals = (
            pd.to_numeric(all_feat[col], errors="coerce")
            if col in all_feat.columns
            else pd.Series(dtype="float64")
        )
        extremes[f"vmin_{col}"] = _num(vals.min(), 4)
        extremes[f"vmax_{col}"] = _num(vals.max(), 4)
    extremes["vmin_dwell_s"] = _num(dur.min(), 1)
    extremes["vmax_dwell_s"] = _num(dur.max(), 1)

    return {
        **extremes,
        "search_text": _search_text(distinct_list, feat, stories or {}),
        "collection_id": cid,
        "session_id": str(sess),
        "start_ts": start_ts.isoformat(),
        "end_ts": end_ts.isoformat(),
        "duration_min": round((end_ts - start_ts).total_seconds() / 60.0, 2),
        "n_plays": n_plays,
        "n_distinct": n_distinct,
        "total_watch_s": _num(dur.fillna(0).sum(), 1) or 0.0,
        "median_dwell_s": med_dwell,
        "n_scraped": n_scraped,
        "n_annotated": n_annotated,
        "n_embedded": n_embedded,
        "coverage_scraped": round(n_scraped / n_distinct, 4) if n_distinct else 0.0,
        "coverage_annotated": round(n_annotated / n_distinct, 4) if n_distinct else 0.0,
        "coverage_embedded": round(n_embedded / n_distinct, 4) if n_distinct else 0.0,
        "emb_play_coverage": round(emb_plays / n_plays, 4) if n_plays else 0.0,
        "min_window_cosdist": min_cosdist,
        "min_window_entropy_norm": ent_norm,
        "n_episodes": len(episodes),
        "episode_play_frac": round(ep_plays / n_plays, 4) if n_plays else 0.0,
        "dominant_niche": niche,
        "n_niches": int(emb_feat["niche_name"].nunique()) if len(emb_feat) else 0,
    }, windows


def build_collection(
    cid: str,
    plays: pd.DataFrame,
    id2idx: dict,
    U: np.ndarray,
    feat: pd.DataFrame,
    id_sets: dict,
    params: dict | None = None,
    trend_cols: list[str] | None = None,
    stories: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Segment one collection's sessions and build its session + episode rows.

    Every session gets a row (including sessions with no embedded plays — they
    are exactly what the quality filter must be able to see and exclude);
    episodes are detected only on embedded plays.

    Args:
        cid: The collection id.
        plays: The collection's play rows (from :func:`load_plays`).
        id2idx: item_id → row map into ``U``.
        U: Directional vector store.
        feat: Per-video features indexed by item_id.
        id_sets: Enrichment id sets from :func:`enrichment_id_sets`.
        params: Optional parameter overrides (see :func:`default_params`).
        trend_cols: Numeric feature columns for the session-extreme columns.
        stories: item_id → story text for the search blob.

    Returns:
        ``(session_rows, episode_rows, window_rows)``.
    """
    p = {**inputs.default_params(), **(params or {})}
    prepared, play_ts = _prepare_collection(plays, id2idx)
    return build_session_group(
        cid, prepared, play_ts, id2idx, U, feat, id_sets, p, trend_cols=trend_cols, stories=stories
    )


def build_session_group(
    cid: str,
    plays: pd.DataFrame,
    play_ts: np.ndarray,
    id2idx: dict,
    U: np.ndarray,
    feat: pd.DataFrame,
    id_sets: dict,
    p: dict,
    trend_cols: list[str] | None = None,
    stories: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Build the rows for a group of whole sessions of one collection.

    The parallel work unit: ``plays`` is a session-complete slice of the
    frame :func:`_prepare_collection` returned (any subset of sessions, in
    first-appearance order), so the rows for a collection are the same
    whether it is built in one call or many.

    Args:
        cid: The collection id.
        plays: Prepared play rows (``_sess``/``_emb`` present, time-sorted)
            for whole sessions only.
        play_ts: Sorted int64 timestamps of ALL the collection's plays.
        id2idx: item_id → row map into ``U``.
        U: Directional vector store.
        feat: Per-video features indexed by item_id.
        id_sets: Enrichment id sets from :func:`enrichment_id_sets`.
        p: Resolved segmentation parameters (see :func:`default_params`).
        trend_cols: Numeric feature columns for the session-extreme columns.
        stories: item_id → story text for the search blob.

    Returns:
        ``(session_rows, episode_rows, window_rows)``.
    """
    session_rows: list[dict] = []
    episode_rows: list[dict] = []
    window_rows: list[dict] = []
    for s, g in plays.groupby("_sess", sort=False):
        emb = g[g["_emb"]]
        seq = [
            (iid, id2idx[iid], ts, du)
            for iid, ts, du in zip(emb["item_id"], emb["_ts"], emb["play_duration"])
        ]
        eps = []
        for ep_idx, ep in enumerate(
            segment_session(
                seq,
                U,
                p["cut"],
                p["mem"],
                p["min_videos"],
                p["min_minutes"],
                max_skip=p["max_skip"],
                flick_seconds=p["flick_seconds"],
            )
        ):
            row = episode_record(ep, cid, s, U, feat, play_ts, mem=p["mem"])
            row["episode_idx"] = ep_idx
            eps.append(row)
        episode_rows.extend(eps)
        srow, wins = session_record(
            cid,
            s,
            g,
            id2idx,
            U,
            feat,
            id_sets,
            eps,
            window_n=p["window_n"],
            max_windows=p["max_windows"],
            trend_cols=trend_cols,
            stories=stories,
        )
        session_rows.append(srow)
        window_rows.extend(wins)
    return session_rows, episode_rows, window_rows


# Explicit Arrow schemas so `data_io.save_parquet` takes its all-ArrowDtype
# fast path and readers see stable dtypes (DEVELOPING.md: PyArrow dtypes always).
_SESSIONS_SCHEMA: dict[str, pa.DataType] = {
    "collection_id": pa.string(),
    "session_id": pa.string(),
    "start_ts": pa.string(),
    "end_ts": pa.string(),
    "duration_min": pa.float32(),
    "n_plays": pa.int32(),
    "n_distinct": pa.int32(),
    "total_watch_s": pa.float32(),
    "median_dwell_s": pa.float32(),
    "n_scraped": pa.int32(),
    "n_annotated": pa.int32(),
    "n_embedded": pa.int32(),
    "coverage_scraped": pa.float32(),
    "coverage_annotated": pa.float32(),
    "coverage_embedded": pa.float32(),
    "emb_play_coverage": pa.float32(),
    "min_window_cosdist": pa.float32(),
    "min_window_entropy_norm": pa.float32(),
    "n_episodes": pa.int16(),
    "episode_play_frac": pa.float32(),
    "dominant_niche": pa.string(),
    "n_niches": pa.int16(),
}
