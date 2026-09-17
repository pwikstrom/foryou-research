#!/usr/bin/env python3
"""Intake report: every number a methods write-up needs about ingestion.

Reads a DOWNLOADED SNAPSHOT of the Hub's ``recoded`` storage location, never
the live bucket, and writes four files into ``--out``:

- ``report.json``            every figure, plus the snapshot's provenance
- ``tables.md``              the same figures as markdown tables with caveats
- ``fig4_attrition_funnel.svg``  one 100 % bar per route, losses named beside it
- ``quarantine_worksheet.csv``   one row per quarantined file for a human to
                             classify (approved with no change / benign
                             vintage change / led to a parser change)

What it computes, in the order the paper reports it: intake attrition per
route (rows read, dropped by reason, deduplicated, kept); the ledger outcome
distribution; the structure sentinel's denominator, false-positive split,
time in quarantine and a quarantined-versus-accepted comparison by route and
donor region; time-zone resolution levels and a calibration of the
inference against supplied zones; and sensitivity of the pipeline's
constants (session gap, comment-link window, donor-merge overlap).

The snapshot is wired in through a throwaway config directory so the
repository's own ``config.local.toml`` and ``.env`` are never read; the
script refuses to run if storage would resolve to GCS.

Usage:
    SNAP=~/fyp_snapshot_2026-09-17          # holds recoded/ingestion_ledger.json etc.
    python scripts/intake_report.py --snapshot $SNAP --out tmp/intake_report --skip-parquet
    # fill the `class` column of quarantine_worksheet.csv for approved rows, then
    python scripts/intake_report.py --snapshot $SNAP --out tmp/intake_report \\
        --classification tmp/intake_report/quarantine_worksheet.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent.parent))


RECODED = "recoded"
LEDGER_FILENAME = "ingestion_ledger.json"
VERDICTS_FILENAME = "structure_verdicts.json"
BASELINES_FILENAME = "structure_baselines.json"
TAGS_FILENAME = "collections_tags.json"
PARQUET_FILENAME = "collections_recoded.parquet"
PARSER_PATHS = ("fyp/ingest", "fyp/ingest.py", "fyp/core/structure_sentinel.py", "fyp/structure_sentinel.py")
SESSION_GAPS = (300, 900, 1800)
COMMENT_GAPS = (60, 180, 300)
OVERLAP_THRESHOLDS = (0.1, 0.2, 0.4)
SMALL_FILE_EVENTS = 30
CLASS_CODES = ("a", "b", "c", "d", "e")
CLASS_LABELS = {
    "a": "approved, no parser change, no vintage change (false positive)",
    "b": "approved as a benign vintage change",
    "c": "led to a parser change",
    "d": "rejected",
    "e": "still pending",
}
WITHHELD_PREFIX = "Uploader withheld:"
TZ_NOTE_PREFIX = "Time zone:"
PROVENANCE_KEYS = ("original_filename", "display_collection_id", "user_id", "tz",
                   "client_reviewed", "uploaded_by", "uploaded_at")
QUARANTINE_STATUSES = ("quarantined", "approved", "rejected")
_FIXED_OFFSET_RE = re.compile(r"^([+-])(\d{1,2})(?::?(\d{2}))?$")
WORKSHEET_COLUMNS = (
    "filename", "platform", "source", "variant", "status", "review_action", "reviewed_by",
    "quarantine_start", "start_source", "ts_evaluated", "reviewed_at", "days_in_quarantine",
    "findings_codes", "findings_digest", "withheld", "in_accepted_structures", "was_warn_only",
    "parser_commits", "suggested_class", "class",
)





# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)





def route_of(entry: dict) -> str:
    """Return ``"{platform}_{source}"`` for a ledger entry, or ``"unknown"``."""
    platform = entry.get("platform")
    source = entry.get("source")
    if not platform:
        return "unknown"
    return f"{platform}_{source or 'unknown'}"





def baseline_key_of(verdict: dict) -> str:
    """Baseline key a verdict was evaluated against (mirrors ``structure_sentinel.baseline_key``)."""
    key = f"{verdict.get('platform')}_{verdict.get('source')}"
    variant = verdict.get("variant")
    return f"{key}__{variant}" if variant else key





def has_provenance(entry: dict) -> bool:
    """True when the ledger entry carries any upload-manifest provenance."""
    return any(entry.get(k) not in (None, "", False) for k in PROVENANCE_KEYS)





def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None





# ---------------------------------------------------------------------------
# 5.1 Intake attrition
# ---------------------------------------------------------------------------

def attrition_by_route(ledger_files: dict[str, dict]) -> dict[str, dict]:
    """Rows read, dropped by reason, deduplicated and kept, per route and in total.

    Entries whose ``raw_rows`` is None (files migrated from the legacy
    discard list) are counted as files without counts and contribute no rows.
    Entries written before the ledger recorded ``processed_rows`` and a
    ``dropped`` breakdown carry only rows read and rows kept; their gap is
    reported as ``unattributed_pre_breakdown`` rather than left unexplained.

    Args:
        ledger_files: The ``files`` map of ``ingestion_ledger.json``.

    Returns:
        ``{route: {...}, "all": {...}}`` with integer counts and ``kept_pct``.
    """
    per_route: dict[str, Counter] = defaultdict(Counter)
    for entry in ledger_files.values():
        c = per_route[route_of(entry)]
        c["files"] += 1
        if entry.get("raw_rows") is None:
            c["files_without_counts"] += 1
            continue
        c["files_with_counts"] += 1
        c["rows_read"] += int(entry.get("raw_rows") or 0)
        c["processed_rows"] += int(entry.get("processed_rows") or 0)
        c["kept"] += int(entry.get("kept_rows") or 0)
        c["deduped"] += int(entry.get("deduped_rows") or 0)
        if entry.get("dropped") is None and entry.get("processed_rows") is None:
            c["files_without_breakdown"] += 1
            c["unattributed_pre_breakdown"] += max(int(entry.get("raw_rows") or 0) - int(entry.get("kept_rows") or 0), 0)
            continue
        c["files_with_breakdown"] += 1
        dropped = entry.get("dropped") or {}
        c["dropped_not_parseable"] += int(dropped.get("not_parseable") or 0)
        c["dropped_missing_required"] += int(dropped.get("missing_required") or 0)
        c["dropped_outside_whitelist"] += int(dropped.get("outside_whitelist") or 0)
        for reason, n in dropped.items():
            if reason not in ("not_parseable", "missing_required", "outside_whitelist"):
                c["dropped_other"] += int(n or 0)
    total = Counter()
    for c in per_route.values():
        total.update(c)
    per_route["all"] = total
    out: dict[str, dict] = {}
    for route, c in per_route.items():
        row = {k: int(v) for k, v in c.items()}
        for k in ("files", "files_with_counts", "files_without_counts", "files_with_breakdown",
                  "files_without_breakdown", "rows_read", "processed_rows", "kept", "deduped",
                  "dropped_not_parseable", "dropped_missing_required", "dropped_outside_whitelist", "dropped_other",
                  "unattributed_pre_breakdown"):
            row.setdefault(k, 0)
        row["unaccounted"] = row["rows_read"] - row["kept"] - row["deduped"] - row["dropped_not_parseable"] \
            - row["dropped_missing_required"] - row["dropped_outside_whitelist"] - row["dropped_other"] \
            - row["unattributed_pre_breakdown"]
        row["kept_pct"] = round(100.0 * row["kept"] / row["rows_read"], 2) if row["rows_read"] else None
        out[route] = row
    return out





def table_reconciliation(ledger_files: dict[str, dict], files_in_table: dict[str, str]) -> dict:
    """Reconcile the files the activity table holds with the ledger's entries.

    Args:
        ledger_files: The ``files`` map of ``ingestion_ledger.json``.
        files_in_table: ``{raw_file: route}`` for every raw file in the table.

    Returns:
        Files in the table split by whether they have a ledger entry and
        whether that entry carries counts, and the counted entries that are
        not in the table, by outcome (fully deduplicated re-uploads, files
        removed since).
    """
    in_table = set(files_in_table)
    with_entry = {fn for fn in in_table if fn in ledger_files}
    with_counts = {fn for fn in with_entry if ledger_files[fn].get("raw_rows") is not None}
    counted = {fn for fn, e in ledger_files.items() if e.get("raw_rows") is not None}
    not_in_table = Counter(str(ledger_files[fn].get("outcome")) for fn in counted - in_table)
    return {
        "n_files_in_table": len(in_table),
        "n_in_table_with_entry": len(with_entry),
        "n_in_table_with_counts": len(with_counts),
        "n_in_table_without_entry": len(in_table - with_entry),
        "n_counted_entries": len(counted),
        "counted_entries_not_in_table_by_outcome": dict(sorted(not_in_table.items())),
    }





def null_activity_type_counts(df: pd.DataFrame) -> dict:
    """Rows whose ``activity_type`` is null, and the files they came from."""
    if df.empty or "activity_type" not in df.columns:
        return {"rows": 0, "files": 0}
    null = df["activity_type"].isna()
    return {"rows": int(null.sum()), "files": int(df.loc[null, "raw_file"].nunique()) if null.any() else 0}





def outcomes_by_route(ledger_files: dict[str, dict]) -> dict[str, dict[str, int]]:
    """Count ledger outcomes per route and in total."""
    per_route: dict[str, Counter] = defaultdict(Counter)
    for entry in ledger_files.values():
        per_route[route_of(entry)][str(entry.get("outcome"))] += 1
    total = Counter()
    for c in per_route.values():
        total.update(c)
    per_route["all"] = total
    return {route: dict(sorted(c.items())) for route, c in per_route.items()}





# ---------------------------------------------------------------------------
# 5.2 Sentinel
# ---------------------------------------------------------------------------

def sentinel_denominators(verdicts: dict[str, dict], baselines: dict[str, dict]) -> dict[str, dict]:
    """Per baseline key: how many files each sentinel layer actually evaluated.

    ``status != "learning"`` is the exact condition under which the structure
    layer ran (the sentinel returns ``learning`` while the baseline holds fewer
    accepted files than the structure threshold). The statistical layer ran
    only for verdicts carrying ``processed_stats``. Files present in a
    baseline's ``learned_files`` with no verdict were bootstrapped from disk
    and never evaluated. The ``learned_files`` index is deliberately NOT used
    as a phase marker: approved files are appended at approval time and
    pending or rejected files never appear in it.
    """
    out: dict[str, dict] = {}
    keys = set(baselines) | {baseline_key_of(v) for v in verdicts.values()}
    for key in sorted(keys):
        vs = [v for v in verdicts.values() if baseline_key_of(v) == key]
        learned = list((baselines.get(key) or {}).get("learned_files") or [])
        out[key] = {
            "n_verdicts": len(vs),
            "n_learning": sum(1 for v in vs if v.get("status") == "learning"),
            "n_structure_eligible": sum(1 for v in vs if v.get("status") != "learning"),
            "n_stats_eligible": sum(1 for v in vs if v.get("processed_stats") is not None),
            "n_learned_files": len(learned),
            "n_bootstrapped": sum(1 for fn in learned if fn not in verdicts),
            "n_accepted": int((baselines.get(key) or {}).get("n_accepted") or 0),
        }
    total = Counter()
    for row in out.values():
        total.update(row)
    out["all"] = {k: int(v) for k, v in total.items()}
    return out





def parse_git_log(text: str) -> list[dict]:
    """Parse ``git log --format=%H%x09%ad%x09%s`` output into commit dicts."""
    commits = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        sha, date, subject = parts
        when = parse_iso(date)
        if when is None:
            continue
        commits.append({"sha": sha[:8], "date": when.isoformat(), "subject": subject})
    return commits





def commits_between(commits: list[dict], start: str | None, end: str | None) -> list[dict]:
    """Commits whose date lies in ``[start, end]`` (inclusive); open-ended when a bound is None."""
    t0 = parse_iso(start)
    t1 = parse_iso(end)
    picked = []
    for c in commits:
        when = parse_iso(c["date"])
        if when is None:
            continue
        if t0 is not None and when < t0:
            continue
        if t1 is not None and when > t1:
            continue
        picked.append(c)
    return picked





def findings_digest(findings: list[dict]) -> str:
    """One-line digest of a findings list (same rule as the sentinel's ledger note)."""
    if not findings:
        return ""
    return "; ".join(f.get("detail", f.get("code", "?")) for f in findings[:6])





def parse_withheld_note(note: str | None) -> list[str]:
    """Section names from a ledger note of the form ``Uploader withheld: A, B | ...``."""
    if not note:
        return []
    for part in str(note).split(" | "):
        part = part.strip()
        if part.startswith(WITHHELD_PREFIX):
            body = part[len(WITHHELD_PREFIX):].strip()
            return [s.strip() for s in body.split(",") if s.strip()]
    return []





def quarantine_rows(
    verdicts: dict[str, dict],
    ledger_files: dict[str, dict],
    baselines: dict[str, dict],
    commits: list[dict],
    now: datetime | None = None,
) -> list[dict]:
    """One worksheet row per file the sentinel ever quarantined.

    Quarantine START is the upload time from the ledger's manifest provenance,
    because the verdict's ``ts_evaluated`` is overwritten on every ingest run
    and, after an approval, re-stamped later than ``reviewed_at``. The
    fallback is the earliest of ``ts_first_seen`` and ``ts_evaluated`` and the
    row says which source applied. Duration is ``reviewed_at - start`` and is
    left negative when the sources contradict, so the count of negatives is
    itself reported.
    """
    now = now or datetime.now(UTC)
    accepted_by_key: dict[str, set[str]] = {
        key: {a.get("filename") for a in (b.get("accepted_structures") or [])}
        for key, b in baselines.items()
    }
    rows = []
    for filename, v in sorted(verdicts.items()):
        if v.get("status") not in QUARANTINE_STATUSES:
            continue
        entry = ledger_files.get(filename) or {}
        candidates = {
            "uploaded_at": parse_iso(entry.get("uploaded_at")),
            "ts_first_seen": parse_iso(entry.get("ts_first_seen")),
            "ts_evaluated": parse_iso(v.get("ts_evaluated")),
        }
        if candidates["uploaded_at"] is not None:
            start, start_source = candidates["uploaded_at"], "uploaded_at"
        else:
            fallbacks = {k: t for k, t in candidates.items() if k != "uploaded_at" and t is not None}
            if fallbacks:
                start_source = min(fallbacks, key=fallbacks.get)
                start = fallbacks[start_source]
            else:
                start, start_source = None, "none"
        reviewed = parse_iso(v.get("reviewed_at"))
        days = None
        if start is not None and reviewed is not None:
            days = round((reviewed - start).total_seconds() / 86400.0, 2)
        findings = v.get("findings") or []
        status = v.get("status")
        suggested = {"rejected": "d", "quarantined": "e"}.get(status, "")
        window_end = reviewed.isoformat() if reviewed else now.isoformat()
        rows.append({
            "filename": filename,
            "platform": v.get("platform"),
            "source": v.get("source"),
            "variant": v.get("variant") or "",
            "status": status,
            "review_action": v.get("review_action") or "",
            "reviewed_by": v.get("reviewed_by") or "",
            "quarantine_start": start.isoformat() if start else "",
            "start_source": start_source,
            "ts_evaluated": v.get("ts_evaluated") or "",
            "reviewed_at": v.get("reviewed_at") or "",
            "days_in_quarantine": days,
            "findings_codes": ";".join(sorted({f"{f.get('layer')}:{f.get('code')}" for f in findings})),
            "findings_digest": findings_digest(findings),
            "withheld": ", ".join(v.get("withheld_sections") or []),
            "in_accepted_structures": filename in accepted_by_key.get(baseline_key_of(v), set()),
            "was_warn_only": status == "approved" and not any(f.get("severity") == "quarantine" for f in findings),
            "parser_commits": "; ".join(f"{c['sha']} {c['date'][:10]} {c['subject']}"
                                        for c in commits_between(commits, start.isoformat() if start else None, window_end)),
            "suggested_class": suggested,
            "class": "",
        })
    return rows





def quarantine_summary(rows: list[dict], classification: dict[str, str] | None = None) -> dict:
    """Counts a-e, time in quarantine and data-quality counters over the worksheet rows."""
    classification = classification or {}
    classes: Counter = Counter()
    unclassified = []
    for r in rows:
        code = r["suggested_class"] or classification.get(r["filename"], "")
        if code in CLASS_CODES:
            classes[code] += 1
        else:
            unclassified.append(r["filename"])
    durations = [r["days_in_quarantine"] for r in rows if r["days_in_quarantine"] is not None]
    positive = [d for d in durations if d >= 0]
    n_structure = sum(1 for r in rows if r["status"] in QUARANTINE_STATUSES)
    return {
        "n_files_ever_quarantined": n_structure,
        "n_pending": sum(1 for r in rows if r["status"] == "quarantined"),
        "n_approved": sum(1 for r in rows if r["status"] == "approved"),
        "n_rejected": sum(1 for r in rows if r["status"] == "rejected"),
        "n_warn_only_approvals": sum(1 for r in rows if r["was_warn_only"]),
        "classes": {code: int(classes.get(code, 0)) for code in CLASS_CODES},
        "class_labels": CLASS_LABELS,
        "n_unclassified_approved": len(unclassified),
        "unclassified": unclassified,
        "days_in_quarantine_median": _median(positive),
        "days_in_quarantine_max": max(positive) if positive else None,
        "n_reviewed_with_duration": len(positive),
        "n_negative_durations": len(durations) - len(positive),
        "start_sources": dict(Counter(r["start_source"] for r in rows)),
        "classification_source": "worksheet" if classification else "auto (d/e only)",
    }





def findings_by_layer_code(verdicts: dict[str, dict]) -> dict[str, dict[str, int]]:
    """Per verdict status: files carrying each ``layer:code`` and total findings.

    Findings on a stored verdict are those of the LATEST evaluation, so an
    approved file shows what its post-approval run still found.
    """
    files: dict[str, Counter] = defaultdict(Counter)
    findings: dict[str, Counter] = defaultdict(Counter)
    for v in verdicts.values():
        status = str(v.get("status"))
        seen = set()
        for f in v.get("findings") or []:
            code = f"{f.get('layer')}:{f.get('code')}"
            findings[status][code] += 1
            if code not in seen:
                files[status][code] += 1
                seen.add(code)
    return {
        "files_with_code": {s: dict(sorted(c.items())) for s, c in files.items()},
        "findings": {s: dict(sorted(c.items())) for s, c in findings.items()},
    }





def withheld_counts(verdicts: dict[str, dict], ledger_files: dict[str, dict]) -> dict:
    """Files with withheld sections and how often each section is withheld.

    Unions the verdict's ``withheld_sections`` with the ledger note form so a
    file recorded by both counts once.
    """
    per_file: dict[str, set[str]] = defaultdict(set)
    for filename, v in verdicts.items():
        for section in v.get("withheld_sections") or []:
            per_file[filename].add(str(section))
    for filename, entry in ledger_files.items():
        for section in parse_withheld_note(entry.get("notes")):
            per_file[filename].add(section)
    sections: Counter = Counter()
    for names in per_file.values():
        sections.update(names)
    return {
        "n_files_with_withheld_sections": sum(1 for s in per_file.values() if s),
        "sections": dict(sections.most_common()),
    }





def region_of_tz(tz: str | None) -> str:
    """Coarse region from a donor zone: IANA continent, ``fixed_offset`` or ``unknown``."""
    if not tz:
        return "unknown"
    tz = str(tz).strip()
    if _FIXED_OFFSET_RE.match(tz):
        return "fixed_offset"
    if "/" in tz:
        return tz.split("/", 1)[0]
    return tz if tz in ("UTC", "GMT") else "other"





def contingency(table: dict[str, tuple[int, int]]) -> dict:
    """Test whether quarantine differs across the keys of ``table``.

    Args:
        table: ``{key: (n_quarantined, n_accepted)}``.

    Returns:
        The table, the expected counts, and a chi-square result (Fisher's
        exact test as well when the table is 2x2).
    """
    from scipy import stats

    keys = [k for k, (q, a) in table.items() if q + a > 0]
    if len(keys) < 2:
        return {"table": {k: list(v) for k, v in table.items()}, "test": None,
                "note": "fewer than two non-empty rows"}
    observed = np.array([[table[k][0], table[k][1]] for k in keys], dtype=float)
    result: dict = {"table": {k: [int(table[k][0]), int(table[k][1])] for k in keys}}
    if observed.sum(axis=0).min() == 0:
        result.update({"test": None, "note": "one column is empty"})
        return result
    chi2, p, dof, expected = stats.chi2_contingency(observed, correction=len(keys) == 2)
    result.update({
        "test": "chi2",
        "statistic": round(float(chi2), 3),
        "p": round(float(p), 4),
        "dof": int(dof),
        "expected": {k: [round(float(e), 2) for e in row] for k, row in zip(keys, expected, strict=False)},
        "min_expected": round(float(expected.min()), 2),
    })
    if observed.shape == (2, 2):
        _, fisher_p = stats.fisher_exact(observed)
        result["fisher_p"] = round(float(fisher_p), 4)
    return result





def quarantine_contingencies(verdicts: dict[str, dict], ledger_files: dict[str, dict]) -> dict:
    """Quarantined versus accepted files by route and by donor region."""
    by_route: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_region: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for filename, v in verdicts.items():
        status = v.get("status")
        if status == "learning":
            continue
        entry = ledger_files.get(filename) or {}
        quarantined = status in QUARANTINE_STATUSES
        route = f"{v.get('platform')}_{v.get('source')}"
        region = region_of_tz(entry.get("tz"))
        by_route[route][0 if quarantined else 1] += 1
        by_region[region][0 if quarantined else 1] += 1
    return {
        "by_route": contingency({k: tuple(v) for k, v in by_route.items()}),
        "by_region": contingency({k: tuple(v) for k, v in by_region.items()}),
    }





# ---------------------------------------------------------------------------
# 5.3 Time zone resolution
# ---------------------------------------------------------------------------

def resolution_level(entry: dict) -> str:
    """Which resolution level a ledger entry's offset came from."""
    if entry.get("tz"):
        return "supplied_zone"
    if not has_provenance(entry):
        return "unknown_no_provenance"
    platform = entry.get("platform")
    source = entry.get("source")
    notes = str(entry.get("notes") or "")
    if platform == "youtube":
        return "export_native_ambiguous_label" if TZ_NOTE_PREFIX in notes else "export_native_utc"
    if platform == "instagram":
        return "export_native_utc"
    if platform == "tiktok" and source == "zeeschuimer":
        return "export_native_utc"
    if platform == "tiktok":
        return "inferred"
    return "unknown_no_provenance"





def resolution_levels(ledger_files: dict[str, dict]) -> dict[str, dict[str, int]]:
    """Files per resolution level, per route and in total."""
    per_route: dict[str, Counter] = defaultdict(Counter)
    for entry in ledger_files.values():
        per_route[route_of(entry)][resolution_level(entry)] += 1
    total = Counter()
    for c in per_route.values():
        total.update(c)
    per_route["all"] = total
    return {route: dict(sorted(c.items())) for route, c in per_route.items()}





def stored_offset_distribution(df: pd.DataFrame, ledger_files: dict[str, dict]) -> dict:
    """What the table carries as ``tz_offset`` for files without a supplied zone.

    Every file's stored offset is one integer (the per-file inference for
    every TikTok export ingested before v0.4, and the truncated zone offset
    since). This is the distribution of that integer over files and rows,
    for an analyst to hold against the zones the deployment's donors could
    plausibly be in.
    """
    if df.empty or "tz_offset" not in df.columns:
        return {"n_files": 0, "n_distinct_offsets": 0, "offsets": {}}
    supplied = {fn for fn, e in ledger_files.items() if e.get("tz")}
    sub_df = df[~df["raw_file"].isin(supplied)].dropna(subset=["tz_offset"])
    file_offset = sub_df.drop_duplicates(["raw_file", "tz_offset"])
    files_per_offset = {int(k): int(v) for k, v in file_offset.groupby("tz_offset").size().items()}
    rows_per_offset = {int(k): int(v) for k, v in sub_df.groupby("tz_offset").size().items()}
    offsets = {str(k): {"files": files_per_offset[k], "rows": rows_per_offset.get(k, 0)}
               for k in sorted(files_per_offset)}
    per_file_n = file_offset.groupby("raw_file").size()
    return {"n_files": int(sub_df["raw_file"].nunique()), "n_distinct_offsets": len(offsets), "offsets": offsets,
            "n_files_with_several_offsets": int((per_file_n > 1).sum())}





def calibrate_one_file(utc: pd.Series, tz_str: str, stored_offsets: list | None = None) -> dict:
    """Compare the inferred offset of one file with its supplied zone.

    The inference is recomputed from the file's UTC series and compared with
    the zone's offset at the file's median instant, both as floats, so
    daylight saving and half-hour zones are handled. The stored ``tz_offset``
    is never used for the comparison: it is integer hours, and for TikTok
    files ingested before v0.4 it is the inference itself; it is reported
    alongside when given. ``rows_in_other_dst_half_pct`` is the share of the
    file's rows whose true offset differs from the offset at the median
    event, i.e. the rows a per-file constant gets wrong even when it agrees.
    """
    from fyp.annotation.recode_variables import infer_timezone_offset
    from fyp.ingest.base import _zone_offset_hours, parse_donor_timezone

    # Materialise as a numpy tz-aware series: an Arrow-backed column from the
    # parquet reader compares per-row offsets differently and misreports the
    # daylight-saving share.
    utc = (pd.to_datetime(pd.Series(utc).dropna(), utc=True).astype("datetime64[ns, UTC]")
           .sort_values().reset_index(drop=True))
    zone = parse_donor_timezone(tz_str)
    if zone is None or len(utc) == 0:
        return {"n_events": len(utc), "inferred": None, "zone_offset": None,
                "diff": None, "agree": None, "off_gt_1h": None}
    inferred = float(infer_timezone_offset(utc))
    median_ts = utc.iloc[len(utc) // 2]
    zone_offset = float(_zone_offset_hours(pd.Series([median_ts]), zone).iloc[0])
    per_row = _zone_offset_hours(utc, zone).astype(float)
    other_half = float((per_row != zone_offset).mean()) * 100.0
    diff = inferred - zone_offset
    return {
        "n_events": len(utc),
        "inferred": inferred,
        "zone_offset": zone_offset,
        "diff": round(diff, 2),
        "agree": abs(diff) < 0.25,
        "off_gt_1h": abs(diff) > 1.0,
        "rows_in_other_dst_half_pct": round(other_half, 1),
        "stored_offsets": sorted(int(v) for v in stored_offsets) if stored_offsets is not None else None,
    }





def calibration_summary(per_file: list[dict]) -> dict:
    """Aggregate per-file calibration results."""
    usable = [r for r in per_file if r.get("diff") is not None]
    n = len(usable)
    return {
        "n_files_with_supplied_zone": len(per_file),
        "n_calibrated": n,
        "agree_pct": round(100.0 * sum(1 for r in usable if r["agree"]) / n, 1) if n else None,
        "off_gt_1h_pct": round(100.0 * sum(1 for r in usable if r["off_gt_1h"]) / n, 1) if n else None,
        "median_abs_diff_h": _median([abs(r["diff"]) for r in usable]),
        "rows_in_other_dst_half_pct_range": [min(r["rows_in_other_dst_half_pct"] for r in usable),
                                             max(r["rows_in_other_dst_half_pct"] for r in usable)] if usable else None,
    }





# ---------------------------------------------------------------------------
# 5.4 Sensitivity
# ---------------------------------------------------------------------------

def session_stats(df: pd.DataFrame, gaps: tuple[int, ...] = SESSION_GAPS) -> dict:
    """Session counts and lengths at several gap thresholds from one gap series.

    Sorts once, computes each collection's inter-event gaps once, and applies
    every threshold to the same series; equivalent to
    ``fyp.ingest.base.assign_session_ids`` run per threshold.
    """
    out: dict = {"n_rows": len(df), "n_collections": int(df["collection_id"].nunique()) if len(df) else 0}
    if df.empty:
        return out
    ordered = df.sort_values(["collection_id", "utc_timestamp"], kind="mergesort")
    ts = pd.to_datetime(ordered["utc_timestamp"], utc=True)
    gap = ts.groupby(ordered["collection_id"]).diff().dt.total_seconds()
    for threshold in gaps:
        session_break = gap.isna() | (gap > threshold)
        session_num = session_break.groupby(ordered["collection_id"]).cumsum()
        key = ordered["collection_id"].astype(str) + "__" + session_num.astype(int).astype(str)
        per_session = ts.groupby(key.values).agg(["min", "max", "size"])
        length_s = (per_session["max"] - per_session["min"]).dt.total_seconds()
        per_collection = pd.Series(key.values).groupby(ordered["collection_id"].values).nunique()
        out[f"gap_{threshold}s"] = {
            "n_sessions": len(per_session),
            "sessions_per_collection_median": float(per_collection.median()),
            "sessions_per_collection_mean": round(float(per_collection.mean()), 2),
            "session_length_median_s": float(length_s.median()),
            "session_events_median": float(per_session["size"].median()),
            "singleton_share_pct": round(100.0 * float((per_session["size"] == 1).mean()), 2),
        }
    return out





def comment_gap_stats(df: pd.DataFrame, gaps: tuple[int, ...] = COMMENT_GAPS) -> dict:
    """How many TikTok comments the forward fill links at several windows.

    Every TikTok comment's video id is inferred (the export gives none), so the
    fill is recomputed from scratch per window using the non-comment rows as
    the only id sources: within a raw file, consecutive rows closer than the
    window form one group and a comment takes the last id seen in its group.
    A second, simpler definition (gap to the preceding play in the same
    collection) is reported alongside for readers who think in those terms.
    """
    out: dict = {"n_comments": 0}
    if df.empty:
        return out
    ordered = df.sort_values(["raw_file", "utc_timestamp"], kind="mergesort").reset_index(drop=True)
    ts = pd.to_datetime(ordered["utc_timestamp"], utc=True)
    is_comment = ordered["activity_type"] == "comment"
    out["n_comments"] = int(is_comment.sum())
    out["n_comments_null_item_id"] = int((is_comment & ordered["item_id"].isna()).sum())
    first_play = ts.where(ordered["activity_type"] == "play").groupby(ordered["raw_file"]).transform("min")
    before = is_comment & first_play.notna() & (ts < first_play)
    out["n_comments_before_first_play"] = int(before.sum())
    out["n_comments_in_files_without_plays"] = int((is_comment & first_play.isna()).sum())
    if out["n_comments"]:
        out["before_first_play_pct"] = round(100.0 * out["n_comments_before_first_play"] / out["n_comments"], 1)
    if "link_method" in ordered.columns:
        out["n_comments_marked_ffill_180s"] = int((is_comment & (ordered["link_method"] == "ffill_180s")).sum())
    if out["n_comments"] == 0:
        return out
    source_id = ordered["item_id"].where(~is_comment)
    delta = ts.groupby(ordered["raw_file"]).diff().dt.total_seconds()
    for window in gaps:
        group_break = delta.isna() | (delta > window)
        group = group_break.groupby(ordered["raw_file"]).cumsum()
        filled = source_id.groupby([ordered["raw_file"], group]).ffill()
        linked = int((is_comment & filled.notna()).sum())
        out[f"window_{window}s"] = {
            "linked": linked,
            "linked_pct": round(100.0 * linked / out["n_comments"], 1),
        }
    by_coll = df.sort_values(["collection_id", "utc_timestamp"], kind="mergesort").reset_index(drop=True)
    ts_c = pd.to_datetime(by_coll["utc_timestamp"], utc=True)
    play_ts = ts_c.where(by_coll["activity_type"] == "play")
    last_play = play_ts.groupby(by_coll["collection_id"]).ffill()
    gap_to_play = (ts_c - last_play).dt.total_seconds()
    is_comment_c = by_coll["activity_type"] == "comment"
    for window in gaps:
        linked = int((is_comment_c & (gap_to_play <= window)).sum())
        out[f"window_{window}s"]["linked_to_preceding_play_same_collection_pct"] = \
            round(100.0 * linked / out["n_comments"], 1)
    return out





def timestamp_overlaps(frame, min_events: int = 1):
    """Pairwise per-second timestamp-set overlap between raw files.

    The same statistic as ``identify_similar_file_content`` (shared distinct
    seconds divided by the smaller file's distinct seconds) computed with a
    self-join instead of nested set intersections.

    Args:
        frame: A polars DataFrame with ``raw_file`` and ``utc_timestamp``.
        min_events: Ignore files with fewer distinct seconds than this.

    Returns:
        A polars DataFrame with ``a``, ``b``, ``shared``, ``n_a``, ``n_b``, ``overlap``.
    """
    import polars as pl

    secs = (
        frame.select(
            pl.col("raw_file").cast(pl.Utf8),
            (pl.col("utc_timestamp").cast(pl.Datetime("us", "UTC")).dt.epoch("s")).alias("sec"),
        )
        .drop_nulls()
        .unique()
    )
    counts = secs.group_by("raw_file").len().rename({"len": "n"}).filter(pl.col("n") >= min_events)
    secs = secs.join(counts.select("raw_file"), on="raw_file")
    pairs = (
        secs.join(secs, on="sec", suffix="_b")
        .filter(pl.col("raw_file") < pl.col("raw_file_b"))
        .group_by(["raw_file", "raw_file_b"]).len().rename({"len": "shared"})
        .join(counts.rename({"raw_file": "raw_file", "n": "n_a"}), on="raw_file")
        .join(counts.rename({"raw_file": "raw_file_b", "n": "n_b"}), on="raw_file_b")
        .with_columns((pl.col("shared") / pl.min_horizontal("n_a", "n_b")).alias("overlap"))
        .rename({"raw_file": "a", "raw_file_b": "b"})
        .sort("overlap", descending=True)
    )
    return pairs





def union_find_merges(
    pairs: list[tuple[str, str, float]],
    threshold: float,
    events_per_file: dict[str, int],
    user_id_per_file: dict[str, str | None],
    platform_per_file: dict[str, str] | None = None,
    collection_per_file: dict[str, str] | None = None,
    shared_per_pair: dict[tuple[str, str], int] | None = None,
    route_per_file: dict[str, str] | None = None,
) -> dict:
    """Cluster files whose overlap exceeds ``threshold`` and describe the merges.

    Besides the merge counts, reports how many merges would join files that
    production keeps in different collections (the merges the threshold
    would newly cause), how many qualifying pairs rest on two shared seconds
    or fewer (the small-file coincidence the ratio cannot see), how many
    qualifying pairs cross collection routes (production merges within a
    route only, so those pairs are never candidates), and how the
    qualifying pairs split by account relation (same account, different
    accounts, or no account on record for at least one file).
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    n_pairs = 0
    n_cross_platform = 0
    n_cross_route = 0
    n_tiny = 0
    relation: Counter = Counter()
    for a, b, overlap in pairs:
        if overlap <= threshold:
            continue
        n_pairs += 1
        if platform_per_file and platform_per_file.get(a) != platform_per_file.get(b):
            n_cross_platform += 1
        if route_per_file and route_per_file.get(a) != route_per_file.get(b):
            n_cross_route += 1
        if shared_per_pair and shared_per_pair.get((a, b), 3) <= 2:
            n_tiny += 1
        ua, ub = user_id_per_file.get(a), user_id_per_file.get(b)
        relation["same_account" if ua and ub and ua == ub else "different_accounts" if ua and ub else "account_unknown"] += 1
        parent[find(a)] = find(b)
    clusters: dict[str, list[str]] = defaultdict(list)
    for x in list(parent):
        clusters[find(x)].append(x)
    multi = [sorted(c) for c in clusters.values() if len(c) > 1]
    small = sum(1 for c in multi if any(events_per_file.get(f, 0) < SMALL_FILE_EVENTS for f in c))
    false = 0
    for c in multi:
        users = {user_id_per_file.get(f) for f in c if user_id_per_file.get(f)}
        if len(users) > 1:
            false += 1
    spanning = 0
    if collection_per_file:
        spanning = sum(1 for c in multi if len({collection_per_file.get(f) for f in c}) > 1)
    return {
        "threshold": threshold,
        "n_pairs_above_threshold": n_pairs,
        "n_pairs_on_two_shared_seconds_or_fewer": n_tiny,
        "n_merges": len(multi),
        "n_files_merged": sum(len(c) for c in multi),
        "n_merges_spanning_collections": spanning,
        "n_merges_involving_small_file": small,
        "n_false_merges_different_accounts": false,
        "n_cross_platform_pairs": n_cross_platform,
        "n_cross_route_pairs": n_cross_route,
        "pairs_by_account_relation": dict(relation),
    }





def ledger_merge_truth(ledger_files: dict[str, dict]) -> dict:
    """Production merges as the ledger recorded them (the 20 % rule at ingest)."""
    merged_files = {fn for fn, e in ledger_files.items() if e.get("merged_with_siblings")}
    clusters: dict[str, set[str]] = defaultdict(set)
    for fn in merged_files:
        e = ledger_files[fn]
        cid = e.get("collection_id") or fn
        clusters[cid].add(fn)
        clusters[cid].update(e.get("merged_with_siblings") or [])
    return {
        "n_files_merged_with_siblings": len(merged_files),
        "n_clusters": len(clusters),
        "rows_deduplicated": sum(int(e.get("deduped_rows") or 0) for e in ledger_files.values()),
    }





def account_per_file(ledger_files: dict[str, dict], collection_per_file: dict[str, str] | None,
                     tags: dict[str, dict] | None) -> dict[str, str | None]:
    """The participant account behind each file: the ledger's ``user_id``, else
    the collection's ``user_id`` in ``collections_tags.json``."""
    out: dict[str, str | None] = {fn: e.get("user_id") for fn, e in ledger_files.items()}
    for fn, cid in (collection_per_file or {}).items():
        if not out.get(fn):
            out[fn] = ((tags or {}).get(cid) or {}).get("user_id")
    return out





def overlap_sensitivity(pairs: list[tuple[str, str, float]], ledger_files: dict[str, dict],
                        events_per_file: dict[str, int], platform_per_file: dict[str, str] | None = None,
                        collection_per_file: dict[str, str] | None = None,
                        shared_per_pair: dict[tuple[str, str], int] | None = None,
                        route_per_file: dict[str, str] | None = None,
                        tags: dict[str, dict] | None = None) -> dict:
    """Merges at each threshold plus the overlap distribution and the ledger's ground truth.

    ``thresholds`` counts every pair in the table; ``thresholds_within_route``
    restricts to pairs on the same collection route, which is the only kind
    production compares (``identify_similar_file_content`` runs per
    sub-collection).
    """
    users = account_per_file(ledger_files, collection_per_file, tags)
    overlaps = sorted(o for _, _, o in pairs)
    out = {
        "note": "recomputed on the post-deduplication activity table, so shared rows of a merged "
                "re-donation are already collapsed; the ledger figures are what production did",
        "n_pairs": len(pairs),
        "overlap_quantiles": {q: round(float(np.quantile(overlaps, q)), 4) for q in (0.5, 0.9, 0.99)} if overlaps else {},
        "n_pairs_over_0_05": sum(1 for o in overlaps if o > 0.05),
        "ledger": ledger_merge_truth(ledger_files),
        "thresholds": {str(t): union_find_merges(pairs, t, events_per_file, users, platform_per_file,
                                                  collection_per_file, shared_per_pair, route_per_file)
                       for t in OVERLAP_THRESHOLDS},
    }
    if route_per_file:
        within = [(a, b, o) for a, b, o in pairs if route_per_file.get(a) == route_per_file.get(b)]
        out["n_pairs_within_route"] = len(within)
        out["thresholds_within_route"] = {
            str(t): union_find_merges(within, t, events_per_file, users, platform_per_file,
                                      collection_per_file, shared_per_pair, route_per_file)
            for t in OVERLAP_THRESHOLDS}
    return out





# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.1f}" if abs(value) >= 100 else f"{value:g}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)





def _md_table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_fmt(v) for v in row) + " |")
    return "\n".join(lines)





def render_tables_md(report: dict) -> str:
    """Render the report as markdown, one section per part of §5, with caveats."""
    parts = ["# Intake report", "",
             f"Snapshot: `{report['snapshot'].get('snapshot_root')}` · repo `{report['snapshot'].get('git_head')}` · "
             f"run {report['snapshot'].get('run_at')}", ""]
    att = report["attrition"]
    routes = [r for r in att if r != "all"] + ["all"]
    parts += ["## 5.1 Intake by route (Table 3)", "",
              _md_table(["Route", "Files", "No counts", "No breakdown", "Rows read", "Outside whitelist", "Not parseable",
                         "Missing required", "Deduplicated", "Lost, pre-breakdown ledger", "Kept", "Kept %", "Unaccounted"],
                        [[r, att[r]["files"], att[r]["files_without_counts"], att[r]["files_without_breakdown"],
                          att[r]["rows_read"], att[r]["dropped_outside_whitelist"], att[r]["dropped_not_parseable"],
                          att[r]["dropped_missing_required"], att[r]["deduped"], att[r]["unattributed_pre_breakdown"],
                          att[r]["kept"], att[r]["kept_pct"], att[r]["unaccounted"]] for r in routes]),
              "", "Files without counts were migrated from the legacy discard list and contribute no rows. "
              "Files without breakdown were ledgered before drop reasons were recorded: their rows read minus rows kept "
              "is reported as lost without attribution.", ""]
    comp = report.get("composition")
    if comp:
        parts += ["### Activity table composition", "",
                  _md_table(["Route", "Rows", "Collections", "Raw files", "First event", "Last event", "Null activity type"],
                            [[r, c["rows"], c["collections"], c["raw_files"], c["first_event"], c["last_event"],
                              f"{c['null_activity_type']['rows']} rows / {c['null_activity_type']['files']} files"]
                             for r, c in comp.items()]), ""]
        for r, c in comp.items():
            parts += [f"{r} by activity type: {c['activity_types']}", ""]
    rec = report.get("table_reconciliation")
    if rec:
        parts += ["### Table vs ledger", "",
                  f"Files in the table {rec['n_files_in_table']}: with a ledger entry {rec['n_in_table_with_entry']} "
                  f"(with counts {rec['n_in_table_with_counts']}), without {rec['n_in_table_without_entry']}. "
                  f"Counted ledger entries {rec['n_counted_entries']}; not in the table by outcome: "
                  f"{rec['counted_entries_not_in_table_by_outcome']}.", ""]
    outc = report["outcomes"]
    all_outcomes = sorted({o for r in outc.values() for o in r})
    parts += ["## 5.2 Outcomes (Table 4)", "",
              _md_table(["Route", *all_outcomes],
                        [[r] + [outc[r].get(o, 0) for o in all_outcomes] for r in routes if r in outc]), ""]
    sd = report["sentinel"]["denominators"]
    parts += ["### Sentinel denominators", "",
              _md_table(["Baseline", "Verdicts", "Learning", "Structure-eligible", "Stats-eligible", "Bootstrapped"],
                        [[k, v["n_verdicts"], v["n_learning"], v["n_structure_eligible"], v["n_stats_eligible"],
                          v["n_bootstrapped"]] for k, v in sd.items()]), ""]
    q = report["sentinel"]["quarantine"]
    parts += ["### Quarantine", "",
              _md_table(["Class", "Meaning", "Files"],
                        [[c, CLASS_LABELS[c], q["classes"][c]] for c in CLASS_CODES]),
              "",
              f"Ever quarantined: {q['n_files_ever_quarantined']} · pending: {q['n_pending']} · "
              f"approved: {q['n_approved']} (of which warn-only: {q['n_warn_only_approvals']}) · "
              f"rejected: {q['n_rejected']} · unclassified approved: {q['n_unclassified_approved']} "
              f"({q['classification_source']}).",
              f"Time in quarantine (reviewed files, days): median {_fmt(q['days_in_quarantine_median'])}, "
              f"max {_fmt(q['days_in_quarantine_max'])}, n={q['n_reviewed_with_duration']}; "
              f"negative durations (contradicting timestamps): {q['n_negative_durations']}; "
              f"start sources: {q['start_sources']}.",
              "", "Findings on a stored verdict are those of the latest evaluation.", ""]
    fc = report["sentinel"]["findings"]["files_with_code"]
    parts += ["### Findings by layer and code (files)", "",
              _md_table(["Status", "layer:code", "Files"],
                        [[s, code, n] for s, codes in fc.items() for code, n in codes.items()]), ""]
    for name in ("by_route", "by_region"):
        ct = report["sentinel"]["contingency"][name]
        parts += [f"### Quarantined vs accepted {name.replace('_', ' ')}", "",
                  _md_table(["Key", "Quarantined", "Accepted"], [[k, v[0], v[1]] for k, v in ct["table"].items()]),
                  "", f"Test: {ct.get('test')} statistic {ct.get('statistic')} p={ct.get('p')} "
                      f"(min expected {ct.get('min_expected')}; Fisher p={ct.get('fisher_p', 'n/a')}). {ct.get('note', '')}", ""]
    wh = report["sentinel"]["withheld"]
    parts += [f"Withheld sections: {wh['n_files_with_withheld_sections']} files; most common: "
              f"{list(wh['sections'].items())[:5]}", ""]
    res = report["resolution"]
    levels = sorted({lvl for r in res.values() for lvl in r})
    parts += ["## 5.3 Time zone resolution", "",
              _md_table(["Route", *levels], [[r] + [res[r].get(level, 0) for level in levels] for r in routes if r in res]), ""]
    cal = report.get("calibration")
    if cal:
        parts += [f"Calibration of the inference against supplied zones: n={cal['n_calibrated']} of "
                  f"{cal['n_files_with_supplied_zone']} files; agree (<15 min) {cal['agree_pct']} %; "
                  f"off by more than 1 h {cal['off_gt_1h_pct']} %; median |diff| {cal['median_abs_diff_h']} h; "
                  f"rows in the other daylight-saving half {cal.get('rows_in_other_dst_half_pct_range')} %.", ""]
        per = report.get("calibration_per_file") or []
        if per:
            parts += [_md_table(["Zone", "Events", "Inferred", "Zone offset at median", "Diff", "Stored", "Other DST half %"],
                                [[r.get("tz"), r["n_events"], r["inferred"], r["zone_offset"], r["diff"],
                                  r.get("stored_offsets"), r.get("rows_in_other_dst_half_pct")] for r in per]), ""]
    so = report.get("stored_offsets")
    if so:
        for route, d in so.items():
            parts += [f"Stored per-file offsets, {route}, files without a supplied zone: {d['n_files']} files, "
                      f"{d['n_distinct_offsets']} distinct offsets, {d.get('n_files_with_several_offsets', 0)} files with several; "
                      f"{d['offsets']}", ""]
    ses = report.get("sessions")
    if ses:
        parts += ["## 5.4 Sensitivity", "", "### Session gap (all activity rows)", ""]
        for platform, s in ses.items():
            rows = [[f"{g} s", s[f'gap_{g}s']["n_sessions"], s[f'gap_{g}s']["sessions_per_collection_median"],
                     s[f'gap_{g}s']["session_length_median_s"], s[f'gap_{g}s']["session_events_median"],
                     s[f'gap_{g}s']["singleton_share_pct"]] for g in SESSION_GAPS if f"gap_{g}s" in s]
            parts += [f"{platform}: {s['n_rows']:,} rows, {s['n_collections']} collections", "",
                      _md_table(["Gap", "Sessions", "Per collection (median)", "Length median s",
                                 "Events median", "Singletons %"], rows), ""]
    ses_p = report.get("sessions_plays_only")
    if ses_p:
        parts += ["### Session gap (play and observe rows only)", ""]
        for platform, s in ses_p.items():
            rows = [[f"{g} s", s[f'gap_{g}s']["n_sessions"], s[f'gap_{g}s']["sessions_per_collection_median"],
                     s[f'gap_{g}s']["session_length_median_s"], s[f'gap_{g}s']["session_events_median"],
                     s[f'gap_{g}s']["singleton_share_pct"]] for g in SESSION_GAPS if f"gap_{g}s" in s]
            parts += [f"{platform}: {s['n_rows']:,} rows, {s['n_collections']} collections", "",
                      _md_table(["Gap", "Sessions", "Per collection (median)", "Length median s",
                                 "Events median", "Singletons %"], rows), ""]
    com = report.get("comments")
    if com and com.get("n_comments"):
        parts += ["### Comment link window (TikTok)", "",
                  f"Comments {com['n_comments']:,}; null item id {com['n_comments_null_item_id']:,}; "
                  f"marked ffill_180s {com.get('n_comments_marked_ffill_180s', 0):,} (rows ingested from v0.4 only); "
                  f"timestamped before the file's first play {com.get('n_comments_before_first_play', 0):,} "
                  f"({com.get('before_first_play_pct')} %); in files without plays {com.get('n_comments_in_files_without_plays', 0):,}.", "",
                  _md_table(["Window", "Linked", "Linked %", "Preceding play in collection %"],
                            [[f"{w} s", com[f'window_{w}s']["linked"], com[f'window_{w}s']["linked_pct"],
                              com[f'window_{w}s'].get("linked_to_preceding_play_same_collection_pct")] for w in COMMENT_GAPS]), ""]
    ov = report.get("overlap")
    if ov:
        parts += ["### Donor-merge overlap", "", ov["note"], "",
                  f"Pairs {ov['n_pairs']:,}; overlap quantiles {ov['overlap_quantiles']}; pairs over 0.05: {ov['n_pairs_over_0_05']}; "
                  f"ledger: {ov['ledger']}", "",
                  _md_table(["Threshold", "Pairs above", "On <=2 shared seconds", "Cross-route pairs", "Merges", "Files merged",
                             "Spanning collections", "With a file <30 events", "Different accounts", "Cross-platform pairs",
                             "Pairs by account relation"],
                            [[t, v["n_pairs_above_threshold"], v["n_pairs_on_two_shared_seconds_or_fewer"], v["n_cross_route_pairs"],
                              v["n_merges"], v["n_files_merged"], v["n_merges_spanning_collections"], v["n_merges_involving_small_file"],
                              v["n_false_merges_different_accounts"], v["n_cross_platform_pairs"], v["pairs_by_account_relation"]]
                             for t, v in ov["thresholds"].items()]), ""]
        if ov.get("thresholds_within_route"):
            parts += [f"Within-route pairs only (what production compares): {ov['n_pairs_within_route']:,} pairs", "",
                      _md_table(["Threshold", "Pairs above", "On <=2 shared seconds", "Merges", "Files merged",
                                 "Spanning collections", "With a file <30 events", "Different accounts", "Pairs by account relation"],
                                [[t, v["n_pairs_above_threshold"], v["n_pairs_on_two_shared_seconds_or_fewer"], v["n_merges"],
                                  v["n_files_merged"], v["n_merges_spanning_collections"], v["n_merges_involving_small_file"],
                                  v["n_false_merges_different_accounts"], v["pairs_by_account_relation"]]
                                 for t, v in ov["thresholds_within_route"].items()]), ""]
    return "\n".join(parts)





def render_funnel_svg(attrition: dict[str, dict], placeholder: bool = False) -> str:
    """Draw one 100 % bar per route with the losses named beside it (SVG string)."""
    routes = [r for r in attrition if r != "all" and attrition[r]["rows_read"] > 0]
    routes.sort(key=lambda r: -attrition[r]["rows_read"])
    row_h, top, left, bar_w = 78, 70, 250, 520
    height = top + row_h * max(len(routes), 1) + 40
    segs = [("kept", "Rows kept", "#2f5d50"), ("deduped", "duplicates of rows already held", "#8f9a95"),
            ("dropped_missing_required", "missing a required field", "#b7bfbb"),
            ("dropped_not_parseable", "not interpretable by the parser", "#d6dbd8")]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="{height}" viewBox="0 0 1000 {height}" '
           'font-family="Helvetica, Arial, sans-serif" font-size="12">']
    x = left
    for _key, label, colour in segs:
        out.append(f'<rect x="{x}" y="24" width="12" height="12" fill="{colour}"/>')
        out.append(f'<text x="{x + 16}" y="34" fill="#1a1a1a" font-size="11">{label}</text>')
        x += 16 + 7 * len(label) + 20
    for i, route in enumerate(routes):
        a = attrition[route]
        y = top + i * row_h
        out.append(f'<text x="{left - 14}" y="{y + 22}" text-anchor="end" fill="#1a1a1a">{route.replace("_", " ")}</text>')
        out.append(f'<text x="{left - 14}" y="{y + 38}" text-anchor="end" fill="#6b6b6b" font-size="11">'
                   f'{a["files"]:,} files · {a["rows_read"]:,} rows read</text>')
        cx = left
        notes = []
        for key, label, colour in segs:
            share = a[key] / a["rows_read"]
            w = bar_w * share
            if w > 0:
                out.append(f'<rect x="{cx:.1f}" y="{y + 8}" width="{max(w, 0.5):.1f}" height="40" fill="{colour}" stroke="#fff"/>')
            if key == "kept":
                out.append(f'<text x="{cx + w / 2:.1f}" y="{y + 33}" text-anchor="middle" fill="#fff">{100 * share:.1f}% kept</text>')
            else:
                notes.append(f"{100 * share:.1f}% {label}")
            cx += w
        unacc = a["unaccounted"] / a["rows_read"] if a["rows_read"] else 0
        if unacc > 0.0005:
            out.append(f'<rect x="{cx:.1f}" y="{y + 8}" width="{bar_w * unacc:.1f}" height="40" fill="#fff" stroke="#6b6b6b" stroke-dasharray="3 2"/>')
            notes.append(f"{100 * unacc:.1f}% unaccounted")
        for j, note in enumerate(notes):
            out.append(f'<text x="{left + bar_w + 14}" y="{y + 20 + 14 * j}" fill="#3a3a3a" font-size="11">{note}</text>')
    if placeholder:
        out.append(f'<text x="500" y="{height / 2:.0f}" text-anchor="middle" fill="#a03530" fill-opacity="0.35" '
                   'font-size="28" font-weight="bold" transform="rotate(-12 500 300)">PLACEHOLDER: SYNTHETIC LEDGER</text>')
    out.append("</svg>")
    return "\n".join(out)





def worksheet_rows(rows: list[dict]) -> list[dict]:
    """Project quarantine rows onto the worksheet columns, in order."""
    return [{col: ("" if r.get(col) is None else r.get(col)) for col in WORKSHEET_COLUMNS} for r in rows]





def read_classification(text: str, approved_files: set[str]) -> dict[str, str]:
    """Read a filled-in worksheet; fail loudly on bad codes or unclassified approved files."""
    reader = csv.DictReader(io.StringIO(text))
    classes: dict[str, str] = {}
    bad = []
    for row in reader:
        code = (row.get("class") or "").strip().lower()
        if not code:
            continue
        if code not in CLASS_CODES:
            bad.append(f"{row.get('filename')}: '{code}'")
            continue
        classes[row["filename"]] = code
    if bad:
        raise ValueError("unknown class codes: " + "; ".join(bad))
    missing = sorted(f for f in approved_files if f not in classes)
    if missing:
        raise ValueError("approved files without a class: " + ", ".join(missing))
    return classes





# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def write_snapshot_config(repo_root: Path, snapshot_root: Path, out_dir: Path) -> Path:
    """Write a throwaway config dir that points every storage location at the snapshot.

    Returns the path to the ``config.toml`` copy; its grandparent becomes the
    project root when ``FYP_CONFIG_PATH`` names it, and the overlay beside it
    is the only local overlay the config loader will see.
    """
    cfg_dir = out_dir / "_snapshot_config" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repo_root / "config" / "config.toml", cfg_dir / "config.toml")
    media_dir = out_dir / "_snapshot_config" / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    overlay = (
        "# Written by scripts/intake_report.py: read-only snapshot wiring.\n"
        "[paths]\n"
        f'local_data = "{snapshot_root.resolve()}"\n'
        f'local_media = "{media_dir.resolve()}"\n'
        "[misc]\n"
        "local_mode = true\n"
        "[data_io]\n"
        "use_gcs_for_data = false\n"
        "use_gcs_for_cache = false\n"
        "use_gcs_for_media = false\n"
    )
    (cfg_dir / "config.local.toml").write_text(overlay)
    return cfg_dir / "config.toml"





def assert_snapshot_storage(snapshot_root: Path) -> None:
    """Refuse unless every storage location resolves inside the snapshot."""
    if os.environ.get("FYP_FORCE_GCS") or os.environ.get("K_SERVICE"):
        raise SystemExit("REFUSING: FYP_FORCE_GCS or K_SERVICE is set; this script reads snapshots only.")
    from tests._storage_guard import assert_local_storage
    assert_local_storage()
    from fyp.fyp_config import get_config
    cf = get_config()
    recoded = Path(cf["paths"]["recoded"]).resolve()
    if snapshot_root.resolve() not in recoded.parents:
        raise SystemExit(f"REFUSING: 'recoded' resolves to {recoded}, not inside the snapshot {snapshot_root}.")
    if cf.get("data_io", {}).get("bucket"):
        raise SystemExit("REFUSING: a GCS bucket is configured.")





@dataclass
class Inputs:
    """The snapshot's JSON stores plus provenance about the files read."""

    ledger: dict[str, dict]
    verdicts: dict[str, dict]
    baselines: dict[str, dict]
    tags: dict[str, dict]
    provenance: dict[str, dict] = field(default_factory=dict)





def load_inputs() -> Inputs:
    """Load the four JSON stores through data_io and record their fingerprints."""
    import fyp.data_io as data_io

    def load(name: str, required: bool) -> dict:
        if not data_io.exists(RECODED, name):
            if required:
                raise SystemExit(f"missing {RECODED}/{name} in the snapshot")
            return {}
        return data_io.load_json(RECODED, name) or {}

    provenance = {}
    for name in (LEDGER_FILENAME, VERDICTS_FILENAME, BASELINES_FILENAME, TAGS_FILENAME, PARQUET_FILENAME):
        st = data_io.stat(RECODED, name)
        if st:
            provenance[name] = {"size": int(st["size"]),
                                "mtime": datetime.fromtimestamp(st["mtime"], tz=UTC).isoformat()}
    ledger = load(LEDGER_FILENAME, required=True)
    verdicts = load(VERDICTS_FILENAME, required=False)
    baselines = load(BASELINES_FILENAME, required=False)
    tags = load(TAGS_FILENAME, required=False)
    return Inputs(
        ledger=ledger.get("files") or {},
        verdicts=verdicts.get("files") or {},
        baselines=baselines.get("baselines") or {},
        tags=tags if isinstance(tags, dict) else {},
        provenance=provenance,
    )





def git_parser_commits(repo_root: Path) -> list[dict]:
    """Commits touching the parsers or the sentinel, newest first."""
    proc = subprocess.run(
        ["git", "log", "--date=iso-strict", "--format=%H%x09%ad%x09%s", "--", *PARSER_PATHS],
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    return parse_git_log(proc.stdout) if proc.returncode == 0 else []





def git_head(repo_root: Path) -> str:
    proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root,
                          capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"





def load_platform_frame(platform: str, columns: list[str]) -> pd.DataFrame:
    """One platform's activity rows with only the requested columns."""
    import fyp.data_io as data_io
    return data_io.load_parquet_selective(RECODED, PARQUET_FILENAME, columns=columns,
                                          filters=[("source_platform", "==", platform)])





def list_platforms() -> list[str]:
    """Distinct ``source_platform`` values in the activity table."""
    import fyp.data_io as data_io
    df = data_io.load_parquet_selective(RECODED, PARQUET_FILENAME, columns=["source_platform"])
    return sorted(str(p) for p in df["source_platform"].dropna().unique())





def overlap_frame():
    """The activity table's ``raw_file`` and ``utc_timestamp`` columns as a polars frame."""
    import polars as pl
    import pyarrow as pa

    import fyp.data_io as data_io

    batches = list(data_io.iter_parquet_batches(RECODED, PARQUET_FILENAME, columns=["raw_file", "utc_timestamp"]))
    if not batches:
        return pl.DataFrame({"raw_file": [], "utc_timestamp": []})
    return pl.from_arrow(pa.Table.from_batches(batches))





def write_outputs(out_dir: Path, report: dict, tables_md: str, svg: str, rows: list[dict]) -> None:
    """Write the four outputs as plain files (these are not project storage)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    (out_dir / "tables.md").write_text(tables_md)
    (out_dir / "fig4_attrition_funnel.svg").write_text(svg)
    with (out_dir / "quarantine_worksheet.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(WORKSHEET_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)





def build_report(inputs: Inputs, commits: list[dict], classification: dict[str, str] | None,
                 platforms: list[str] | None, skip_parquet: bool,
                 exclude_routes: tuple[str, ...] = ()) -> tuple[dict, list[dict]]:
    """Compute every part of the report; returns the report and the worksheet rows."""
    if exclude_routes:
        inputs.ledger = {fn: e for fn, e in inputs.ledger.items() if route_of(e) not in exclude_routes}
        inputs.verdicts = {fn: v for fn, v in inputs.verdicts.items()
                           if f"{v.get('platform')}_{v.get('source')}" not in exclude_routes}
    rows = quarantine_rows(inputs.verdicts, inputs.ledger, inputs.baselines, commits)
    report: dict = {
        "attrition": attrition_by_route(inputs.ledger),
        "outcomes": outcomes_by_route(inputs.ledger),
        "sentinel": {
            "denominators": sentinel_denominators(inputs.verdicts, inputs.baselines),
            "quarantine": quarantine_summary(rows, classification),
            "findings": findings_by_layer_code(inputs.verdicts),
            "withheld": withheld_counts(inputs.verdicts, inputs.ledger),
            "contingency": quarantine_contingencies(inputs.verdicts, inputs.ledger),
        },
        "resolution": resolution_levels(inputs.ledger),
    }
    if skip_parquet:
        return report, rows
    platforms = platforms or list_platforms()
    events_per_file: dict[str, int] = {}
    platform_per_file: dict[str, str] = {}
    collection_per_file: dict[str, str] = {}
    route_per_file: dict[str, str] = {}
    stored_offsets: dict[str, dict] = {}
    calibration: list[dict] = []
    sessions: dict[str, dict] = {}
    sessions_plays: dict[str, dict] = {}
    comments: dict = {}
    composition: dict[str, dict] = {}
    for platform in platforms:
        df = load_platform_frame(platform, ["raw_file", "collection_id", "utc_timestamp", "activity_type",
                                            "item_id", "link_method", "data_source", "tz_offset"])
        if exclude_routes and "data_source" in df.columns:
            df = df[~(platform + "_" + df["data_source"].astype(str)).isin(exclude_routes)]
        if df.empty:
            continue
        for (src,), grp in df.groupby(["data_source"]):
            composition[f"{platform}_{src}"] = {
                "rows": len(grp), "collections": int(grp["collection_id"].nunique()),
                "raw_files": int(grp["raw_file"].nunique()),
                "first_event": str(grp["utc_timestamp"].min())[:10], "last_event": str(grp["utc_timestamp"].max())[:10],
                "activity_types": {str(k): int(v) for k, v in grp["activity_type"].value_counts(dropna=False).items()},
                "null_activity_type": null_activity_type_counts(grp),
            }
            for fn in grp["raw_file"].unique():
                route_per_file[str(fn)] = f"{platform}_{src}"
        for (src,), grp in df.groupby(["data_source"]):
            stored_offsets[f"{platform}_{src}"] = stored_offset_distribution(grp[["raw_file", "tz_offset"]], inputs.ledger)
        counts = df.groupby("raw_file").size()
        for fn, n in counts.items():
            events_per_file[str(fn)] = int(n)
            platform_per_file[str(fn)] = platform
        for fn, cid in df.groupby("raw_file")["collection_id"].first().items():
            collection_per_file[str(fn)] = str(cid)
        sessions[platform] = session_stats(df[["collection_id", "utc_timestamp"]])
        plays = df[df["activity_type"].isin(["play", "observe"])]
        sessions_plays[platform] = session_stats(plays[["collection_id", "utc_timestamp"]])
        if platform == "tiktok":
            comments = comment_gap_stats(df)
        supplied = {fn: e.get("tz") for fn, e in inputs.ledger.items() if e.get("tz")}
        for fn, sub in df[df["raw_file"].isin(list(supplied))].groupby("raw_file"):
            rec = calibrate_one_file(sub["utc_timestamp"], supplied[str(fn)],
                                     stored_offsets=sub["tz_offset"].dropna().unique().tolist())
            rec.update({"raw_file": str(fn), "platform": platform, "tz": supplied[str(fn)]})
            calibration.append(rec)
        del df
    pairs_df = timestamp_overlaps(overlap_frame())
    pairs = [(str(a), str(b), float(o)) for a, b, o in
             zip(pairs_df["a"].to_list(), pairs_df["b"].to_list(), pairs_df["overlap"].to_list(), strict=True)]
    shared = {(str(a), str(b)): int(s) for a, b, s in
              zip(pairs_df["a"].to_list(), pairs_df["b"].to_list(), pairs_df["shared"].to_list(), strict=True)}
    report["composition"] = composition
    report["table_reconciliation"] = table_reconciliation(inputs.ledger, route_per_file)
    report["stored_offsets"] = stored_offsets
    report["calibration"] = calibration_summary(calibration)
    report["calibration_per_file"] = calibration
    report["sessions"] = sessions
    report["sessions_plays_only"] = sessions_plays
    report["comments"] = comments
    report["overlap"] = overlap_sensitivity(pairs, inputs.ledger, events_per_file, platform_per_file,
                                            collection_per_file, shared, route_per_file, inputs.tags)
    return report, rows





def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", required=True, help="snapshot root holding recoded/")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--skip-parquet", action="store_true", help="ledger and verdict parts only")
    parser.add_argument("--classification", help="filled-in quarantine_worksheet.csv")
    parser.add_argument("--platforms", help="comma-separated platform list (default: all in the table)")
    parser.add_argument("--exclude-routes", default="", help="comma-separated platform_source routes to leave out, e.g. tiktok_demo")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    snapshot_root = Path(args.snapshot).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    if not (snapshot_root / RECODED).is_dir():
        raise SystemExit(f"{snapshot_root} has no {RECODED}/ directory")
    config_path = write_snapshot_config(repo_root, snapshot_root, out_dir)
    os.environ["FYP_CONFIG_PATH"] = str(config_path)
    assert_snapshot_storage(snapshot_root)

    inputs = load_inputs()
    commits = git_parser_commits(repo_root)
    classification = None
    if args.classification:
        approved = {fn for fn, v in inputs.verdicts.items() if v.get("status") == "approved"}
        classification = read_classification(Path(args.classification).read_text(), approved)
    platforms = [p.strip() for p in args.platforms.split(",")] if args.platforms else None
    exclude = tuple(r.strip() for r in args.exclude_routes.split(",") if r.strip())
    report, rows = build_report(inputs, commits, classification, platforms, args.skip_parquet, exclude)
    report["snapshot"] = {
        "snapshot_root": str(snapshot_root),
        "inputs": inputs.provenance,
        "git_head": git_head(repo_root),
        "run_at": datetime.now(UTC).isoformat(),
        "args": vars(args),
    }
    write_outputs(out_dir, report, render_tables_md(report), render_funnel_svg(report["attrition"]),
                  worksheet_rows(rows))
    print(f"wrote {out_dir}/report.json, tables.md, fig4_attrition_funnel.svg, quarantine_worksheet.csv")
    if report["sentinel"]["quarantine"]["n_unclassified_approved"]:
        print(f"{report['sentinel']['quarantine']['n_unclassified_approved']} approved file(s) await a class "
              f"in quarantine_worksheet.csv; re-run with --classification once filled in.")
    return 0





if __name__ == "__main__":
    sys.exit(main())
