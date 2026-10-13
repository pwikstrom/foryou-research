"""Bring whole viewing sessions to full coverage: select them, queue their videos.

An analysis that needs every video of a session described cannot use a
session in which a tenth of the videos were never scraped or annotated. The
regular enrichment loop samples whole sessions on random days, which rarely
completes long sessions. A *session campaign* instead picks long sessions
that are close to complete and queues exactly their missing videos.

The selection is deterministic and balanced across participants:

* A session is **covered** when at least ``coverage`` of its distinct videos
  are both scraped and annotated, and **long** when it has at least
  ``min_plays`` plays.
* A candidate is a long session that is not covered but **feasible**: its
  expected coverage after enrichment (current annotations plus the expected
  yield of its scrapeable and annotatable videos) reaches ``feasible``.
* Its **cost** is the expected number of new annotations.
* Participants take turns, fewest covered long sessions first. In each round
  every participant below ``target`` takes its cheapest feasible candidate,
  provided that month already holds fewer than ``month_cap`` of its covered
  or selected sessions and the participant's and the campaign's budgets
  allow it. Ties break by a salted hash, so the same inputs always give the
  same selection.

Every attemptable missing video of a selected session is queued, not just
enough to reach the threshold, as a buffer against failed scrapes.

The pure functions here take frames and return frames;
``scripts/session_campaign.py`` loads the data, writes the manifest and
queues the videos.
"""

import hashlib
import json

import numpy as np
import pandas as pd

from web_interface.services.collection_enrichment import stable_rank

LONG_PLAYS = 60
COVERAGE = 0.9
FEASIBLE = 0.92


def _flag(status: pd.DataFrame | None, name: str, ids: pd.Index) -> np.ndarray:
    if status is None or name not in status.columns:
        return np.zeros(len(ids), dtype=bool)
    return status[name].reindex(ids).fillna(False).to_numpy(dtype=bool)


def item_states(item_ids, status: pd.DataFrame | None) -> pd.DataFrame:
    """Each item's enrichment state for coverage purposes.

    Args:
        item_ids: Item ids.
        status: Enrichment status indexed by item_id with ``scraped_ok``,
            ``scrape_fail``, ``video_downloaded``, ``annotated_ok`` and
            ``annotated_fail`` (see ``collection_enrichment.load_status``).

    Returns:
        A frame indexed by item_id with boolean ``covered`` (scraped and
        annotated), ``to_annotate`` (scraped with media, not yet annotated,
        not failed), ``to_scrape`` (not scraped, not permanently failed);
        anything else is dead (a failed scrape or annotation, or no media).
    """
    ids = pd.Index([str(i) for i in item_ids])
    scraped = _flag(status, "scraped_ok", ids)
    scrape_fail = _flag(status, "scrape_fail", ids)
    media = (
        _flag(status, "video_downloaded", ids)
        if status is not None and "video_downloaded" in status.columns
        else scraped
    )
    ann = _flag(status, "annotated_ok", ids)
    ann_fail = _flag(status, "annotated_fail", ids)
    return pd.DataFrame(
        {
            "covered": scraped & ann,
            "to_annotate": scraped & media & ~ann & ~ann_fail,
            "to_scrape": ~scraped & ~scrape_fail,
        },
        index=ids,
    )


def enrichment_yields(states_status: pd.DataFrame | None) -> dict:
    """Observed success shares of scraping and annotating, for feasibility.

    Args:
        states_status: Enrichment status (as in :func:`item_states`) for the
            campaign's items.

    Returns:
        ``{"scrape": share of attempted scrapes that succeeded, "annotate":
        share of attempted annotations that succeeded}`` (1.0 when nothing
        was attempted).
    """
    if states_status is None or states_status.empty:
        return {"scrape": 1.0, "annotate": 1.0}
    s = states_status
    ok = int(s.get("scraped_ok", pd.Series(dtype=bool)).fillna(False).sum())
    fail = int(s.get("scrape_fail", pd.Series(dtype=bool)).fillna(False).sum())
    a_ok = int(s.get("annotated_ok", pd.Series(dtype=bool)).fillna(False).sum())
    a_fail = int(s.get("annotated_fail", pd.Series(dtype=bool)).fillna(False).sum())
    return {
        "scrape": round(ok / (ok + fail), 4) if ok + fail else 1.0,
        "annotate": round(a_ok / (a_ok + a_fail), 4) if a_ok + a_fail else 1.0,
    }


def session_table(
    plays: pd.DataFrame,
    states: pd.DataFrame,
    scrape_yield: float = 1.0,
    annotate_yield: float = 1.0,
) -> pd.DataFrame:
    """One row per session with its size, coverage, expected coverage and cost.

    Args:
        plays: Play rows with ``collection_id``, ``session_id``, ``item_id``
            and ``_ts``.
        states: :func:`item_states` for (at least) the plays' items.
        scrape_yield: Expected share of queued scrapes that succeed.
        annotate_yield: Expected share of queued annotations that succeed.

    Returns:
        Columns ``collection_id``, ``session_id``, ``start``, ``month``
        (``YYYY-MM`` of the start), ``n_plays``, ``n_distinct``, ``n_covered``,
        ``n_to_annotate``, ``n_to_scrape``, ``coverage``,
        ``expected_coverage`` and ``cost`` (expected new annotations).
    """
    p = plays.dropna(subset=["session_id"]).copy()
    p["item_id"] = p["item_id"].astype(str)
    p["session_id"] = p["session_id"].astype(str)
    p["collection_id"] = p["collection_id"].astype(str)
    sess = p.groupby("session_id", sort=False).agg(
        collection_id=("collection_id", "first"),
        start=("_ts", "min"),
        n_plays=("item_id", "size"),
    )
    distinct = p.drop_duplicates(["session_id", "item_id"])[["session_id", "item_id"]]
    st = states.reindex(distinct["item_id"].to_numpy())
    distinct = distinct.assign(
        covered=st["covered"].fillna(False).to_numpy(dtype=bool),
        to_annotate=st["to_annotate"].fillna(False).to_numpy(dtype=bool),
        to_scrape=st["to_scrape"].fillna(True).to_numpy(dtype=bool),
    )
    counts = distinct.groupby("session_id", sort=False).agg(
        n_distinct=("item_id", "size"),
        n_covered=("covered", "sum"),
        n_to_annotate=("to_annotate", "sum"),
        n_to_scrape=("to_scrape", "sum"),
    )
    out = sess.join(counts).join(_runs(p, states)).reset_index()
    out["month"] = pd.to_datetime(out["start"]).dt.strftime("%Y-%m")
    n = out["n_distinct"].clip(lower=1)
    out["coverage"] = out["n_covered"] / n
    out["attempted_share"] = (out["n_distinct"] - out["n_to_scrape"]) / n
    out["expected_coverage"] = (
        out["n_covered"]
        + annotate_yield * out["n_to_annotate"]
        + scrape_yield * annotate_yield * out["n_to_scrape"]
    ) / n
    out["cost"] = out["n_to_annotate"] + scrape_yield * out["n_to_scrape"]
    return out[
        [
            "collection_id",
            "session_id",
            "start",
            "month",
            "n_plays",
            "n_distinct",
            "n_covered",
            "n_to_annotate",
            "n_to_scrape",
            "coverage",
            "attempted_share",
            "max_missing_run",
            "max_dead_run",
            "expected_coverage",
            "cost",
        ]
    ]


def _runs(p: pd.DataFrame, states: pd.DataFrame) -> pd.DataFrame:
    """Per session, the longest run of consecutive plays whose video is not
    covered (``max_missing_run``) and whose video can no longer be covered
    (``max_dead_run``: failed scrape, no media or failed annotation)."""
    q = p.sort_values(["session_id", "_ts"], kind="mergesort")
    st = states.reindex(q["item_id"].to_numpy())
    covered = st["covered"].fillna(False).to_numpy(dtype=bool)
    dead = ~covered & ~st["to_annotate"].fillna(False).to_numpy(dtype=bool)
    dead &= ~st["to_scrape"].fillna(True).to_numpy(dtype=bool)
    sess = q["session_id"].to_numpy()
    out_missing, out_dead = {}, {}
    run_m = run_d = 0
    for i in range(len(q)):
        if i == 0 or sess[i] != sess[i - 1]:
            run_m = run_d = 0
        run_m = 0 if covered[i] else run_m + 1
        run_d = run_d + 1 if dead[i] else 0
        s = sess[i]
        if run_m > out_missing.get(s, 0):
            out_missing[s] = run_m
        if run_d > out_dead.get(s, 0):
            out_dead[s] = run_d
    idx = pd.Index(pd.unique(sess), name="session_id")
    return pd.DataFrame(
        {
            "max_missing_run": [out_missing.get(s, 0) for s in idx],
            "max_dead_run": [out_dead.get(s, 0) for s in idx],
        },
        index=idx,
    )


def mark_rule(
    table: pd.DataFrame,
    rule: str = "share",
    coverage: float = COVERAGE,
    feasible: float = FEASIBLE,
    max_gap: int | None = None,
) -> pd.DataFrame:
    """Mark which sessions are covered now and which could become covered.

    Rules:
        ``"share"``: covered when at least ``coverage`` of the distinct videos
            are scraped and annotated; feasible when the expected share after
            enrichment reaches ``feasible``.
        ``"attempted"``: covered when every distinct video has been tried
            (scraped or permanently failed) and at least ``coverage`` of them
            are annotated; feasible when the expected annotated share after
            enrichment reaches ``feasible``.
        With ``max_gap``, a covered session also has no run of more than
        ``max_gap`` consecutive plays without an annotated video, and a
        feasible one has no such run of videos that can no longer be
        annotated.

    Returns:
        ``table`` with boolean ``is_covered`` and ``is_feasible`` columns.
    """
    t = table.copy()
    if rule == "attempted":
        covered = (t["attempted_share"] >= 1.0) & (t["coverage"] >= coverage)
    elif rule == "share":
        covered = t["coverage"] >= coverage
    else:
        raise ValueError(f"unknown rule {rule!r}")
    feas = t["expected_coverage"] >= feasible
    if max_gap is not None:
        covered &= t["max_missing_run"] <= max_gap
        feas &= t["max_dead_run"] <= max_gap
    t["is_covered"] = covered
    t["is_feasible"] = feas & ~covered
    return t


def select_sessions(
    table: pd.DataFrame,
    target: int,
    month_cap: int,
    budget: float,
    per_participant_budget: float | None = None,
    min_plays: int = LONG_PLAYS,
    coverage: float = COVERAGE,
    feasible: float = FEASIBLE,
    salt: str = "",
    preselected: set | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Pick sessions to complete (see the module docstring for the rule).

    Args:
        table: :func:`session_table` output, optionally with ``is_covered`` /
            ``is_feasible`` from :func:`mark_rule` (otherwise the share rule
            with ``coverage`` / ``feasible`` is applied).
        preselected: Session ids already selected by an earlier campaign
            wave. They count as covered for the per-participant target and the
            monthly cap, and are not selected again.
        target: Covered long sessions wanted per participant.
        month_cap: Most covered-or-selected long sessions per participant-month.
        budget: Campaign budget in expected annotations.
        per_participant_budget: Per-participant budget (None: no cap).
        min_plays: Plays that make a session long.
        coverage: Share of distinct videos scraped and annotated that makes a
            session covered.
        feasible: Expected coverage a candidate must reach.
        salt: Salt for the deterministic tie-breaks.

    Returns:
        ``(selected, summary)``: the selected rows of ``table`` in selection
        order (with a ``round`` column), and per-participant and per-month
        totals before and after.
    """
    long = table[table["n_plays"] >= min_plays].copy()
    if "is_covered" not in long.columns:
        long = mark_rule(long, "share", coverage, feasible)
    pre = long["session_id"].isin(set(preselected or ()))
    already = long[long["is_covered"]]
    counted = long[long["is_covered"] | pre]
    have = counted.groupby("collection_id").size().to_dict()
    month_n: dict[tuple[str, str], int] = (
        counted.groupby(["collection_id", "month"]).size().to_dict()
    )
    cands = long[long["is_feasible"] & ~pre & (long["cost"] > 0)].copy()
    tie = {k: i for i, k in enumerate(stable_rank(cands["session_id"].tolist(), salt))}
    cands["_tie"] = cands["session_id"].map(tie)
    queues = {
        cid: g.sort_values(["cost", "_tie"]).to_dict("records")
        for cid, g in cands.groupby("collection_id", sort=False)
    }
    rank = {c: i for i, c in enumerate(stable_rank(sorted(set(long["collection_id"])), salt))}
    participants = sorted(rank, key=lambda c: (have.get(c, 0), rank[c]))

    picked: list[dict] = []
    n_sel: dict[str, int] = {}
    spent_p: dict[str, float] = {}
    spent = 0.0
    rnd = 0
    exhausted = False
    while not exhausted:
        rnd += 1
        progressed = False
        for cid in participants:
            if have.get(cid, 0) + n_sel.get(cid, 0) >= target:
                continue
            q = queues.get(cid) or []
            choice = None
            for k, row in enumerate(q):
                if month_n.get((cid, row["month"]), 0) >= month_cap:
                    continue
                if (
                    per_participant_budget is not None
                    and spent_p.get(cid, 0.0) + row["cost"] > per_participant_budget
                ):
                    continue
                choice = k
                break
            if choice is None:
                continue
            row = q.pop(choice)
            if spent + row["cost"] > budget:
                exhausted = True
                break
            spent += row["cost"]
            spent_p[cid] = spent_p.get(cid, 0.0) + row["cost"]
            n_sel[cid] = n_sel.get(cid, 0) + 1
            month_n[(cid, row["month"])] = month_n.get((cid, row["month"]), 0) + 1
            picked.append({**row, "round": rnd})
            progressed = True
        if not progressed:
            break

    selected = pd.DataFrame(picked, columns=[*long.columns, "_tie", "round"]).drop(
        columns=["_tie", "is_covered", "is_feasible"]
    )
    after = {c: have.get(c, 0) + n_sel.get(c, 0) for c in participants}
    summary = {
        "participants": len(participants),
        "covered_long_before": int(len(already)),
        "preselected_long": int(pre.sum()),
        "selected_sessions": len(selected),
        "covered_long_after_if_all_succeed": int(sum(after.values())),
        "participants_at_target_before": int(
            sum(1 for c in participants if have.get(c, 0) >= target)
        ),
        "participants_at_target_after": int(sum(1 for v in after.values() if v >= target)),
        "expected_annotations": round(spent, 1),
        "videos_to_scrape": int(selected["n_to_scrape"].sum()) if len(selected) else 0,
        "videos_to_annotate_now": int(selected["n_to_annotate"].sum()) if len(selected) else 0,
        "budget_exhausted": exhausted,
        "selected_by_month": (
            selected.groupby("month").size().sort_index().to_dict() if len(selected) else {}
        ),
    }
    return selected, summary


def campaign_items(
    plays: pd.DataFrame, states: pd.DataFrame, session_ids
) -> tuple[list[str], list[str]]:
    """The selected sessions' missing videos: ``(to_scrape, to_annotate)``."""
    keep = plays["session_id"].astype(str).isin({str(s) for s in session_ids})
    ids = pd.unique(plays.loc[keep, "item_id"].astype(str))
    st = states.reindex(ids)
    to_scrape = [i for i, f in zip(ids, st["to_scrape"].fillna(True)) if f]
    to_annotate = [i for i, f in zip(ids, st["to_annotate"].fillna(False)) if f]
    return sorted(to_scrape), sorted(to_annotate)


def manifest_hash(manifest: dict) -> str:
    """SHA-256 of the manifest's selection (parameters, sessions, items)."""
    core = {
        k: manifest[k] for k in ("params", "sessions", "to_scrape", "to_annotate") if k in manifest
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()
