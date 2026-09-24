#!/usr/bin/env python3
"""Replay the TikTok export corpus through today's ingestion, one donation at a time.

The ingestion ledger only counts rows for files ingested since it existed, so
the attrition of most of the corpus is unknown. This script re-ingests every
raw TikTok export in a DOWNLOADED SNAPSHOT, in the order the files were
donated, into an empty scratch store, and records for every step:

- intake: records read per export section, records outside the parser's
  whitelist, records the parser could not read (by kind), identical share
  records merged, rows dropped for a missing required field, and whole files
  discarded for too few viewing records;
- overlap: how much of the new file's activity coincides with what was
  already stored, whether the donor merge joined it to an earlier donation,
  and how many rows the earlier files lost to it (the newest donation wins);
- unification: the activity rows each file produced, by type;
- engagement linking: for every like, bookmark, comment and share, whether
  it was folded onto an adjacent play of the same video, onto the nearest
  play of that video elsewhere in the file (with the time between them), or
  stayed unlinked, and where each comment's video id came from.

It drives the collection objects in the order ``run_ingest_refresh`` does
(load_raw -> process -> migrate_sub_collections -> per-file summary ->
ledger), without the structure sentinel and without the side effects that
follow the save (account linking, scrape queueing, study sync). Nothing is
written outside ``--out``: the snapshot is only read, and every storage
location resolves inside a scratch store under ``--out``. The replay runs the
code in this checkout, so it shows what the current pipeline does with this
donation history, not what happened when each file first arrived.

Donation time, in order of precedence (the source used is recorded per file):
the ledger's ``uploaded_at``; the UTC stamp in a generated stored name; the
AIO donation date from ``aio_participants``; the earliest
``ts_added_to_dataset`` of the file's rows in the snapshot's activity table;
the ledger's ``ts_first_seen``; the file's last event. GCS object times are
not used.

Usage:
    SNAP=~/fyp_snapshot_2026-09-17
    python scripts/replay_ingestion.py --snapshot $SNAP --out tmp/replay --limit 20
    python scripts/replay_ingestion.py --snapshot $SNAP --out tmp/replay
    python scripts/replay_ingestion.py --snapshot $SNAP --out tmp/replay_rev --order reverse \\
        --compare-with tmp/replay
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent.parent))


ROUTE_FOLDERS = {"ddp": ("ddp", "ddp_raw"), "aio": ("aio", "aio_raw")}
MANIFEST_FILENAME = "ingestion_manifest.json"
ORDER_SOURCES = ("uploaded_at", "name_stamp", "aio_date", "table_first_added",
                 "ledger_first_seen", "last_event")
NAME_STAMP_RE = re.compile(r"_(\d{8}T\d{6}Z)_[0-9a-f]{8}")
ENGAGEMENT = ("fave", "save", "comment", "share")
CHAT_PREFIX = "chat history with"
MIN_FILES_TO_NAME_SECTION = 3
PLAY_CAP_SECONDS = 600
OVERLAP_THRESHOLD = 0.2
MIN_SHARED_SECONDS = 3
DT_BINS = (("within 1 min", 60), ("1 min to 1 h", 3600), ("1 h to 1 day", 86400),
           ("1 to 30 days", 30 * 86400), ("over 30 days", None))
FILL_WINDOWS = (60, 180, 300)
SESSION_GAPS = (300, 900, 1800)
PRODUCTION_SESSION_GAP = 900
VIEWING = ("play", "observe")
GAP_BINS = (("under 10 s", 10), ("10 to 30 s", 30), ("30 to 60 s", 60), ("1 to 2 min", 120),
            ("2 to 5 min", 300), ("5 to 15 min", 900), ("15 to 30 min", 1800), ("30 to 60 min", 3600),
            ("1 to 3 h", 10800), ("over 3 h", None))
# Australian postcode ranges -> IANA zone. Broken Hill (2880) keeps South
# Australian time and Lord Howe Island (2898) its own half-hour zone.
POSTCODE_ZONES = (
    ((200, 299), "Australia/Sydney"), ((800, 999), "Australia/Darwin"),
    ((1000, 2599), "Australia/Sydney"), ((2600, 2618), "Australia/Sydney"),
    ((2619, 2879), "Australia/Sydney"), ((2880, 2880), "Australia/Broken_Hill"),
    ((2881, 2897), "Australia/Sydney"), ((2898, 2898), "Australia/Lord_Howe"),
    ((2899, 2999), "Australia/Sydney"), ((3000, 3999), "Australia/Melbourne"),
    ((4000, 4999), "Australia/Brisbane"), ((5000, 5999), "Australia/Adelaide"),
    ((6000, 6999), "Australia/Perth"), ((7000, 7999), "Australia/Hobart"),
    ((8000, 8999), "Australia/Melbourne"), ((9000, 9999), "Australia/Brisbane"),
)
STEP_COLUMNS = (
    "rank", "raw_file", "route", "order_source", "order_ts", "records", "viewing_records",
    "outside_whitelist", "not_parseable", "share_copies_merged", "missing_required",
    "processed_rows", "within_file_duplicates", "rows_replaced_in_older_files",
    "final_rows", "outcome", "copy_of_rank", "merged_with_earlier", "merged_with_ranks", "max_overlap", "max_overlap_rank",
    "files_touching", "table_rows_after", "collections_after", "census_matches_parser",
    "tokens_match", "sentinel_baseline_n", "sentinel_status", "sentinel_findings",
    "sentinel_withheld_sections", "sentinel_review", "inferred_offset", "median_utc", "tz_zone", "tz_basis", "tz_true_offset",
    "tz_diff", "tz_other_dst_pct", "seconds",
)





def parse_iso(value) -> datetime | None:
    """Parse an ISO string or datetime into an aware UTC datetime (None when unreadable)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)





def stamp_from_name(name: str) -> datetime | None:
    """The UTC upload stamp in a generated stored name (``tiktok_ddp_<stamp>_<hex>.json``)."""
    m = NAME_STAMP_RE.search(name)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)





def choose_order_time(candidates: dict[str, datetime | None]) -> tuple[datetime | None, str | None]:
    """First available donation time in ``ORDER_SOURCES`` precedence."""
    for source in ORDER_SOURCES:
        ts = candidates.get(source)
        if ts is not None:
            return ts, source
    return None, None





def donation_order(files: list[dict], order: str = "donation", seed: int = 0) -> list[dict]:
    """Rank files by donation time and assign strictly increasing replay mtimes.

    Args:
        files: One dict per raw file with ``raw_file``, ``route`` and
            ``candidates`` (source name -> datetime or None).
        order: ``donation`` (oldest first), ``reverse`` or ``shuffle``.
        seed: Shuffle seed.

    Returns:
        The files in replay order, each with ``order_ts``, ``order_source``,
        ``rank`` (1-based donation rank, whatever the replay order) and
        ``replay_mtime`` (epoch seconds, strictly increasing along the replay
        order, so the newest-wins dedup follows the replay order even where
        two donations share a timestamp).
    """
    rows = []
    for f in files:
        ts, source = choose_order_time(f["candidates"])
        rows.append({**f, "order_ts": ts, "order_source": source or "none"})
    far = datetime(2100, 1, 1, tzinfo=UTC)
    rows.sort(key=lambda r: (r["order_ts"] or far, r["raw_file"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    if order == "reverse":
        rows = rows[::-1]
    elif order == "shuffle":
        random.Random(seed).shuffle(rows)
    base = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    prev = None
    for r in rows:
        want = r["order_ts"].timestamp() if (order == "donation" and r["order_ts"]) else base
        mtime = want if prev is None else max(want, prev + 1.0)
        r["replay_mtime"] = mtime
        prev = mtime
    return rows





def zone_from_postcode(postcode, country) -> tuple[str | None, str]:
    """The donor's zone from an Australian postcode, and why when there is none.

    Returns:
        ``(zone, basis)``; ``basis`` is ``postcode``, ``outside_australia``,
        ``no_postcode`` or ``unrecognised_postcode``.
    """
    c = str(country or "").strip().lower()
    if c and c not in ("australia", "au", "aus"):
        return None, "outside_australia"
    digits = re.sub(r"\D", "", str(postcode or ""))
    if not digits:
        return None, "no_postcode"
    code = int(digits)
    if len(digits) not in (3, 4):
        return None, "unrecognised_postcode"
    for (lo, hi), zone in POSTCODE_ZONES:
        if lo <= code <= hi:
            return zone, "postcode"
    return None, "unrecognised_postcode"





def _gap_bin(seconds: float) -> str:
    for label, upper in GAP_BINS:
        if upper is None or seconds < upper:
            return label
    return GAP_BINS[-1][0]





def _session_ids(collection: np.ndarray, ts_seconds: np.ndarray, gap: int) -> np.ndarray:
    """Session number per row of a frame sorted by collection then time."""
    new_collection = np.r_[True, collection[1:] != collection[:-1]]
    step = np.r_[np.inf, np.diff(ts_seconds)]
    return np.cumsum(new_collection | (step > gap))





def session_census(df: pd.DataFrame, gaps: tuple[int, ...] = SESSION_GAPS,
                   production_gap: int = PRODUCTION_SESSION_GAP) -> dict:
    """How sessions come out of the table, and how they depend on what counts as activity.

    ``assign_session_ids`` splits each collection's rows at any gap over
    ``production_gap`` seconds, counting the donor's own rows: plays, likes,
    logins, follows (``without_followed_by`` here). Until the replay showed
    ``followed_by`` rows, another account following the donor, joining
    separate sittings, it counted every row (``all_rows``). This reports
    sessions under those two definitions and over viewing rows only, at
    each gap, how many viewing sittings the non-viewing rows join together,
    and the distribution of the gaps between consecutive viewing events.

    Args:
        df: Rows with ``collection_id``, ``utc_timestamp`` and ``activity_type``.
        gaps: Thresholds in seconds.
        production_gap: The configured threshold.

    Returns:
        A JSON-serialisable dict.
    """
    frame = pd.DataFrame({
        "c": df["collection_id"].astype(str).to_numpy(),
        "t": pd.to_datetime(df["utc_timestamp"], utc=True).astype("int64").to_numpy() // 1_000_000_000,
        "a": df["activity_type"].astype("string").fillna("").to_numpy(),
    }).sort_values(["c", "t"], kind="mergesort").reset_index(drop=True)
    is_view = frame["a"].isin(VIEWING).to_numpy()
    definitions = {
        "all_rows": np.ones(len(frame), dtype=bool),
        "without_followed_by": (frame["a"] != "followed_by").to_numpy(),
        "viewing_only": is_view,
    }
    out: dict = {"rows": len(frame), "viewing_rows": int(is_view.sum()),
                 "collections": int(frame["c"].nunique()), "definitions": {}}
    view_ids_by_def: dict[str, np.ndarray] = {}
    for label, mask in definitions.items():
        sub = frame[mask]
        c, t = sub["c"].to_numpy(), sub["t"].to_numpy()
        view = sub["a"].isin(VIEWING).to_numpy()
        per_gap = {}
        for gap in gaps:
            sid = _session_ids(c, t, gap)
            sessions = pd.DataFrame({"sid": sid, "c": c, "t": t, "v": view})
            g = sessions.groupby("sid")
            n_view = g["v"].sum()
            span = g["t"].max() - g["t"].min()
            has_view = n_view > 0
            per_c = sessions[sessions["v"]].groupby("c")["sid"].nunique()
            per_gap[str(gap)] = {
                "sessions": len(n_view),
                "sessions_without_viewing": int((~has_view).sum()),
                "viewing_sessions": int(has_view.sum()),
                "single_view_sessions_pct": round(100.0 * float((n_view[has_view] == 1).mean()), 1)
                if has_view.any() else None,
                "median_views_per_session": float(n_view[has_view].median()) if has_view.any() else None,
                "median_duration_s": float(span[has_view].median()) if has_view.any() else None,
                "p90_duration_s": float(span[has_view].quantile(0.9)) if has_view.any() else None,
                "median_sessions_per_collection": float(per_c.median()) if len(per_c) else None,
            }
            if gap == production_gap:
                view_ids_by_def[label] = sid[view]
        out["definitions"][label] = per_gap
    view_only = view_ids_by_def["viewing_only"]
    for label in ("all_rows", "without_followed_by"):
        joined = pd.Series(view_only).groupby(view_ids_by_def[label]).nunique()
        out[f"viewing_sittings_joined_by_{label}"] = int((joined - 1).sum())
    views = frame[is_view]
    same = views["c"].to_numpy()[1:] == views["c"].to_numpy()[:-1]
    gaps_s = np.diff(views["t"].to_numpy())[same]
    out["viewing_gap_bins"] = {label: int(n) for label, n in
                               Counter(_gap_bin(float(g)) for g in gaps_s).items()}
    out["viewing_gaps"] = len(gaps_s)
    return out





def sentinel_summary(steps: list[dict]) -> dict:
    """What the sentinel said about each file, learned from nothing in donation order."""
    evaluated = [s for s in steps if s.get("sentinel_status")]
    by_status = Counter(s["sentinel_status"] for s in evaluated)
    codes: Counter = Counter()
    flagged = []
    for s in evaluated:
        for f in filter(None, str(s.get("sentinel_findings") or "").split(";")):
            codes[f] += 1
        if s["sentinel_status"] in ("warn", "quarantined"):
            flagged.append({k: s.get(k) for k in ("rank", "route", "sentinel_status", "sentinel_findings",
                                                  "sentinel_baseline_n", "copy_of_rank", "records",
                                                  "processed_rows", "outcome", "sentinel_review")})
    return {
        "files_evaluated": len(evaluated),
        "by_status": dict(by_status),
        "past_learning": sum(1 for s in evaluated if s["sentinel_status"] != "learning"),
        "findings": dict(codes.most_common()),
        "withheld_sections_files": sum(1 for s in evaluated if (s.get("sentinel_withheld_sections") or 0) > 0),
        "flagged": flagged,
    }





def tz_summary(steps: list[dict]) -> dict:
    """The inferred offset against the zone of the donor's postcode, per distinct file."""
    rows = [s for s in steps if s.get("tz_zone") and s.get("tz_diff") is not None and not s.get("copy_of_rank")]
    basis = Counter(s.get("tz_basis") or "no_participant_record" for s in steps
                    if s["route"] == "aio" and not s.get("copy_of_rank") and s["processed_rows"])
    diffs = [float(s["tz_diff"]) for s in rows]
    return {
        "aio_files_by_location_basis": dict(basis),
        "files_calibrated": len(rows),
        "by_zone": dict(Counter(s["tz_zone"] for s in rows)),
        "agree": sum(1 for d in diffs if abs(d) < 0.25),
        "off_by_half_hour": sum(1 for d in diffs if 0.25 <= abs(d) < 0.75),
        "off_by_one_hour": sum(1 for d in diffs if 0.75 <= abs(d) <= 1.25),
        "off_by_more": sum(1 for d in diffs if abs(d) > 1.25),
        "diff_hours": dict(Counter(round(d, 1) for d in diffs)),
        "rows_in_other_dst_half_pct": _dist([float(s["tz_other_dst_pct"]) for s in rows
                                             if s.get("tz_other_dst_pct") is not None]),
    }





def copy_of_earlier(rows: list[dict]) -> dict[str, int]:
    """Rank of the earliest donation with byte-identical content, for every later copy.

    Args:
        rows: ``donation_order`` output; each row carries ``sha256``.

    Returns:
        ``{raw_file: rank of the first file with the same content}`` for
        files that repeat an earlier donation byte for byte.
    """
    first: dict[str, int] = {}
    out: dict[str, int] = {}
    for r in sorted(rows, key=lambda r: r["rank"]):
        h = r.get("sha256")
        if not h:
            continue
        if h in first:
            out[r["raw_file"]] = first[h]
        else:
            first[h] = r["rank"]
    return out





def order_agreement(rows: list[dict]) -> dict:
    """How the ordering sources agree where a file has more than one.

    Returns the per-source file counts, and for each pair of sources the
    number of files carrying both and the median and largest absolute gap in
    days. Also counts files whose chosen time falls more than five minutes
    after their first appearance in the snapshot's table, which no true
    donation time can.
    """
    used = Counter(r["order_source"] for r in rows)
    pairs: dict[str, dict] = {}
    for i, a in enumerate(ORDER_SOURCES):
        for b in ORDER_SOURCES[i + 1:]:
            gaps = [abs((r["candidates"][a] - r["candidates"][b]).total_seconds()) / 86400
                    for r in rows if r["candidates"].get(a) and r["candidates"].get(b)]
            if gaps:
                pairs[f"{a}~{b}"] = {"files": len(gaps), "median_days": statistics.median(gaps),
                                     "max_days": max(gaps)}
    after_table = sum(1 for r in rows if r["order_ts"] and r["candidates"].get("table_first_added")
                      and r["order_ts"] > r["candidates"]["table_first_added"] + timedelta(minutes=5))
    return {"source_used": dict(used), "pairs": pairs, "chosen_after_first_in_table": after_table}





def section_label(name: str) -> str:
    """Section name safe to report: direct-message sections are named after a username."""
    return f"{CHAT_PREFIX} …" if CHAT_PREFIX in (name or "") else (name or "")





def section_census(records: list[dict], parser_cls) -> dict:
    """Count one export's records by section and by what the parser will do with them.

    Follows the order of ``TikTokDDPCollection.process_single``: the
    whitelist filter, the date-first check, the direct-message filter, the
    timestamp parse, the identical-share collapse and the play-without-video
    drop. The replay checks the predicted counts against the ledger's.

    Args:
        records: ``parser_cls._walk_sections(export)`` output.
        parser_cls: The TikTok DDP parser class (its whitelist and unpacker).

    Returns:
        ``records``, ``viewing_records`` (the floor's count), ``by_section``
        ({label: n}), ``section_fate`` ({label: activity type, "login" or
        "outside_whitelist"}), ``outside_whitelist``, ``not_parseable`` by
        kind, ``share_copies_merged``, ``kept_by_type`` and ``last_event``.
    """
    whitelist = parser_cls._ACTIVITY_TYPE_MAP
    by_section: Counter = Counter()
    fate: dict[str, Counter] = defaultdict(Counter)
    outside = 0
    fail: Counter = Counter()
    kept: Counter = Counter()
    share_seen: Counter = Counter()
    share_copies = 0
    viewing = 0
    last_event = None
    for rec in records:
        name = rec["activity_type"] or ""
        label = section_label(name)
        variables = rec["variable_list"]
        values = rec["value_list"]
        by_section[label] += 1
        if name == "videolist":
            viewing += 1
        is_login = len(variables) > 1 and variables[1] == "ip"
        if name not in whitelist and not is_login:
            outside += 1
            fate[label]["outside_whitelist"] += 1
            continue
        if not (len(variables) > 1 and variables[0] == "date"):
            fail["no_leading_date"] += 1
            continue
        if CHAT_PREFIX in name:
            fail["direct_message"] += 1
            continue
        when = pd.to_datetime(parser_cls._strip_zone_suffix(values[0]), format=parser_cls._DATE_FORMAT,
                              errors="coerce")
        if pd.isna(when):
            fail["unreadable_date"] += 1
            continue
        if name in parser_cls._SHARE_SECTIONS:
            key = name + "\x1f" + "\x1f".join(map(str, values))
            share_seen[key] += 1
            if share_seen[key] > 1:
                share_copies += 1
                continue
        primary, _extra, link, _context = parser_cls._unpack_record(variables, values)
        atype = "login" if primary == "ip" else whitelist.get(name)
        if atype == "play" and not (link and parser_cls._VIDEO_ID_RE.search(link)):
            fail["play_without_video_id"] += 1
            continue
        fate[label][atype] += 1
        kept[atype] += 1
        last_event = when if last_event is None or when > last_event else last_event
    section_fate = {label: c.most_common(1)[0][0] for label, c in fate.items()}
    return {
        "records": sum(by_section.values()),
        "viewing_records": viewing,
        "by_section": dict(by_section),
        "section_fate": section_fate,
        "outside_whitelist": outside,
        "not_parseable": dict(fail),
        "share_copies_merged": share_copies,
        "kept_by_type": dict(kept),
        "last_event": last_event.tz_localize(UTC).to_pydatetime() if last_event is not None else None,
    }





def _dt_bin(seconds: float) -> str:
    for label, upper in DT_BINS:
        if upper is None or seconds < upper:
            return label
    return DT_BINS[-1][0]





def link_census(df: pd.DataFrame, cap_seconds: int = PLAY_CAP_SECONDS) -> dict:
    """Classify every engagement row of one file the way ``derive_play_duration`` folds it.

    Run on the frame exactly as ``derive_play_duration`` receives it (one
    raw file, chronological, comment ids already forward-filled).

    Args:
        df: Pre-fold frame with ``utc_timestamp``, ``activity_type``,
            ``item_id`` and optionally ``link_method``.
        cap_seconds: The play-duration cap.

    Returns:
        ``by_type`` ({engagement type: {rows, no_item_id, adjacent,
        nearest_play, before_watch_history, no_play_of_item}}), where
        ``before_watch_history`` is a row whose item was played in the file
        but which predates the file's first play, so the fallback leaves it; ``nearest_dt_seconds`` and
        ``nearest_dt_by_type`` for nearest-play links; ``comments``
        (observed / filled / no id, before the file's first play, the
        fill's reach and the type of row it borrowed from); ``plays`` (lead
        of a run, folded repeats, over the cap, last row); and
        ``tokens_expected``, the tokens the fold writes onto plays.
    """
    df = df.reset_index(drop=True)
    n = len(df)
    atype = df["activity_type"].astype("string")
    item = df["item_id"].astype("string")
    ts = pd.to_datetime(df["utc_timestamp"], utc=True)
    has_item = item.notna().to_numpy()
    item_arr = item.fillna("").to_numpy(dtype=object)
    same_prev = np.zeros(n, dtype=bool)
    if n > 1:
        same_prev[1:] = has_item[1:] & has_item[:-1] & (item_arr[1:] == item_arr[:-1])
    same_next = np.zeros(n, dtype=bool)
    if n > 1:
        same_next[:-1] = same_prev[1:]
    in_run = same_prev | same_next
    run_id = np.cumsum(~same_prev)
    is_play = (atype == "play").fillna(False).to_numpy()
    first_play_of_run: dict[int, int] = {}
    for i in np.flatnonzero(in_run & is_play):
        first_play_of_run.setdefault(int(run_id[i]), int(i))
    lead = np.zeros(n, dtype=bool)
    lead[list(first_play_of_run.values())] = True
    run_has_play = in_run & np.isin(run_id, list(first_play_of_run))
    adjacent = run_has_play & ~lead & atype.notna().to_numpy()

    plays_by_item: dict[str, list] = defaultdict(list)
    for i in np.flatnonzero(is_play & has_item):
        plays_by_item[item_arr[i]].append(ts.iat[i])

    first_play = ts[is_play].min() if is_play.any() else None
    by_type = {t: Counter() for t in ENGAGEMENT}
    nearest_dt: list[float] = []
    nearest_by_type: dict[str, list[float]] = {t: [] for t in ENGAGEMENT}
    for i in range(n):
        t = atype.iat[i]
        if t is pd.NA or t not in by_type:
            continue
        c = by_type[t]
        c["rows"] += 1
        if not has_item[i]:
            c["no_item_id"] += 1
        elif adjacent[i]:
            c["adjacent"] += 1
        elif plays_by_item.get(item_arr[i]) and first_play is not None and ts.iat[i] < first_play:
            c["before_watch_history"] += 1
        elif plays_by_item.get(item_arr[i]):
            c["nearest_play"] += 1
            gap = min(abs((p - ts.iat[i]).total_seconds()) for p in plays_by_item[item_arr[i]])
            nearest_dt.append(gap)
            nearest_by_type[t].append(gap)
        else:
            c["no_play_of_item"] += 1

    if "link_method" in df.columns:
        filled_mask = (df["link_method"].astype("string") == "ffill_180s").fillna(False).to_numpy()
    else:
        filled_mask = np.zeros(n, dtype=bool)
    is_comment = (atype == "comment").fillna(False).to_numpy()
    filled = is_comment & filled_mask
    observed = is_comment & has_item & ~filled
    first_play_ts = ts[is_play].min() if is_play.any() else None
    if first_play_ts is not None:
        before_first = int((is_comment & (ts < first_play_ts).to_numpy()).sum())
    else:
        before_first = int(is_comment.sum())
    reach: list[float] = []
    source_types: Counter = Counter()
    source_rows = has_item & ~filled
    last_source: dict[str, int] = {}
    for i in range(n):
        if filled[i]:
            j = last_source.get(item_arr[i])
            if j is not None:
                reach.append((ts.iat[i] - ts.iat[j]).total_seconds())
                source_types[str(atype.iat[j])] += 1
        if source_rows[i]:
            last_source[item_arr[i]] = i

    # Ground truth for the fill: comments whose export names the video. For
    # each, the id the forward fill would have borrowed had the export not
    # named it (the last earlier row with an observed id in the same burst),
    # at several burst gaps.
    fill_check: dict[str, Counter] = {}
    if observed.any():
        secs = ts.astype("int64").to_numpy() // 1_000_000_000
        step = np.r_[np.inf, np.diff(secs)]
        for window in FILL_WINDOWS:
            burst = np.cumsum(step > window)
            c = Counter()
            last_id, last_burst = None, None
            for i in range(n):
                if burst[i] != last_burst:
                    last_id, last_burst = None, burst[i]
                if observed[i]:
                    if last_id is None:
                        c["no_fill"] += 1
                    elif last_id == item_arr[i]:
                        c["same_video"] += 1
                    else:
                        c["different_video"] += 1
                if source_rows[i]:
                    last_id = item_arr[i]
            fill_check[str(window)] = c

    forward = ts.diff().dt.total_seconds().shift(-1).to_numpy()
    solo_play = is_play & ~run_has_play
    plays = {
        "plays": int(is_play.sum()),
        "leading_a_run": int(lead.sum()),
        "folded_repeat_plays": int((adjacent & is_play).sum()),
        "solo_plays": int(solo_play.sum()),
        "solo_last_row": int(n > 0 and solo_play[-1]),
        "solo_over_cap": int((solo_play & (forward > cap_seconds)).sum()),
    }
    return {
        "by_type": {t: dict(c) for t, c in by_type.items()},
        "nearest_dt_seconds": nearest_dt,
        "nearest_dt_by_type": nearest_by_type,
        "comments": {
            "comments": int(is_comment.sum()),
            "observed_id": int(observed.sum()),
            "filled_id": int(filled.sum()),
            "no_id": int((is_comment & ~has_item).sum()),
            "before_first_play": before_first,
            "fill_reach_seconds": reach,
            "fill_source_types": dict(source_types),
            "fill_check": {w: dict(c) for w, c in fill_check.items()},
        },
        "plays": plays,
        "tokens_expected": int(adjacent.sum()) + len(nearest_dt),
    }





def tokens_written(df: pd.DataFrame) -> int:
    """Tokens ``derive_play_duration`` wrote onto plays (the check on ``link_census``)."""
    if "link_method" not in df.columns or len(df) == 0:
        return 0
    lm = df["link_method"].astype("string")
    folded = ((df["activity_type"].astype("string") == "play").fillna(False)
              & lm.str.contains("adjacent|nearest_play", regex=True).fillna(False))
    if not folded.any():
        return 0
    return int(df.loc[folded, "extra_data"].astype("string").fillna("").str.count(",").add(1).sum())





def prior_overlap(new_seconds: set[int], prior: dict[str, set[int]],
                  threshold: float = OVERLAP_THRESHOLD, min_shared: int = MIN_SHARED_SECONDS) -> dict:
    """Second-level overlap of a new file with every file already stored.

    The statistic ``identify_similar_file_content`` clusters on: shared
    distinct seconds over the smaller file's distinct seconds.

    Returns:
        ``files_touching`` (sharing at least one second), ``would_cluster``
        (files over the threshold with at least ``min_shared`` seconds),
        ``max_overlap`` and ``max_partner``.
    """
    best, partner, touching, cluster = 0.0, None, 0, []
    for name, secs in prior.items():
        if not secs or not new_seconds:
            continue
        shared = len(new_seconds & secs)
        if shared == 0:
            continue
        touching += 1
        ratio = shared / min(len(new_seconds), len(secs))
        if ratio > best:
            best, partner = ratio, name
        if shared >= min_shared and ratio > threshold:
            cluster.append(name)
    return {"files_touching": touching, "would_cluster": cluster, "max_overlap": best, "max_partner": partner}





def seconds_of(frame: pd.DataFrame) -> dict[str, set[int]]:
    """Distinct whole seconds per raw file, as the donor merge builds them."""
    if len(frame) == 0:
        return {}
    secs = pd.to_datetime(frame["utc_timestamp"], utc=True).astype("int64").to_numpy() // 1_000_000_000
    names = frame["raw_file"].astype(str).to_numpy()
    out: dict[str, set[int]] = defaultdict(set)
    for name, sec in zip(names, secs, strict=True):
        out[name].add(int(sec))
    return dict(out)





def _sum_counters(dicts) -> dict:
    total: Counter = Counter()
    for d in dicts:
        total.update({k: int(v) for k, v in (d or {}).items()})
    return dict(total)





def _dist(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    arr = np.asarray(values, dtype=float)
    return {"n": int(arr.size), "median": float(np.median(arr)), "p90": float(np.percentile(arr, 90)),
            "max": float(arr.max()), "bins": dict(Counter(_dt_bin(v) for v in arr))}





def aggregate(steps: list[dict], census: dict[str, dict], links: dict[str, dict],
              unification: dict[str, dict], order_rows: list[dict], files_seen: dict[str, int]) -> dict:
    """Fold the per-step records into the figures the paper reports.

    Args:
        steps: One record per replayed file (``STEP_COLUMNS``).
        census: ``section_census`` per raw file.
        links: ``link_census`` per raw file (files that reached the fold).
        unification: Rows produced per activity type, per raw file.
        order_rows: ``donation_order`` output for the replayed files.
        files_seen: Number of files each section label appears in.

    Returns:
        A JSON-serialisable report.
    """
    by_route: dict[str, list[dict]] = defaultdict(list)
    for s in steps:
        by_route[s["route"]].append(s)
        by_route["all"].append(s)

    def intake(rows: list[dict]) -> dict:
        names = [r["raw_file"] for r in rows]
        too_small = [r for r in rows if r["outcome"] == "discarded_at_load"]
        failed = [r for r in rows if r["outcome"] == "load_failed"]
        return {
            "files": len(rows),
            "outcomes": dict(Counter(r["outcome"] for r in rows)),
            "records": sum(r["records"] for r in rows),
            "outside_whitelist": sum(r["outside_whitelist"] for r in rows),
            "not_parseable": sum(r["not_parseable"] for r in rows),
            "not_parseable_by_kind": _sum_counters(census[n]["not_parseable"] for n in names
                                                   if n in census and n not in {t["raw_file"] for t in too_small}),
            "share_copies_merged": sum(r["share_copies_merged"] for r in rows),
            "missing_required": sum(r["missing_required"] for r in rows),
            "too_small_files": len(too_small),
            "too_small_records": sum(r["records"] for r in too_small),
            "load_failed_files": len(failed),
            "load_failed_records": sum(r["records"] for r in failed),
            "processed_rows": sum(r["processed_rows"] for r in rows),
            "within_file_duplicates": sum(r["within_file_duplicates"] for r in rows),
            "rows_replaced_in_older_files": sum(r["rows_replaced_in_older_files"] for r in rows),
            "census_mismatches": sum(1 for r in rows if r["census_matches_parser"] is False),
            "token_mismatches": sum(1 for r in rows if r["tokens_match"] is False),
        }

    report: dict = {"intake": {route: intake(rows) for route, rows in by_route.items()}}

    last = max(steps, key=lambda s: s["_step"]) if steps else {}
    report["table"] = {"rows": last.get("table_rows_after", 0), "collections": last.get("collections_after", 0)}

    merged = [s for s in steps if s["merged_with_earlier"]]
    rank_ts = {r["rank"]: r["order_ts"] for r in order_rows}
    donated_gaps = []
    for s in merged:
        mine = rank_ts.get(s["rank"])
        others = [rank_ts.get(int(x)) for x in str(s["merged_with_ranks"] or "").split(";") if x.isdigit()]
        others = [o for o in others if o and mine and o <= mine]
        if others:
            donated_gaps.append((mine - max(others)).total_seconds() / 86400)
    replaced_share = [s["rows_replaced_in_older_files"] / s["processed_rows"]
                      for s in merged if s["processed_rows"]]
    copies = [s for s in steps if s.get("copy_of_rank")]
    genuine = [s for s in merged if not s.get("copy_of_rank")]
    report["overlap"] = {
        "distinct_contents": len(steps) - len(copies),
        "byte_copies_of_an_earlier_file": len(copies),
        "byte_copies_merged": sum(1 for s in copies if s["merged_with_earlier"]),
        "byte_copies_discarded_too_small": sum(1 for s in copies if s["outcome"] == "discarded_at_load"),
        "rows_superseded_by_byte_copies": sum(s["rows_replaced_in_older_files"] for s in copies),
        "redonations_with_new_content": len(genuine),
        "rows_superseded_by_new_content_files": sum(s["rows_replaced_in_older_files"] for s in genuine),
        "new_rows_added_by_new_content_files": sum(max(s["final_rows"] - s["rows_replaced_in_older_files"], 0)
                                                   for s in genuine),
        "files_merged_with_earlier": len(merged),
        "merged_but_summary_says_added_as_new": sum(1 for s in merged if s["outcome"] == "added_as_new"),
        "files_fully_deduplicated": sum(1 for s in steps if s["outcome"] == "fully_deduped"),
        "files_touching_any_earlier": sum(1 for s in steps if s["files_touching"]),
        "days_since_previous_donation": _dist(donated_gaps),
        "share_of_new_rows_already_held": _dist(replaced_share),
        "max_overlap_of_unmerged_files": _dist([s["max_overlap"] for s in steps if s["final_rows"]
                                                and not s["merged_with_earlier"] and s["files_touching"]]),
        "unmerged_files_over_threshold": sum(1 for s in steps if s["final_rows"] and not s["merged_with_earlier"]
                                             and s["max_overlap"] > OVERLAP_THRESHOLD),
    }

    section_totals: Counter = Counter()
    section_fate: dict[str, str] = {}
    for c in census.values():
        section_totals.update(c["by_section"])
        for label, f in c["section_fate"].items():
            section_fate.setdefault(label, f)
    named = {}
    other = {"records": 0, "sections": 0}
    for label, n in section_totals.most_common():
        if files_seen.get(label, 0) >= MIN_FILES_TO_NAME_SECTION:
            named[label] = {"records": n, "files": files_seen[label], "fate": section_fate.get(label, "-")}
        else:
            other["records"] += n
            other["sections"] += 1
    report["sections"] = {"named": named, "rare_sections": other}
    report["unification"] = {"rows_by_type": _sum_counters(unification.values()), "files": len(unification)}

    by_type = {t: Counter() for t in ENGAGEMENT}
    nearest_by_type: dict[str, list[float]] = {t: [] for t in ENGAGEMENT}
    comment_totals: Counter = Counter()
    reach: list[float] = []
    fill_sources: Counter = Counter()
    play_totals: Counter = Counter()
    fill_check: dict[str, Counter] = defaultdict(Counter)
    for lc in links.values():
        for w, c in (lc["comments"].get("fill_check") or {}).items():
            fill_check[w].update(c)
        for t in ENGAGEMENT:
            by_type[t].update(lc["by_type"].get(t, {}))
            nearest_by_type[t].extend(lc["nearest_dt_by_type"].get(t, []))
        comment_totals.update({k: v for k, v in lc["comments"].items() if isinstance(v, int)})
        reach.extend(lc["comments"]["fill_reach_seconds"])
        fill_sources.update(lc["comments"]["fill_source_types"])
        play_totals.update(lc["plays"])
    report["linking"] = {
        "by_type": {t: dict(c) for t, c in by_type.items()},
        "nearest_dt_by_type": {t: _dist(v) for t, v in nearest_by_type.items()},
        "nearest_dt_all": _dist([v for vs in nearest_by_type.values() for v in vs]),
        "nearest_bins_by_type": {t: _dist(v).get("bins", {}) for t, v in nearest_by_type.items()},
        "comments": dict(comment_totals),
        "fill_reach": _dist(reach),
        "fill_reach_over_180s": sum(1 for r in reach if r > 180),
        "fill_source_types": dict(fill_sources),
        "fill_check_on_observed_ids": {w: dict(c) for w, c in sorted(fill_check.items(), key=lambda kv: int(kv[0]))},
        "plays": dict(play_totals),
    }
    report["order"] = order_agreement(order_rows)
    return report





def fidelity(prod_rows: dict[str, int], prod_cid: dict[str, str],
             replay_rows: dict[str, int], replay_cid: dict[str, str]) -> dict:
    """Compare the replayed table with the production table file by file.

    Returns counts of files in both or only one, files whose kept rows match
    exactly, the summed absolute row difference, and agreement on which
    pairs of files share a collection.
    """
    both = sorted(set(prod_rows) & set(replay_rows))
    diffs = [replay_rows[f] - prod_rows[f] for f in both]

    def pairs(cid: dict[str, str], files: list[str]) -> set[tuple[str, str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for f in files:
            if cid.get(f) is not None:
                groups[cid[f]].append(f)
        return {tuple(sorted((a, b))) for g in groups.values() for i, a in enumerate(g) for b in g[i + 1:]}

    p_prod, p_replay = pairs(prod_cid, both), pairs(replay_cid, both)
    return {
        "files_in_both": len(both),
        "only_in_production": len(set(prod_rows) - set(replay_rows)),
        "only_in_replay": len(set(replay_rows) - set(prod_rows)),
        "rows_production": sum(prod_rows[f] for f in both),
        "rows_replay": sum(replay_rows[f] for f in both),
        "files_exact": sum(1 for d in diffs if d == 0),
        "files_more_in_replay": sum(1 for d in diffs if d > 0),
        "files_fewer_in_replay": sum(1 for d in diffs if d < 0),
        "abs_row_difference": int(sum(abs(d) for d in diffs)),
        "merged_pairs_production": len(p_prod),
        "merged_pairs_replay": len(p_replay),
        "merged_pairs_both": len(p_prod & p_replay),
    }





LEGACY_TYPE_NAMES = {"following": "follow"}





def rows_by_type_comparison(prod: dict[str, int], replay: dict[str, int]) -> dict:
    """Whole-table rows by activity type, production against replay.

    The snapshot may predate the engagement-vocabulary migration, so legacy
    names are mapped forward and likes and bookmarks are also compared
    together (``fave+save``): before the migration TikTok bookmarks were
    stored as ``fave``.
    """
    p: Counter = Counter()
    for t, n in prod.items():
        p[LEGACY_TYPE_NAMES.get(t, t)] += int(n)
    r = Counter({t: int(n) for t, n in replay.items()})
    for c in (p, r):
        c["fave+save"] = c.get("fave", 0) + c.get("save", 0)
    types = sorted(set(p) | set(r), key=lambda t: -max(p.get(t, 0), r.get(t, 0)))
    return {t: {"production": p.get(t, 0), "replay": r.get(t, 0), "difference": r.get(t, 0) - p.get(t, 0)}
            for t in types}





def compare_runs(a: list[dict], b: list[dict]) -> dict:
    """Order dependence: compare two runs' final per-file rows and collections."""
    ra = {r["raw_file"]: r for r in a}
    rb = {r["raw_file"]: r for r in b}
    both = set(ra) & set(rb)

    def groups(rows: dict[str, dict]) -> set[frozenset]:
        g: dict[str, set] = defaultdict(set)
        for f, r in rows.items():
            if r.get("collection_id"):
                g[r["collection_id"]].add(f)
        return {frozenset(v) for v in g.values()}

    ga = groups({f: ra[f] for f in both})
    gb = groups({f: rb[f] for f in both})
    return {
        "table_rows": [sum(int(r["final_rows"]) for r in a), sum(int(r["final_rows"]) for r in b)],
        "files": [len(ra), len(rb)],
        "files_with_different_rows": sum(1 for f in both if int(ra[f]["final_rows"]) != int(rb[f]["final_rows"])),
        "collections": [len(ga), len(gb)],
        "identical_collections": len(ga & gb),
    }





def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.1f}" if abs(value) >= 100 else f"{value:.3g}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)





def _md_table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_fmt(v) for v in row) + " |")
    return "\n".join(lines)





def _pct(part: float, whole: float) -> float | None:
    return 100.0 * part / whole if whole else None





def render_tables_md(report: dict) -> str:
    """The replay's figures as markdown tables, one section per paper table."""
    out = ["# Replay of the TikTok export corpus", ""]
    intake = report["intake"]
    routes = [r for r in ("ddp", "aio", "all") if r in intake]
    keys = [("Files", "files"), ("Records read", "records"), ("Outside the whitelist", "outside_whitelist"),
            ("Not parseable", "not_parseable"), ("Identical shares merged", "share_copies_merged"),
            ("Missing a required field", "missing_required"), ("Too-small files", "too_small_files"),
            ("Records in too-small files", "too_small_records"), ("Unreadable files", "load_failed_files"),
            ("Rows produced", "processed_rows"), ("Duplicates within a file", "within_file_duplicates"),
            ("Rows superseded in older files", "rows_replaced_in_older_files"),
            ("Census/parser mismatches", "census_mismatches"), ("Fold-token mismatches", "token_mismatches")]
    out += ["## Intake by route", "",
            _md_table(["", *routes], [[label, *[intake[r][k] for r in routes]] for label, k in keys]), "",
            "Outcomes: " + "; ".join(f"{r}: {json.dumps(intake[r]['outcomes'])}" for r in routes), "",
            "Not parseable by kind (all): " + json.dumps(intake["all"]["not_parseable_by_kind"]), ""]
    t = report["table"]
    out += [f"Final table: {t['rows']:,} rows in {t['collections']:,} collections.", ""]

    o = report["overlap"]
    out += ["## Overlap with earlier donations", "",
            _md_table(["Measure", "Value"], [
                ["Distinct file contents", o["distinct_contents"]],
                ["Byte-identical copies of an earlier file", o["byte_copies_of_an_earlier_file"]],
                ["... merged with it", o["byte_copies_merged"]],
                ["... discarded as too small", o["byte_copies_discarded_too_small"]],
                ["Rows superseded by byte copies", o["rows_superseded_by_byte_copies"]],
                ["Re-donations with new content", o["redonations_with_new_content"]],
                ["Rows they superseded", o["rows_superseded_by_new_content_files"]],
                ["Rows they added", o["new_rows_added_by_new_content_files"]],
                ["Files merged with an earlier donation", o["files_merged_with_earlier"]],
                ["... of which the per-file summary calls added_as_new", o["merged_but_summary_says_added_as_new"]],
                ["Files fully deduplicated", o["files_fully_deduplicated"]],
                ["Files sharing any second with an earlier file", o["files_touching_any_earlier"]],
                ["Median days since the previous donation (merged)", o["days_since_previous_donation"].get("median")],
                ["Median share of a merged file's rows already held",
                 o["share_of_new_rows_already_held"].get("median")],
                ["Median overlap of unmerged files that touch", o["max_overlap_of_unmerged_files"].get("median")],
                ["Max overlap of unmerged files", o["max_overlap_of_unmerged_files"].get("max")],
                ["Unmerged files over the threshold", o["unmerged_files_over_threshold"]],
            ]), ""]

    rows = [[label, v["files"], v["records"], v["fate"]] for label, v in report["sections"]["named"].items()]
    rare = report["sections"]["rare_sections"]
    rows.append([f"{rare['sections']} sections in fewer than {MIN_FILES_TO_NAME_SECTION} files", "-",
                 rare["records"], "-"])
    out += ["## Export sections and what they became", "",
            _md_table(["Section", "Files", "Records", "Became"], rows), "",
            "Rows produced by type: " + json.dumps(report["unification"]["rows_by_type"]), ""]

    lk = report["linking"]
    rows = []
    for t, c in lk["by_type"].items():
        n = c.get("rows", 0)
        rows.append([t, n, c.get("adjacent", 0), _pct(c.get("adjacent", 0), n), c.get("nearest_play", 0),
                     _pct(c.get("nearest_play", 0), n), c.get("before_watch_history", 0),
                     _pct(c.get("before_watch_history", 0), n), c.get("no_play_of_item", 0),
                     _pct(c.get("no_play_of_item", 0), n), c.get("no_item_id", 0),
                     _pct(c.get("no_item_id", 0), n), lk["nearest_dt_by_type"][t].get("median")])
    c = lk["comments"]
    out += ["## Engagement linking", "",
            _md_table(["Type", "Rows", "Adjacent", "%", "Nearest play", "%", "Before watch history", "%",
                       "No play of item", "%", "No video id", "%", "Median gap to nearest play (s)"], rows), "",
            "Nearest-play gap bins (all types): " + json.dumps(lk["nearest_dt_all"].get("bins", {})), "",
            _md_table(["Comments", "Value"], [
                ["Comments", c.get("comments")], ["Video id in the export", c.get("observed_id")],
                ["Video id filled from the burst", c.get("filled_id")], ["No video id", c.get("no_id")],
                ["Before the file's first play", c.get("before_first_play")],
                ["Median reach of a fill (s)", lk["fill_reach"].get("median")],
                ["Fills reaching back over 180 s", lk["fill_reach_over_180s"]],
                ["Fill source types", json.dumps(lk["fill_source_types"])],
            ]), "",
            "The fill checked on comments whose export names the video (what it would have borrowed):", "",
            _md_table(["Burst gap (s)", "Same video", "Different video", "No fill"],
                      [[w, v.get("same_video", 0), v.get("different_video", 0), v.get("no_fill", 0)]
                       for w, v in lk["fill_check_on_observed_ids"].items()]), "",
            "Plays: " + json.dumps(lk["plays"]), ""]

    if "sentinel" in report:
        sn = report["sentinel"]
        out += ["## Structure sentinel (learning from the first donation)", "",
                f"Files evaluated: {sn['files_evaluated']}; by status: {json.dumps(sn['by_status'])}; "
                f"past learning: {sn['past_learning']}; files with withheld sections: {sn['withheld_sections_files']}", "",
                _md_table(["Finding (layer:code:severity)", "Files"], [[k, v] for k, v in sn["findings"].items()]), "",
                _md_table(["Rank", "Route", "Status", "Findings", "Baseline n", "Copy of", "Records", "Outcome", "Review"],
                          [[f["rank"], f["route"], f["sentinel_status"], f["sentinel_findings"],
                            f["sentinel_baseline_n"], f["copy_of_rank"], f["records"], f["outcome"],
                            f["sentinel_review"]] for f in sn["flagged"]]), ""]
    if "sessions" in report:
        ss = report["sessions"]
        out += ["## Sessions", "", f"Rows {ss['rows']:,}; viewing rows {ss['viewing_rows']:,}; "
                f"collections {ss['collections']}.", ""]
        rows = []
        for label, per_gap in ss["definitions"].items():
            for gap, v in per_gap.items():
                rows.append([label, gap, v["sessions"], v["viewing_sessions"], v["sessions_without_viewing"],
                             v["single_view_sessions_pct"], v["median_views_per_session"],
                             v["median_duration_s"], v["p90_duration_s"], v["median_sessions_per_collection"]])
        out += [_md_table(["Rows counted", "Gap (s)", "Sessions", "With viewing", "Without viewing",
                           "Single-view %", "Median views", "Median duration (s)", "p90 duration (s)",
                           "Median sessions per collection"], rows), "",
                f"Viewing sittings joined by non-viewing rows at the production gap: all rows "
                f"{ss['viewing_sittings_joined_by_all_rows']:,}; without followed_by "
                f"{ss['viewing_sittings_joined_by_without_followed_by']:,}.", "",
                _md_table(["Gap between consecutive views", "Gaps"],
                          [[label, ss["viewing_gap_bins"].get(label, 0)] for label, _u in GAP_BINS]), ""]
    if "time_zone" in report:
        tz = report["time_zone"]
        out += ["## Time zone: inference against the donor's postcode", "",
                "AIO files by location basis: " + json.dumps(tz["aio_files_by_location_basis"]), "",
                _md_table(["Measure", "Value"], [
                    ["Files calibrated (distinct content)", tz["files_calibrated"]],
                    ["By zone", json.dumps(tz["by_zone"])], ["Agree", tz["agree"]],
                    ["Off by half an hour", tz["off_by_half_hour"]], ["Off by one hour", tz["off_by_one_hour"]],
                    ["Off by more", tz["off_by_more"]], ["Differences (h)", json.dumps(tz["diff_hours"])],
                    ["Median rows in the other daylight-saving half (%)",
                     tz["rows_in_other_dst_half_pct"].get("median")],
                ]), ""]
    od = report["order"]
    out += ["## Donation order", "", "Source used: " + json.dumps(od["source_used"]),
            "", "Chosen time after first appearance in the table: " + str(od["chosen_after_first_in_table"]), "",
            _md_table(["Sources", "Files", "Median |gap| days", "Max |gap| days"],
                      [[k, v["files"], v["median_days"], v["max_days"]] for k, v in od["pairs"].items()])]
    if "fidelity" in report:
        out += ["", "## Fidelity against the production table", "",
                _md_table(["Measure", "Value"], [[k, v] for k, v in report["fidelity"].items()])]
    if "rows_by_type_vs_production" in report:
        out += ["", "## Whole table by type, production against replay", "",
                _md_table(["Type", "Production", "Replay", "Difference"],
                          [[t, v["production"], v["replay"], v["difference"]]
                           for t, v in report["rows_by_type_vs_production"].items()]),
                "", "Collections: " + json.dumps(report.get("collections_vs_production"))]
    if "order_dependence" in report:
        out += ["", "## Order dependence", "",
                _md_table(["Measure", "Value"], [[k, json.dumps(v)] for k, v in report["order_dependence"].items()])]
    return "\n".join(out) + "\n"





def render_growth_svg(steps: list[dict]) -> str:
    """Cumulative records read, rows produced and rows held, over donation rank (SVG)."""
    width, height, left, top, plot_w, plot_h = 1000, 460, 90, 50, 820, 330
    ordered = sorted(steps, key=lambda s: s["rank"])
    read = np.cumsum([s["records"] for s in ordered]) if ordered else np.zeros(1)
    produced = np.cumsum([s["processed_rows"] for s in ordered]) if ordered else np.zeros(1)
    held = np.cumsum([s["final_rows"] - s["rows_replaced_in_older_files"] for s in ordered]) \
        if ordered else np.zeros(1)
    ymax = max(float(read.max()), 1.0)
    n = max(len(ordered), 1)

    def path(values: np.ndarray) -> str:
        pts = [f"{left + plot_w * (i + 1) / n:.1f},{top + plot_h * (1 - v / ymax):.1f}"
               for i, v in enumerate(values)]
        return "M" + " L".join(pts) if pts else ""

    series = (("records read", read, "#8f9a95"), ("rows produced", produced, "#5b7f73"),
              ("rows held after deduplication", held, "#2f5d50"))
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" font-family="Helvetica, Arial, sans-serif" font-size="12">',
           f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
           f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#6b6b6b"/>',
           f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#6b6b6b"/>']
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = top + plot_h * (1 - frac)
        out.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#3a3a3a">'
                   f'{ymax * frac / 1e6:.1f}M</text>')
    x = left
    for label, values, colour in series:
        out.append(f'<path d="{path(values)}" fill="none" stroke="{colour}" stroke-width="2"/>')
        out.append(f'<rect x="{x}" y="18" width="12" height="12" fill="{colour}"/>')
        out.append(f'<text x="{x + 16}" y="28" fill="#1a1a1a" font-size="11">{label}</text>')
        x += 16 + 7 * len(label) + 24
    out.append(f'<text x="{left + plot_w / 2}" y="{top + plot_h + 36}" text-anchor="middle" fill="#1a1a1a">'
               f'donations in the order received (1 to {len(ordered)})</text>')
    out.append("</svg>")
    return "\n".join(out)





def render_intake_svg(report: dict) -> str:
    """Waterfall from records read to rows in the table, each loss named (SVG)."""
    a = report["intake"]["all"]
    produced = a["processed_rows"]
    final = produced - a["within_file_duplicates"] - a["rows_replaced_in_older_files"]
    steps = [
        ("Records read", a["records"], "total"),
        ("Sections outside the whitelist", -a["outside_whitelist"], "loss"),
        ("Records in files below the viewing floor", -a["too_small_records"], "loss"),
        ("Identical share records merged", -a["share_copies_merged"], "loss"),
        ("Records the parser could not read", -a["not_parseable"], "loss"),
        ("Rows produced", produced, "total"),
        ("Duplicates within a file", -a["within_file_duplicates"], "loss"),
        ("Rows a later copy superseded", -a["rows_replaced_in_older_files"], "loss"),
        ("Rows in the activity table", final, "total"),
    ]
    width, left, bar_w, row_h, top = 1000, 330, 470, 34, 20
    height = top + row_h * len(steps) + 20
    scale = bar_w / max(a["records"], 1)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" font-family="Helvetica, Arial, sans-serif" font-size="13">',
           f'<rect width="{width}" height="{height}" fill="#ffffff"/>']
    level = 0
    for i, (label, value, kind) in enumerate(steps):
        y = top + i * row_h
        if kind == "total":
            x0, w, colour, level = left, value * scale, "#2f5d50", value
        else:
            w = -value * scale
            x0, colour = left + (level + value) * scale, "#c9a227"
            level += value
        out.append(f'<text x="{left - 12}" y="{y + 19}" text-anchor="end" fill="#1a1a1a">{label}</text>')
        out.append(f'<rect x="{x0:.1f}" y="{y + 4}" width="{max(w, 1.0):.1f}" height="{row_h - 10}" fill="{colour}"/>')
        pct = f" ({100 * abs(value) / a['records']:.2f}%)" if kind == "loss" else ""
        out.append(f'<text x="{left + bar_w + 12}" y="{y + 19}" fill="#3a3a3a">{abs(value):,}{pct}</text>')
    out.append("</svg>")
    return "\n".join(out)





def render_gap_svg(report: dict) -> str:
    """Gaps between consecutive views, with the session thresholds marked (SVG)."""
    bins = report["sessions"]["viewing_gap_bins"]
    labels = [label for label, _u in GAP_BINS]
    counts = [bins.get(label, 0) for label in labels]
    total = max(sum(counts), 1)
    width, height, left, top, plot_w, plot_h = 1000, 420, 80, 30, 880, 300
    bar = plot_w / len(labels)
    ymax = max(counts) / total * 100
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" font-family="Helvetica, Arial, sans-serif" font-size="12">',
           f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
           f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#6b6b6b"/>']
    for i, (label, n) in enumerate(zip(labels, counts, strict=True)):
        share = n / total * 100
        h = plot_h * share / ymax
        x = left + i * bar
        out.append(f'<rect x="{x + 6:.1f}" y="{top + plot_h - h:.1f}" width="{bar - 12:.1f}" height="{h:.1f}" '
                   f'fill="#2f5d50"/>')
        out.append(f'<text x="{x + bar / 2:.1f}" y="{top + plot_h - h - 6:.1f}" text-anchor="middle" '
                   f'fill="#3a3a3a">{share:.1f}%</text>')
        out.append(f'<text x="{x + bar / 2:.1f}" y="{top + plot_h + 18}" text-anchor="middle" '
                   f'fill="#1a1a1a" font-size="11">{label}</text>')
    for edge, label in ((5, "5 min"), (6, "15 min (production)"), (7, "30 min")):
        x = left + edge * bar
        out.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" stroke="#a03530" '
                   f'stroke-dasharray="4 3"/>')
        out.append(f'<text x="{x + 4:.1f}" y="{top + 12 + 14 * (edge - 5)}" fill="#a03530" font-size="11">{label}</text>')
    out.append(f'<text x="{left + plot_w / 2}" y="{height - 12}" text-anchor="middle" fill="#1a1a1a">'
               f'gap between consecutive viewing events in a collection ({total:,} gaps)</text>')
    out.append("</svg>")
    return "\n".join(out)





def write_rendered(out_dir: Path, report: dict, steps: list[dict] | None = None) -> None:
    """Write the tables and figures for ``report`` into ``out_dir``."""
    (out_dir / "replay_tables.md").write_text(render_tables_md(report))
    (out_dir / "fig_replay_intake.svg").write_text(render_intake_svg(report))
    if "sessions" in report:
        (out_dir / "fig_replay_view_gaps.svg").write_text(render_gap_svg(report))
    if steps is not None:
        (out_dir / "fig_replay_growth.svg").write_text(render_growth_svg(steps))





# ---------------------------------------------------------------------------
# I/O: snapshot inputs, scratch store, the replay loop.
# ---------------------------------------------------------------------------

def load_intake_report_module(repo_root: Path):
    """Import ``scripts/intake_report.py`` for its snapshot-wiring helpers."""
    spec = importlib.util.spec_from_file_location("fyp_intake_report", repo_root / "scripts" / "intake_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod





def write_scratch_config(repo_root: Path, store: Path, out_dir: Path, ir) -> Path:
    """Config wiring every storage location into the scratch store, with the AIO fetch off."""
    config_path = ir.write_snapshot_config(repo_root, store, out_dir)
    overlay = config_path.parent / "config.local.toml"
    overlay.write_text(overlay.read_text() + "[features]\naio_aws_fetch = false\n")
    return config_path





def snapshot_inputs(snapshot: Path) -> dict:
    """Read the snapshot's ledger, AIO donation dates and per-file table facts.

    From the AIO metadata only the donation id and date are kept, and the
    participant's postcode and country are reduced at once to a time zone
    (or the reason there is none); nothing else about the participant is
    read, and nothing about a single participant is written out.
    """
    import polars as pl

    ledger = json.loads((snapshot / "recoded" / "ingestion_ledger.json").read_text()).get("files", {})
    aio_dates: dict[str, datetime] = {}
    zones: dict[str, tuple] = {}
    aio_dir = snapshot / "activity_data" / "aio" / "aio_participants"
    for path in sorted(aio_dir.glob("*.json")) if aio_dir.is_dir() else []:
        for item in (json.loads(path.read_text()) or {}).get("Items", []):
            did = (item.get("id") or {}).get("S")
            when = parse_iso((item.get("date") or {}).get("S"))
            if did and when:
                aio_dates[did] = min(when, aio_dates.get(did, when))
            if did:
                postcode = (item.get("postCode") or {}).get("S") or (item.get("postCode") or {}).get("N")
                country = (item.get("country") or {}).get("S")
                zones[did] = zone_from_postcode(postcode, country)
    table = (pl.scan_parquet(snapshot / "recoded" / "collections_recoded.parquet")
             .filter(pl.col("source_platform") == "tiktok", pl.col("data_source") != "zeeschuimer")
             .group_by("raw_file")
             .agg(pl.col("ts_added_to_dataset").min().alias("first_added"), pl.len().alias("rows"),
                  pl.col("collection_id").first().alias("collection_id"))
             .collect())
    rows = list(table.iter_rows(named=True))
    return {
        "ledger": ledger,
        "aio_dates": aio_dates,
        "zones": zones,
        "first_added": {r["raw_file"]: r["first_added"].replace(tzinfo=UTC) for r in rows if r["first_added"]},
        "prod_rows": {r["raw_file"]: int(r["rows"]) for r in rows},
        "prod_cid": {r["raw_file"]: r["collection_id"] for r in rows},
        "prod_by_type": {str(k): int(v) for k, v in (
            pl.scan_parquet(snapshot / "recoded" / "collections_recoded.parquet")
            .filter(pl.col("source_platform") == "tiktok", pl.col("data_source") != "zeeschuimer")
            .group_by("activity_type").len().collect().iter_rows())},
        "prod_collections": int(
            pl.scan_parquet(snapshot / "recoded" / "collections_recoded.parquet")
            .filter(pl.col("source_platform") == "tiktok", pl.col("data_source") != "zeeschuimer")
            .select(pl.col("collection_id").n_unique()).collect().item()),
    }





def list_raw_files(snapshot: Path) -> list[dict]:
    """Every raw export in the snapshot's TikTok export folders."""
    files = []
    for route, (group, folder) in ROUTE_FOLDERS.items():
        d = snapshot / "activity_data" / group / folder
        for p in sorted(d.iterdir()) if d.is_dir() else []:
            if p.is_file() and not p.name.startswith(".") and p.name != MANIFEST_FILENAME:
                files.append({"raw_file": p.name, "route": route, "path": p})
    return files





def census_all(files: list[dict], parser_cls) -> tuple[dict[str, dict], dict[str, str]]:
    """Section census of every file, read once before the replay."""
    census, errors = {}, {}
    for f in files:
        try:
            payload = json.loads(Path(f["path"]).read_text())
            if not isinstance(payload, dict):
                raise ValueError("not a JSON object")
            census[f["raw_file"]] = section_census(parser_cls._walk_sections(payload), parser_cls)
        except Exception as exc:  # the replay records the parser's own verdict on these files
            errors[f["raw_file"]] = str(exc)
    return census, errors





def _ingest_pass(main, use_sentinel: bool) -> dict:
    """One ingest run over whatever is pending, in ``run_ingest_refresh`` order.

    Covers load_raw, process, the sentinel's Phase B, migrate, the per-file
    summary, the ledger update and the sentinel commit. Returns what the
    caller needs to describe the step.
    """
    from fyp.core.structure_sentinel import StructureSentinel
    from web_interface.run_ingest_refresh import (
        _build_per_file_summary,
        _per_file_counts,
        _removed_rows_breakdown,
    )

    sentinel = StructureSentinel() if use_sentinel else None
    for sub in main.collections:
        sub.sentinel = sentinel
        sub.quarantined_this_run = {}
    data = main.data
    existing = set(data["raw_file"].astype(str).unique()) if len(data) else set()
    pre_counts, pre_cids = {}, {}
    if len(data):
        grp = data.groupby("raw_file", observed=True)["collection_id"]
        pre_counts = {str(k): int(v) for k, v in grp.size().items()}
        pre_cids = {str(k): str(v) for k, v in grp.first().items() if pd.notna(v)}
    discarded_before: set[str] = set(main.discarded_raw_files)
    for sub in main.collections:
        discarded_before.update(sub.discarded_raw_files)

    main.load_raw()
    raw_counts = _per_file_counts(main.collections)
    discarded_after: set[str] = set()
    for sub in main.collections:
        discarded_after.update(str(f) for f in sub.discarded_raw_files)
    main.process()
    processed_counts = _per_file_counts(main.collections)

    quarantined: dict[str, dict] = {}
    if sentinel is not None:
        for sub in main.collections:
            if sub.data is None or len(sub.data) == 0 or "raw_file" not in sub.data.columns:
                continue
            drop = []
            for rf, df_file in sub.data.groupby("raw_file", observed=True):
                verdict = sentinel.check_processed(sub, str(rf), df_file)
                if verdict["status"] == "quarantined":
                    drop.append(str(rf))
                    sub.quarantined_this_run[str(rf)] = verdict
            if drop:
                sub.data = sub.data[~sub.data["raw_file"].isin(drop)].copy()
        for sub in main.collections:
            quarantined.update(sub.quarantined_this_run)

    frames = {sub.data_source: sub.data.copy() for sub in main.collections
              if sub.state == "processed" and len(sub.data)}
    load_failed = {fn: {"error": err, "platform": s.source_platform, "source": s.data_source}
                   for s in main.collections for fn, err in s.load_failed_this_run.items()}
    file_stats: dict[str, dict] = {}
    for sub in main.collections:
        file_stats.update(getattr(sub, "file_stats_this_run", {}) or {})

    main.migrate_sub_collections()
    summary = _build_per_file_summary(
        main, raw_counts=raw_counts, processed_counts=processed_counts,
        discarded_at_load=discarded_after - discarded_before, existing_raw_files=existing,
        quarantined=quarantined, load_failed=load_failed, file_stats=file_stats,
        pre_cids=pre_cids, cid_remap=getattr(main, "last_cid_remap", {}) or {},
    )
    main.update_ledger(summary)
    if sentinel is not None:
        sentinel.commit(ingested_filenames={e["filename"] for e in summary
                                            if e.get("outcome") in ("added_as_new", "merged_with_existing")})
    replaced, _elsewhere = _removed_rows_breakdown(
        main.data, pre_counts, pre_cids, getattr(main, "last_cid_remap", {}) or {}, summary)
    observations = dict(sentinel.observations) if sentinel is not None else {}
    return {"summary": summary, "frames": frames, "replaced": int(replaced), "existing": existing,
            "pre_counts": pre_counts, "pre_cids": pre_cids, "observations": observations}





def _verdict_record(verdict: dict | None) -> dict:
    """The parts of a sentinel verdict the replay reports (no fingerprints)."""
    if not verdict:
        return {"status": None, "findings": []}
    return {
        "status": verdict.get("status"),
        "findings": [f"{f.get('layer')}:{f.get('code')}:{f.get('severity')}" for f in verdict.get("findings") or []],
        "withheld_sections": len(verdict.get("withheld_sections") or []),
    }





def replay(order_rows: list[dict], census: dict[str, dict], ledger: dict[str, dict],
           limit: int | None, log, use_sentinel: bool = True,
           review: str = "approve", zones: dict[str, tuple] | None = None,
           calibrate=None) -> tuple[list[dict], dict, dict, object]:
    """Ingest the files one at a time, in ``order_rows`` order, into the configured store.

    ``main`` points the configuration at the scratch store before calling
    this. Each file is copied into its route's raw folder with its replay
    mtime (which the pipeline stamps as ``ts_added_to_dataset`` and the
    newest-wins dedup sorts on) and a manifest entry naming its collection
    (the file stem), plus the time zone and browser-review flag its ledger
    entry recorded; it is removed after its step, so a file the parser
    cannot read is not retried.

    With the sentinel on, it learns from nothing in donation order, the way
    it would have had it been running from the first donation. A quarantined
    file is approved or rejected at once, as ``review`` says, the way an
    operator would decide it in the review panel; an approved file is then
    ingested in a second pass, which is what an approval does in production.

    Returns:
        ``(steps, links, unification, main_collection)``.
    """
    import fyp.core.structure_sentinel as sentinel_mod
    import fyp.ingest.tiktok as tiktok_mod
    from fyp.fyp_config import get_config
    from fyp.ingest.base import ForYouCollection
    from fyp.ingest.tiktok import TikTokAIOCollection, TikTokDDPCollection

    links: dict[str, dict] = {}
    original_fold = tiktok_mod.derive_play_duration

    def instrumented_fold(df, *args, **kwargs):
        name = str(df["raw_file"].iloc[0]) if "raw_file" in df.columns and len(df) else None
        lc = link_census(df)
        out = original_fold(df, *args, **kwargs)
        lc["tokens_written"] = tokens_written(out)
        if name:
            links[name] = lc
        return out

    tiktok_mod.derive_play_duration = instrumented_fold
    main = ForYouCollection(verbose=False)
    main.collections = []
    main.register_collection_class(TikTokDDPCollection)
    main.register_collection_class(TikTokAIOCollection)

    rank_of = {r["raw_file"]: r["rank"] for r in order_rows}
    copy_of = copy_of_earlier(order_rows)
    prior_sets: dict[str, set[int]] = {}
    steps: list[dict] = []
    unification: dict[str, dict] = {}
    todo = order_rows[:limit] if limit else order_rows
    try:
        for step_no, row in enumerate(todo, start=1):
            t0 = time.perf_counter()
            name, route = row["raw_file"], row["route"]
            raw_dir = Path(get_config()["paths"][ROUTE_FOLDERS[route][1]])
            raw_dir.mkdir(parents=True, exist_ok=True)
            dest = raw_dir / name
            shutil.copyfile(row["path"], dest)
            os.utime(dest, (row["replay_mtime"], row["replay_mtime"]))
            led = ledger.get(name) or {}
            entry = {"collection_id": Path(name).stem}
            for key in ("tz", "client_reviewed"):
                if led.get(key):
                    entry[key] = led[key]
            (raw_dir / MANIFEST_FILENAME).write_text(json.dumps({name: entry}))
            baseline_n = None
            if use_sentinel:
                key = sentinel_mod.baseline_key("tiktok", route, "reviewed" if led.get("client_reviewed") else None)
                baseline_n = int((sentinel_mod.load_baselines()["baselines"].get(key) or {}).get("n_accepted") or 0)

            first = _ingest_pass(main, use_sentinel)
            first_verdict = _verdict_record(first["observations"].get(name))
            final = first
            reviewed = None
            if first_verdict["status"] == "quarantined":
                reviewed = review
                if review == "approve":
                    sentinel_mod.approve_file(name, reviewed_by="replay")
                    main.remove_from_ledger(name)
                    for lst in [main.discarded_raw_files] + [sub.discarded_raw_files for sub in main.collections]:
                        while name in lst:
                            lst.remove(name)
                    final = _ingest_pass(main, use_sentinel)
                    final["existing"] = first["existing"]
                    final["pre_counts"], final["pre_cids"] = first["pre_counts"], first["pre_cids"]
                else:
                    sentinel_mod.reject_file(name, reviewed_by="replay")
            summary = final["summary"]
            replaced = final["replaced"]
            existing, pre_counts, pre_cids = final["existing"], final["pre_counts"], final["pre_cids"]
            mine = next((e for e in summary if e["filename"] == name), None) or {
                "outcome": "not_seen", "processed_rows": 0, "final_rows": 0, "dropped": {},
                "merged_with_siblings": []}
            dest.unlink(missing_ok=True)

            # The files this donation was merged with: its surviving siblings,
            # plus earlier files whose collection now points at its collection
            # (a file whose rows were all superseded has no row left to be a
            # sibling, and the per-file summary then reports "added_as_new").
            my_cid = mine.get("canonical_collection_id")
            remap = getattr(main, "last_cid_remap", {}) or {}
            absorbed = {f for f, c in pre_cids.items() if my_cid and remap.get(c) == my_cid}
            partners = sorted(set(mine.get("merged_with_siblings") or []) | absorbed)

            new_frame = final["frames"].get(route, pd.DataFrame())
            if len(new_frame):
                new_frame = new_frame[new_frame["raw_file"].astype(str) == name]
            if len(new_frame):
                unification[name] = {str(k): int(v) for k, v in
                                     new_frame["activity_type"].astype("string").value_counts().items()}
            overlap = prior_overlap(seconds_of(new_frame).get(name, set()) if len(new_frame) else set(), prior_sets)

            post = main.data
            if len(post):
                post_counts = {str(f): int(c) for f, c in post.groupby("raw_file", observed=True).size().items()}
                for f in set(pre_counts) - set(post_counts):
                    prior_sets.pop(f, None)
                changed = {f for f, c in post_counts.items() if pre_counts.get(f) != c}
                if changed:
                    prior_sets.update(seconds_of(post[post["raw_file"].astype(str).isin(changed)]))

            dropped = mine.get("dropped") or {}
            cen = census.get(name)
            matches = None
            if cen and mine["outcome"] in ("added_as_new", "merged_with_existing", "fully_deduped"):
                matches = (int(dropped.get("outside_whitelist", 0)) == cen["outside_whitelist"]
                           and int(dropped.get("not_parseable", 0)) == sum(cen["not_parseable"].values())
                           and int(dropped.get("share_copies_merged", 0)) == cen["share_copies_merged"])
            lc = links.get(name)
            processed = int(mine.get("processed_rows") or 0)
            final_rows = int(mine.get("final_rows") or 0)
            tz_mode = None
            median_utc = None
            zone, basis = (zones or {}).get(name, (None, None))
            cal: dict = {}
            if len(new_frame):
                tz_mode = float(new_frame["tz_offset"].astype("float64").mode().iloc[0])
                median_utc = pd.to_datetime(new_frame["utc_timestamp"], utc=True).median().isoformat()
                if zone and calibrate is not None:
                    cal = calibrate(new_frame["utc_timestamp"], zone)
            step = {
                "_step": step_no,
                "rank": row["rank"], "raw_file": name, "route": route, "order_source": row["order_source"],
                "order_ts": row["order_ts"].isoformat() if row["order_ts"] else None,
                "records": cen["records"] if cen else 0,
                "viewing_records": cen["viewing_records"] if cen else 0,
                "outside_whitelist": int(dropped.get("outside_whitelist", 0)),
                "not_parseable": int(dropped.get("not_parseable", 0)),
                "share_copies_merged": int(dropped.get("share_copies_merged", 0)),
                "missing_required": int(dropped.get("missing_required", 0)),
                "processed_rows": processed,
                "within_file_duplicates": max(processed - final_rows, 0),
                "rows_replaced_in_older_files": int(replaced),
                "final_rows": final_rows,
                "outcome": mine["outcome"],
                "copy_of_rank": copy_of.get(name),
                "merged_with_earlier": bool(set(partners) & existing),
                "merged_with_ranks": ";".join(str(rank_of.get(s, "?")) for s in partners),
                "max_overlap": round(overlap["max_overlap"], 4),
                "max_overlap_rank": rank_of.get(overlap["max_partner"]) if overlap["max_partner"] else None,
                "files_touching": overlap["files_touching"],
                "table_rows_after": len(post),
                "collections_after": int(post["collection_id"].nunique()) if len(post) else 0,
                "census_matches_parser": matches,
                "tokens_match": (lc["tokens_expected"] == lc["tokens_written"]) if lc else None,
                "sentinel_baseline_n": baseline_n,
                "sentinel_status": first_verdict["status"],
                "sentinel_findings": ";".join(first_verdict["findings"]),
                "sentinel_withheld_sections": first_verdict.get("withheld_sections"),
                "sentinel_review": reviewed,
                "inferred_offset": tz_mode,
                "median_utc": median_utc,
                "tz_zone": zone,
                "tz_basis": basis,
                "tz_true_offset": cal.get("zone_offset"),
                "tz_diff": cal.get("diff"),
                "tz_other_dst_pct": cal.get("rows_in_other_dst_half_pct"),
                "seconds": round(time.perf_counter() - t0, 2),
            }
            steps.append(step)
            flag = f" [sentinel {first_verdict['status']}]" if first_verdict["status"] in ("warn", "quarantined") else ""
            log(f"[{step_no}/{len(todo)}] rank {row['rank']} {route} {mine['outcome']}{flag}: "
                f"{step['records']:,} records -> {processed:,} rows, {replaced:,} superseded in older files, "
                f"table {len(post):,} ({step['seconds']}s)")
    finally:
        tiktok_mod.derive_play_duration = original_fold
    return steps, links, unification, main





def write_csv(path: Path, rows: list[dict], columns) -> None:
    """Write ``rows`` to ``path`` with the given column order."""
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)





def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)





def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", help="snapshot root holding recoded/ and activity_data/")
    parser.add_argument("--out", required=True, help="output directory (the scratch store lives inside it)")
    parser.add_argument("--render", action="store_true",
                        help="only redraw the tables and figures from <out>/replay_report.json")
    parser.add_argument("--order", choices=("donation", "reverse", "shuffle"), default="donation")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--limit", type=int, help="replay only the first N files of the order")
    parser.add_argument("--compare-with", help="another replay's --out, for the order-dependence check")
    parser.add_argument("--no-sentinel", action="store_true", help="replay without the structure sentinel")
    parser.add_argument("--save-table", action="store_true",
                        help="also write the final activity table to <out>/_final_table.parquet (participant data: "
                             "keep it local and delete it with the snapshot)")
    parser.add_argument("--review", choices=("approve", "reject"), default="approve",
                        help="what the simulated operator does with a quarantined file")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    out_dir = Path(args.out).expanduser().resolve()
    if args.render:
        write_rendered(out_dir, json.loads((out_dir / "replay_report.json").read_text()))
        return 0
    if not args.snapshot:
        parser.error("--snapshot is required unless --render is given")
    snapshot = Path(args.snapshot).expanduser().resolve()
    if not (snapshot / "recoded").is_dir():
        raise SystemExit(f"{snapshot} has no recoded/ directory")
    if snapshot == out_dir or snapshot in out_dir.parents:
        raise SystemExit("--out must not be inside the snapshot")
    store = out_dir / "_replay_store"
    if store.exists():
        shutil.rmtree(store)
    store.mkdir(parents=True)

    ir = load_intake_report_module(repo_root)
    os.environ["FYP_CONFIG_PATH"] = str(write_scratch_config(repo_root, store, out_dir, ir))
    ir.assert_snapshot_storage(store)

    from fyp.ingest.tiktok import TikTokDDPCollection

    def log(msg: str) -> None:
        print(msg, flush=True)

    inputs = snapshot_inputs(snapshot)
    files = list_raw_files(snapshot)
    log(f"{len(files)} raw export file(s); reading sections...")
    census, census_errors = census_all(files, TikTokDDPCollection)
    for f in files:
        name = f["raw_file"]
        led = inputs["ledger"].get(name) or {}
        f["candidates"] = {
            "uploaded_at": parse_iso(led.get("uploaded_at")),
            "name_stamp": stamp_from_name(name),
            "aio_date": inputs["aio_dates"].get(name),
            "table_first_added": inputs["first_added"].get(name),
            "ledger_first_seen": parse_iso(led.get("ts_first_seen")),
            "last_event": (census.get(name) or {}).get("last_event"),
        }
    for f in files:
        f["sha256"] = hashlib.sha256(Path(f["path"]).read_bytes()).hexdigest()
    order_rows = donation_order(files, args.order, args.seed)

    t0 = time.perf_counter()
    steps, links, unification, main_collection = replay(
        order_rows, census, inputs["ledger"], args.limit, log, use_sentinel=not args.no_sentinel,
        review=args.review, zones=inputs["zones"], calibrate=ir.calibrate_one_file)
    elapsed = time.perf_counter() - t0

    replayed = {s["raw_file"] for s in steps}
    files_seen: Counter = Counter()
    for name in replayed:
        files_seen.update(set((census.get(name) or {}).get("by_section", {})))
    report = aggregate(steps, {k: v for k, v in census.items() if k in replayed},
                       {k: v for k, v in links.items() if k in replayed}, unification,
                       [r for r in order_rows if r["raw_file"] in replayed], dict(files_seen))
    data = main_collection.data
    final_files: list[dict] = []
    if len(data):
        grp = data.groupby("raw_file", observed=True)["collection_id"]
        final_files = [{"raw_file": str(f), "final_rows": int(n), "collection_id": str(c)}
                       for (f, n), c in zip(grp.size().items(), grp.first().tolist(), strict=True)]
        report["rows_by_type_vs_production"] = rows_by_type_comparison(
            inputs["prod_by_type"],
            {str(k): int(v) for k, v in data["activity_type"].astype("string").value_counts().items()})
        report["collections_vs_production"] = {"production": inputs["prod_collections"],
                                               "replay": int(data["collection_id"].nunique())}
        report["final_link_method"] = {str(k): int(v) for k, v in
                                       data["link_method"].astype("string").value_counts(dropna=False).items()}
    report["fidelity"] = fidelity(
        {k: v for k, v in inputs["prod_rows"].items() if k in replayed}, inputs["prod_cid"],
        {r["raw_file"]: r["final_rows"] for r in final_files},
        {r["raw_file"]: r["collection_id"] for r in final_files},
    )
    report["census_errors"] = len(census_errors)
    report["sentinel"] = sentinel_summary(steps)
    report["time_zone"] = tz_summary(steps)
    if len(data):
        report["sessions"] = session_census(data)
    report["raw_files_found"] = {route: sum(1 for f in files if f["route"] == route) for route in ROUTE_FOLDERS}
    report["production_files_without_raw"] = len(set(inputs["prod_rows"]) - {f["raw_file"] for f in files})
    if args.compare_with:
        with (Path(args.compare_with).expanduser() / "final_files.csv").open() as fh:
            report["order_dependence"] = compare_runs(final_files, list(csv.DictReader(fh)))
    report["run"] = {"git_head": ir.git_head(repo_root), "run_at": datetime.now(UTC).isoformat(),
                     "seconds": round(elapsed, 1), "files_replayed": len(steps), "args": vars(args)}

    out_dir.mkdir(parents=True, exist_ok=True)
    if args.save_table and len(data):
        data.to_parquet(out_dir / "_final_table.parquet")
    (out_dir / "replay_report.json").write_text(json.dumps(report, indent=2, default=_json_default))
    write_rendered(out_dir, report, steps)
    write_csv(out_dir / "replay_steps.csv", steps, STEP_COLUMNS)
    write_csv(out_dir / "final_files.csv", final_files, ("raw_file", "final_rows", "collection_id"))
    write_csv(out_dir / "donation_order.csv",
              [{"rank": r["rank"], "raw_file": r["raw_file"], "route": r["route"],
                "order_source": r["order_source"], "order_ts": r["order_ts"],
                **{f"cand_{k}": v for k, v in r["candidates"].items()}} for r in order_rows],
              ["rank", "raw_file", "route", "order_source", "order_ts", *[f"cand_{k}" for k in ORDER_SOURCES]])
    log(f"wrote replay_report.json, replay_tables.md, fig_replay_growth.svg, replay_steps.csv, "
        f"final_files.csv and donation_order.csv to {out_dir} ({elapsed:.0f}s)")
    return 0





if __name__ == "__main__":
    sys.exit(main())
