"""Structure sentinel: detect silent format drift in donation uploads.

The three platforms' data-donation exports change format without notice. A
hard break raises in ``load_single_raw`` and the file stays pending, but a
silent change — the file still parses yet yields corrupt or partial rows —
would land in the activity data undetected. This module learns the structure
of past accepted uploads per ``(source_platform, data_source)`` and flags new
files that deviate, quarantining them until an admin reviews the deviation in
the Data Management tab.

Three detection layers:

1. **Raw structure** — zip member paths, recursive JSON key paths with leaf
   types, HTML structural markers (YouTube watch-history), NDJSON record keys.
2. **Parse-output sanity** — per-file stats such as rows-per-MB, the fraction
   of rows surviving processing (timestamp parse rate), null item_id fraction,
   and donated-seed fill rates.
3. **Cross-upload drift** — each new file's stats are compared against the
   learned distribution (running Welford moments + observed range) of prior
   accepted uploads of the same platform/source.

Persistence (both under the ``"recoded"`` data_io location, GCS-backed in
production):

- ``structure_baselines.json`` — the learned structure + stat distributions.
- ``structure_verdicts.json`` — per-file verdicts and review state; the
  Data Management UI's source of truth.

Quarantine itself is enforced by the ingestion ledger (outcome
``"quarantined_structure"`` is a member of ``fyp.ingest.LEDGER_SKIP_OUTCOMES``);
this module only produces verdicts and owns the approve/reject review flow.
"""

import json
import math
import re
from datetime import datetime, timezone

import pandas as pd

from fyp import data_io
from fyp.utils import read_zip_members


STRUCTURE_BASELINES_FILENAME = "structure_baselines.json"
STRUCTURE_VERDICTS_FILENAME = "structure_verdicts.json"
STORAGE_LOCATION = "recoded"

# Below this many accepted files a baseline is learn-only: every new file gets
# status "learning" and is ingested, never quarantined.
MIN_ACCEPTED_FOR_STRUCTURE_CHECKS = 3
# Stat-distribution checks need more history than structure checks.
MIN_ACCEPTED_FOR_STAT_CHECKS = 5

STAT_Z_WARN = 3.0
STAT_Z_QUARANTINE = 4.0
# A key path present in at least this fraction of accepted files is "core":
# its absence from a new file is a quarantine finding.
CORE_PATH_SUPPORT = 0.9

# Purely additive drift (new key paths / members / activity types) warns but
# still ingests by default. Flip to True to quarantine on any structural
# change whatsoever.
ADDITIVE_QUARANTINES = False

MAX_KEY_PATHS = 2000
MAX_LIST_SAMPLE = 50
MAX_NDJSON_SAMPLE = 200
MAX_DEPTH = 8
MAX_ACCEPTED_STRUCTURES = 50

# Metrics whose hard breach quarantines; every other tracked metric only warns.
HARD_STAT_METRICS = {
    "kept_ratio",
    "rows_per_mb",
    "null_item_id_frac",
    "html_ts_match_frac",
}
# Metrics bounded to [0, 1]; they get an absolute tolerance so an all-zero
# history (std = 0) doesn't hard-fail on a tiny non-zero value.
FRACTION_METRICS_PREFIXES = ("kept_ratio", "null_item_id_frac", "seed_fill.", "html_")
FRACTION_ABS_TOLERANCE = 0.05

# Separators used in fingerprint paths. "|" splits a typed path from its leaf
# type ("a.b[]|str"); "::" scopes a path to the zip member it came from.
TYPE_SEP = "|"
MEMBER_SEP = "::"

# Dict keys that embed per-donor data rather than schema (e.g. TikTok DDP's
# "Chat History with <username>:") are collapsed so they don't register as a
# new key path on every upload.
_DYNAMIC_KEY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)^chat history with .+$"), "chat history with *"),
]

# YouTube watch-history.html structural markers. Deliberately duplicated from
# fyp.ingest.YouTubeDDPCollection (importing ingest here would invert the
# dependency: ingest imports this module for its fingerprint hooks).
_HTML_OUTER_CELL_MARKER = '<div class="outer-cell'
_HTML_CAPTION_RE = re.compile(r'mdl-typography--caption">(.*?)</div>', re.S)
_HTML_VIDEO_RE = re.compile(r'watch\?v=([\w-]{11})')
_HTML_TS_RE = re.compile(
    r'(\d{1,2} [A-Za-z]{3,9} \d{4}|[A-Za-z]{3,9} \d{1,2}, \d{4}), '
    r'(\d{1,2}:\d{2}:\d{2})'
    r'(?:[\s\u202f\u00a0]*([APap][Mm]))?'
    r'[\s\u202f\u00a0]+([A-Z]{2,5}(?:[+-]\d{1,2}:?\d{2})?)'
)


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()





def _normalize_key(key: str) -> str:
    """Collapse per-donor dynamic dict keys to a stable wildcard form."""
    for pattern, replacement in _DYNAMIC_KEY_PATTERNS:
        if pattern.match(key):
            return replacement
    return key





def _leaf_type(value: object) -> str:
    """Name the leaf type of a JSON value for typed key paths."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    return type(value).__name__





def base_path_of(typed_path: str) -> str:
    """Strip the ``|type`` suffix from a typed key path."""
    return typed_path.rsplit(TYPE_SEP, 1)[0]





def key_paths_of(obj: object, prefix: str = "", depth: int = 0) -> set[str]:
    """Extract typed key paths from a parsed JSON object.

    Dicts descend as ``a.b``, lists as ``a[]`` (sampling the first
    ``MAX_LIST_SAMPLE`` elements), and leaves emit ``a.b[]|str`` with the leaf
    type appended after ``|``. Depth is capped at ``MAX_DEPTH`` (deeper
    containers are recorded as leaves of their container type) and the result
    set is capped at ``MAX_KEY_PATHS``.

    Args:
        obj: A parsed JSON value (dict/list/scalar).
        prefix: Path accumulated so far (used by the recursion).
        depth: Recursion depth (used by the recursion).

    Returns:
        The set of typed key paths found in ``obj``.
    """
    paths: set[str] = set()
    if depth >= MAX_DEPTH or not isinstance(obj, (dict, list)) or not obj:
        paths.add(f"{prefix or '$'}{TYPE_SEP}{_leaf_type(obj)}")
        return paths

    if isinstance(obj, dict):
        for key, value in obj.items():
            key = _normalize_key(str(key))
            child_prefix = f"{prefix}.{key}" if prefix else key
            paths.update(key_paths_of(value, child_prefix, depth + 1))
            if len(paths) >= MAX_KEY_PATHS:
                break
    else:
        child_prefix = f"{prefix}[]" if prefix else "$[]"
        for item in obj[:MAX_LIST_SAMPLE]:
            paths.update(key_paths_of(item, child_prefix, depth + 1))
            if len(paths) >= MAX_KEY_PATHS:
                break

    if len(paths) > MAX_KEY_PATHS:
        paths = set(sorted(paths)[:MAX_KEY_PATHS])
    return paths





def fingerprint_json_payload(payload: object) -> dict:
    """Fingerprint a single parsed JSON document.

    Args:
        payload: The parsed JSON value.

    Returns:
        ``{"kind": "json", "member_paths": [], "key_paths": [...], "stats": {}}``.
    """
    return {
        "kind": "json",
        "member_paths": [],
        "key_paths": sorted(key_paths_of(payload)),
        "stats": {},
    }





def fingerprint_ndjson_lines(records: list) -> dict:
    """Fingerprint an NDJSON file from a sample of its parsed records.

    Args:
        records: Parsed NDJSON records (list of dicts).

    Returns:
        ``{"kind": "ndjson", ...}`` with key paths unioned over the first
        ``MAX_NDJSON_SAMPLE`` records.
    """
    paths: set[str] = set()
    for record in records[:MAX_NDJSON_SAMPLE]:
        paths.update(key_paths_of(record))
        if len(paths) >= MAX_KEY_PATHS:
            break
    return {
        "kind": "ndjson",
        "member_paths": [],
        "key_paths": sorted(paths),
        "stats": {},
    }





def fingerprint_html_watch_history(raw: bytes) -> dict:
    """Structural markers for a YouTube Takeout ``watch-history.html`` member.

    Emits pseudo key paths for the structural elements the parser depends on
    (outer cells, caption divs, ``watch?v=`` links, parseable timestamps) plus
    two ratio stats that feed the drift distributions.

    Args:
        raw: The member's bytes.

    Returns:
        ``{"marker_paths": [...], "stats": {"html_video_link_frac": ...,
        "html_ts_match_frac": ...}}``.
    """
    text = raw.decode("utf-8", errors="replace")
    blocks = text.split(_HTML_OUTER_CELL_MARKER)[1:]
    n_blocks = len(blocks)
    n_video = 0
    n_caption = 0
    n_ts = 0
    for block in blocks:
        if _HTML_VIDEO_RE.search(block):
            n_video += 1
        captions = _HTML_CAPTION_RE.findall(block)
        if captions:
            n_caption += 1
            if any(_HTML_TS_RE.search(c) for c in captions):
                n_ts += 1

    marker_paths = []
    if n_blocks:
        marker_paths.append(f"html.outer-cell{TYPE_SEP}marker")
    if n_caption:
        marker_paths.append(f"html.caption{TYPE_SEP}marker")
    if n_video:
        marker_paths.append(f"html.watch-link{TYPE_SEP}marker")
    if n_ts:
        marker_paths.append(f"html.timestamp{TYPE_SEP}marker")

    return {
        "marker_paths": marker_paths,
        "stats": {
            "html_video_link_frac": (n_video / n_blocks) if n_blocks else 0.0,
            "html_ts_match_frac": (n_ts / n_caption) if n_caption else 0.0,
        },
    }





def fingerprint_zip(local_path: str, member_suffixes: list[str]) -> dict:
    """Fingerprint a donation zip: which expected members exist and their shape.

    JSON members contribute typed key paths scoped as ``<suffix>::<path>``;
    HTML members contribute structural markers via
    :func:`fingerprint_html_watch_history` (scoped the same way) plus ratio
    stats; CSV members contribute their header columns as
    ``<suffix>::col:<name>`` paths. An unparseable JSON member is recorded as
    a distinct pseudo path so it both surfaces as drift and removes the
    member's core paths.

    Args:
        local_path: Local filesystem path to the zip (see ``data_io.local_copy``).
        member_suffixes: Member-name suffixes to look for, matched with the
            same semantics as :func:`fyp.utils.read_zip_members`.

    Returns:
        ``{"kind": "zip", "member_paths": [...], "key_paths": [...], "stats": {...}}``.
    """
    members = read_zip_members(local_path, member_suffixes)
    member_paths: list[str] = []
    key_paths: set[str] = set()
    stats: dict[str, float] = {}

    for suffix, raw in members.items():
        if raw is None:
            continue
        member_paths.append(suffix)
        if suffix.endswith(".html"):
            html_fp = fingerprint_html_watch_history(raw)
            key_paths.update(f"{suffix}{MEMBER_SEP}{p}" for p in html_fp["marker_paths"])
            stats.update(html_fp["stats"])
            continue
        if suffix.endswith(".csv"):
            header = raw.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
            key_paths.update(
                f"{suffix}{MEMBER_SEP}col:{col.strip()}" for col in header.split(",") if col.strip()
            )
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            key_paths.add(f"{suffix}{MEMBER_SEP}$unparseable{TYPE_SEP}error")
            continue
        key_paths.update(f"{suffix}{MEMBER_SEP}{p}" for p in key_paths_of(payload))

    return {
        "kind": "zip",
        "member_paths": sorted(member_paths),
        "key_paths": sorted(key_paths),
        "stats": stats,
    }





def compute_raw_stats(df: pd.DataFrame, size_bytes: int | None) -> dict:
    """Per-file sanity stats available right after ``load_single_raw``.

    Args:
        df: The raw per-file DataFrame.
        size_bytes: Stored file size in bytes (``data_io.getsize``), or None.

    Returns:
        Dict with ``raw_rows``, ``file_size_mb``, ``rows_per_mb`` and
        ``seed_fill.<col>`` fill fractions for any donated-seed columns.
    """
    stats: dict = {"raw_rows": int(len(df))}
    if size_bytes:
        size_mb = size_bytes / (1024 * 1024)
        stats["file_size_mb"] = round(size_mb, 3)
        stats["rows_per_mb"] = round(len(df) / size_mb, 2) if size_mb > 0 else 0.0
    if len(df) > 0:
        for col in df.columns:
            if col.startswith("seed_"):
                stats[f"seed_fill.{col}"] = round(float(df[col].notna().mean()), 4)
    return stats





def compute_processed_stats(raw_rows: int, df_file: pd.DataFrame) -> dict:
    """Per-file sanity stats on the processed (post ``process()``) rows.

    Args:
        raw_rows: The file's row count before processing.
        df_file: The file's processed rows.

    Returns:
        Dict with ``kept_rows``, ``kept_ratio`` (the timestamp parse rate,
        since processing drops rows with unparseable timestamps),
        ``null_item_id_frac`` and the ``activity_types`` count map.
    """
    kept = int(len(df_file))
    stats: dict = {
        "kept_rows": kept,
        "kept_ratio": round(kept / raw_rows, 4) if raw_rows > 0 else 0.0,
    }
    if kept > 0:
        if "item_id" in df_file.columns:
            stats["null_item_id_frac"] = round(float(df_file["item_id"].isna().mean()), 4)
        if "activity_type" in df_file.columns:
            counts = df_file["activity_type"].value_counts(dropna=True)
            stats["activity_types"] = {str(k): int(v) for k, v in counts.items()}
    return stats





def _new_stat_moments() -> dict:
    """Fresh running-moments record for one metric."""
    return {"count": 0, "mean": 0.0, "m2": 0.0, "min": None, "max": None}





def _update_moments(moments: dict, x: float) -> None:
    """Welford update of a metric's running moments and observed range."""
    moments["count"] += 1
    delta = x - moments["mean"]
    moments["mean"] += delta / moments["count"]
    moments["m2"] += delta * (x - moments["mean"])
    moments["min"] = x if moments["min"] is None else min(moments["min"], x)
    moments["max"] = x if moments["max"] is None else max(moments["max"], x)





def _std_of(moments: dict) -> float:
    """Sample standard deviation from running moments (0.0 when undefined)."""
    if moments["count"] < 2:
        return 0.0
    return math.sqrt(max(moments["m2"] / (moments["count"] - 1), 0.0))





def _is_fraction_metric(metric: str) -> bool:
    """Whether a metric is a [0, 1] fraction (gets an absolute tolerance)."""
    return metric.startswith(FRACTION_METRICS_PREFIXES)





def _empty_baseline() -> dict:
    """Fresh baseline record for one (platform, source)."""
    return {
        "n_accepted": 0,
        "learned_files": [],
        "member_paths": {},
        "key_paths": {},
        "path_types": {},
        "activity_types": {},
        "stats": {},
        "accepted_structures": [],
        "updated_at": None,
    }





def baseline_key(platform: str | None, source: str | None) -> str:
    """Baseline dictionary key for one (platform, data_source) pair."""
    return f"{platform}_{source}"





def load_baselines() -> dict:
    """Load ``structure_baselines.json`` (empty skeleton when absent)."""
    if data_io.exists(storage_location=STORAGE_LOCATION, filename=STRUCTURE_BASELINES_FILENAME):
        loaded = data_io.load_json(
            storage_location=STORAGE_LOCATION,
            filename=STRUCTURE_BASELINES_FILENAME,
            verbose=False,
        )
        if isinstance(loaded, dict) and "baselines" in loaded:
            return loaded
    return {"schema_version": 1, "baselines": {}}





def save_baselines(state: dict) -> None:
    """Persist the baselines state."""
    data_io.save_json(
        data=state,
        storage_location=STORAGE_LOCATION,
        filename=STRUCTURE_BASELINES_FILENAME,
        verbose=False,
    )





def load_verdicts() -> dict:
    """Load ``structure_verdicts.json`` (empty skeleton when absent)."""
    if data_io.exists(storage_location=STORAGE_LOCATION, filename=STRUCTURE_VERDICTS_FILENAME):
        loaded = data_io.load_json(
            storage_location=STORAGE_LOCATION,
            filename=STRUCTURE_VERDICTS_FILENAME,
            verbose=False,
        )
        if isinstance(loaded, dict) and "files" in loaded:
            return loaded
    return {"schema_version": 1, "files": {}}





def save_verdicts(state: dict) -> None:
    """Persist the verdicts state."""
    data_io.save_json(
        data=state,
        storage_location=STORAGE_LOCATION,
        filename=STRUCTURE_VERDICTS_FILENAME,
        verbose=False,
    )





def learn_file(
    baseline: dict,
    fingerprint: dict | None,
    raw_stats: dict | None,
    processed_stats: dict | None,
    filename: str,
    approved_by: str | None = None,
) -> None:
    """Fold one accepted file's fingerprint and stats into a baseline.

    Idempotent per filename (via the baseline's ``learned_files`` list) so an
    admin approval followed by the next refresh's automatic learning cannot
    double-count a file.

    Args:
        baseline: The mutable baseline record (see :func:`_empty_baseline`).
        fingerprint: The file's structure fingerprint, or None.
        raw_stats: Output of :func:`compute_raw_stats` (may include merged
            fingerprint stats), or None.
        processed_stats: Output of :func:`compute_processed_stats`, or None.
        filename: The raw file's name (idempotency key).
        approved_by: Username when learning via an explicit admin approval.
    """
    if filename in baseline["learned_files"]:
        return
    baseline["learned_files"].append(filename)
    baseline["n_accepted"] += 1

    new_paths: list[str] = []
    if fingerprint:
        for member in fingerprint.get("member_paths", []):
            baseline["member_paths"][member] = baseline["member_paths"].get(member, 0) + 1
        for typed_path in fingerprint.get("key_paths", []):
            if typed_path not in baseline["key_paths"]:
                new_paths.append(typed_path)
            baseline["key_paths"][typed_path] = baseline["key_paths"].get(typed_path, 0) + 1
            base = base_path_of(typed_path)
            leaf = typed_path.rsplit(TYPE_SEP, 1)[-1]
            types = baseline["path_types"].setdefault(base, [])
            if leaf not in types:
                types.append(leaf)

    metrics: dict[str, float] = {}
    for source_stats in (raw_stats, processed_stats):
        if not source_stats:
            continue
        for key, value in source_stats.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if key not in ("raw_rows", "kept_rows", "file_size_mb"):
                    metrics[key] = float(value)
    for metric, value in metrics.items():
        moments = baseline["stats"].setdefault(metric, _new_stat_moments())
        _update_moments(moments, value)

    if processed_stats and processed_stats.get("activity_types"):
        for activity_type in processed_stats["activity_types"]:
            baseline["activity_types"][activity_type] = (
                baseline["activity_types"].get(activity_type, 0) + 1
            )

    if new_paths and baseline["n_accepted"] > 1:
        baseline["accepted_structures"].append({
            "ts": _now_iso(),
            "filename": filename,
            "new_key_paths": new_paths[:100],
            "approved_by": approved_by,
        })
        baseline["accepted_structures"] = baseline["accepted_structures"][-MAX_ACCEPTED_STRUCTURES:]
    baseline["updated_at"] = _now_iso()





def evaluate_structure(fingerprint: dict | None, baseline: dict) -> list[dict]:
    """Layer-1 findings: compare a fingerprint against the learned structure.

    Args:
        fingerprint: The file's structure fingerprint, or None (no findings).
        baseline: The (platform, source) baseline.

    Returns:
        Finding dicts (``layer="structure"``), empty when nothing deviates.
    """
    findings: list[dict] = []
    n = baseline["n_accepted"]
    if not fingerprint or n < MIN_ACCEPTED_FOR_STRUCTURE_CHECKS:
        return findings

    additive_severity = "quarantine" if ADDITIVE_QUARANTINES else "warn"
    fp_members = set(fingerprint.get("member_paths", []))
    fp_typed = set(fingerprint.get("key_paths", []))
    fp_bases = {base_path_of(p) for p in fp_typed}

    core_members = {m for m, c in baseline["member_paths"].items() if c / n >= CORE_PATH_SUPPORT}
    missing_members = sorted(core_members - fp_members)
    if missing_members:
        findings.append({
            "layer": "structure",
            "severity": "quarantine",
            "code": "missing_member",
            "detail": f"{len(missing_members)} expected zip member(s) absent",
            "items": missing_members,
        })
    new_members = sorted(fp_members - set(baseline["member_paths"]))
    if new_members:
        findings.append({
            "layer": "structure",
            "severity": additive_severity,
            "code": "new_member",
            "detail": f"{len(new_members)} previously unseen zip member(s)",
            "items": new_members,
        })

    core_typed = {p for p, c in baseline["key_paths"].items() if c / n >= CORE_PATH_SUPPORT}
    missing_core: list[str] = []
    type_changed: list[str] = []
    for typed_path in sorted(core_typed - fp_typed):
        base = base_path_of(typed_path)
        # A missing member already accounts for every path scoped under it.
        if any(base.startswith(f"{m}{MEMBER_SEP}") for m in missing_members):
            continue
        if base in fp_bases:
            type_changed.append(typed_path)
        else:
            missing_core.append(typed_path)
    if type_changed:
        findings.append({
            "layer": "structure",
            "severity": "quarantine",
            "code": "type_changed",
            "detail": f"{len(type_changed)} known key path(s) reappear with a different type",
            "items": type_changed,
        })
    if missing_core:
        findings.append({
            "layer": "structure",
            "severity": "quarantine",
            "code": "missing_core_paths",
            "detail": f"{len(missing_core)} core key path(s) absent",
            "items": missing_core,
        })

    known_bases = {base_path_of(p) for p in baseline["key_paths"]}
    new_paths = sorted(p for p in fp_typed if base_path_of(p) not in known_bases)
    if new_paths:
        findings.append({
            "layer": "structure",
            "severity": additive_severity,
            "code": "new_key_paths",
            "detail": f"{len(new_paths)} previously unseen key path(s)",
            "items": new_paths[:100],
        })

    return findings





def evaluate_stats(metrics: dict, activity_types: dict | None, baseline: dict) -> list[dict]:
    """Layer-2/3 findings: compare per-file stats against learned distributions.

    Args:
        metrics: ``{metric: value}`` for this file (raw + processed +
            fingerprint stats).
        activity_types: The file's processed activity-type count map, or None.
        baseline: The (platform, source) baseline.

    Returns:
        Finding dicts (``layer="stats"``), empty when nothing deviates.
    """
    findings: list[dict] = []
    n = baseline["n_accepted"]

    if activity_types is not None and n >= MIN_ACCEPTED_FOR_STRUCTURE_CHECKS and baseline["activity_types"]:
        dominant = max(baseline["activity_types"], key=baseline["activity_types"].get)
        if baseline["activity_types"][dominant] / n >= CORE_PATH_SUPPORT and dominant not in activity_types:
            findings.append({
                "layer": "stats",
                "severity": "quarantine",
                "code": "dominant_type_missing",
                "detail": f"dominant activity type '{dominant}' absent from this file",
                "items": [dominant],
            })
        new_types = sorted(set(activity_types) - set(baseline["activity_types"]))
        if new_types:
            findings.append({
                "layer": "stats",
                "severity": "quarantine" if ADDITIVE_QUARANTINES else "warn",
                "code": "new_activity_type",
                "detail": f"{len(new_types)} previously unseen activity type(s)",
                "items": new_types,
            })

    if n < MIN_ACCEPTED_FOR_STAT_CHECKS:
        return findings

    for metric, value in metrics.items():
        moments = baseline["stats"].get(metric)
        if not moments or moments["count"] < MIN_ACCEPTED_FOR_STAT_CHECKS:
            continue
        x = float(value)
        std = _std_of(moments)
        z = (x - moments["mean"]) / std if std > 0 else 0.0
        mn, mx = moments["min"], moments["max"]
        spread = mx - mn
        pad = max(0.25 * spread, 0.10 * max(abs(mn), abs(mx)))
        if _is_fraction_metric(metric):
            pad = max(pad, FRACTION_ABS_TOLERANCE)
        hard_range_breach = x < mn - pad or x > mx + pad
        soft_range_breach = x < mn or x > mx

        finding = {
            "layer": "stats",
            "metric": metric,
            "value": round(x, 4),
            "baseline_mean": round(moments["mean"], 4),
            "baseline_min": round(mn, 4),
            "baseline_max": round(mx, 4),
            "z": round(z, 2),
        }
        if metric in HARD_STAT_METRICS and (abs(z) > STAT_Z_QUARANTINE or hard_range_breach):
            finding.update({
                "severity": "quarantine",
                "code": "stat_outlier_hard",
                "detail": f"'{metric}' = {x:.4g} vs accepted {mn:.4g}..{mx:.4g} (z = {z:.1f})",
            })
            findings.append(finding)
        elif abs(z) > STAT_Z_WARN or soft_range_breach:
            finding.update({
                "severity": "warn",
                "code": "stat_outlier_soft",
                "detail": f"'{metric}' = {x:.4g} vs accepted {mn:.4g}..{mx:.4g} (z = {z:.1f})",
            })
            findings.append(finding)

    return findings





def status_from_findings(findings: list[dict], n_accepted: int) -> str:
    """Derive a verdict status from findings and the baseline's maturity."""
    if n_accepted < MIN_ACCEPTED_FOR_STRUCTURE_CHECKS:
        return "learning"
    if any(f["severity"] == "quarantine" for f in findings):
        return "quarantined"
    if findings:
        return "warn"
    return "ok"





def findings_digest(findings: list[dict]) -> str:
    """One-line human-readable digest of a findings list (for ledger notes)."""
    if not findings:
        return ""
    return "; ".join(f.get("detail", f.get("code", "?")) for f in findings[:6])





class StructureSentinel:
    """Per-ingest-run drift detector.

    Created once per run (by ``run_ingest_refresh`` or the bootstrap script)
    and injected into every sub-collection as ``sub.sentinel``. Phase A
    (:meth:`check_raw`) runs inside ``load_raw`` per file; Phase B
    (:meth:`check_processed`) runs on the processed frames before migration;
    :meth:`commit` learns the ingested files and persists verdicts.

    Attributes:
        baselines: Loaded ``structure_baselines.json`` state.
        observations: Per-filename records accumulated this run.
    """





    def __init__(self):
        self.baselines = load_baselines()
        self.observations: dict[str, dict] = {}





    def _baseline_for(self, collection) -> dict:
        key = baseline_key(collection.source_platform, collection.data_source)
        return self.baselines["baselines"].setdefault(key, _empty_baseline())





    def check_raw(self, collection, filename: str, df: pd.DataFrame) -> dict:
        """Phase A: fingerprint a freshly loaded raw file and run structure checks.

        Args:
            collection: The sub-collection that loaded the file.
            filename: The raw file's name.
            df: The per-file raw DataFrame from ``load_single_raw``.

        Returns:
            The verdict dict (status ``ok``/``warn``/``learning``/``quarantined``).
        """
        baseline = self._baseline_for(collection)
        fingerprint = None
        try:
            fingerprint = collection.fingerprint_raw(filename)
        except Exception as exc:
            print(f"WARNING: fingerprinting failed for '{filename}': {exc}. Structure layer skipped.")

        try:
            size_bytes = data_io.getsize(storage_location=collection.raw_path, filename=filename)
        except Exception:
            size_bytes = None
        raw_stats = compute_raw_stats(df, size_bytes)
        if fingerprint and fingerprint.get("stats"):
            raw_stats.update(fingerprint["stats"])

        findings = evaluate_structure(fingerprint, baseline)
        status = status_from_findings(findings, baseline["n_accepted"])
        verdict = {
            "status": status,
            "platform": collection.source_platform,
            "source": collection.data_source,
            "ts_evaluated": _now_iso(),
            "findings": findings,
            "raw_stats": raw_stats,
            "processed_stats": None,
            "fingerprint": fingerprint,
            "reviewed_by": None,
            "reviewed_at": None,
            "review_action": None,
        }
        self.observations[filename] = verdict
        return verdict





    def check_processed(self, collection, filename: str, df_file: pd.DataFrame) -> dict:
        """Phase B: run parse-output sanity + drift checks on a file's processed rows.

        Args:
            collection: The sub-collection that processed the file.
            filename: The raw file's name.
            df_file: The file's processed rows (one ``raw_file`` group).

        Returns:
            The (possibly upgraded) verdict dict.
        """
        baseline = self._baseline_for(collection)
        verdict = self.observations.get(filename)
        if verdict is None:
            verdict = self.check_raw(collection, filename, df_file)

        raw_rows = int(verdict["raw_stats"].get("raw_rows") or len(df_file))
        processed_stats = compute_processed_stats(raw_rows, df_file)
        verdict["processed_stats"] = processed_stats

        metrics = {
            k: v for stats in (verdict["raw_stats"], processed_stats)
            for k, v in stats.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and k not in ("raw_rows", "kept_rows", "file_size_mb")
        }
        verdict["findings"] = verdict["findings"] + evaluate_stats(
            metrics, processed_stats.get("activity_types"), baseline
        )
        verdict["status"] = status_from_findings(verdict["findings"], baseline["n_accepted"])
        verdict["ts_evaluated"] = _now_iso()
        return verdict





    def commit(self, ingested_filenames: set[str]) -> None:
        """Learn ingested files into the baselines and persist all verdicts.

        Only files that actually landed in the activity data (and were not
        quarantined) are learned. Verdicts are merged into the stored file:
        entries for files not observed this run — including quarantined files
        an admin approved or rejected while the task ran — are left untouched.

        Args:
            ingested_filenames: raw_file names whose rows entered the main
                collection this run.
        """
        for filename, verdict in self.observations.items():
            if verdict["status"] in ("ok", "warn", "learning") and filename in ingested_filenames:
                key = baseline_key(verdict["platform"], verdict["source"])
                baseline = self.baselines["baselines"].setdefault(key, _empty_baseline())
                learn_file(
                    baseline,
                    verdict.get("fingerprint"),
                    verdict.get("raw_stats"),
                    verdict.get("processed_stats"),
                    filename,
                )
        save_baselines(self.baselines)

        stored = load_verdicts()
        for filename, verdict in self.observations.items():
            entry = dict(verdict)
            entry.pop("fingerprint", None)
            if verdict["status"] == "quarantined":
                # Keep the fingerprint on quarantined entries so an approval
                # can learn the structure without re-parsing the file.
                entry["fingerprint"] = verdict.get("fingerprint")
            stored["files"][filename] = entry
        save_verdicts(stored)





def approve_file(filename: str, reviewed_by: str | None) -> dict:
    """Approve a quarantined (or warned) file: learn its structure, clear review state.

    Folds the verdict's stored fingerprint and stats into the baseline so the
    structure counts as accepted, then marks the verdict ``approved``. The
    caller is responsible for removing the file's ledger entry so the next
    ingest run reloads it.

    Args:
        filename: The raw file's name.
        reviewed_by: The reviewing user's name.

    Returns:
        The updated verdict entry.

    Raises:
        KeyError: When no verdict exists for ``filename``.
        ValueError: When the verdict is not in a reviewable status.
    """
    verdicts = load_verdicts()
    entry = verdicts["files"].get(filename)
    if entry is None:
        raise KeyError(f"no structure verdict for '{filename}'")
    if entry.get("status") not in ("quarantined", "warn"):
        raise ValueError(f"'{filename}' has status '{entry.get('status')}', nothing to approve")

    baselines = load_baselines()
    key = baseline_key(entry.get("platform"), entry.get("source"))
    baseline = baselines["baselines"].setdefault(key, _empty_baseline())
    learn_file(
        baseline,
        entry.get("fingerprint"),
        entry.get("raw_stats"),
        entry.get("processed_stats"),
        filename,
        approved_by=reviewed_by,
    )
    save_baselines(baselines)

    entry["status"] = "approved"
    entry["reviewed_by"] = reviewed_by
    entry["reviewed_at"] = _now_iso()
    entry["review_action"] = "approve"
    save_verdicts(verdicts)
    return entry





def reject_file(filename: str, reviewed_by: str | None) -> dict:
    """Reject a quarantined file: mark the verdict; caller excludes it in the ledger.

    Args:
        filename: The raw file's name.
        reviewed_by: The reviewing user's name.

    Returns:
        The updated verdict entry.

    Raises:
        KeyError: When no verdict exists for ``filename``.
    """
    verdicts = load_verdicts()
    entry = verdicts["files"].get(filename)
    if entry is None:
        raise KeyError(f"no structure verdict for '{filename}'")
    entry["status"] = "rejected"
    entry["reviewed_by"] = reviewed_by
    entry["reviewed_at"] = _now_iso()
    entry["review_action"] = "reject"
    save_verdicts(verdicts)
    return entry





def review_queue() -> dict:
    """Verdicts the UI should surface, most recent first.

    Returns:
        ``{"files": [verdict entries + filename], "n_quarantined": int,
        "n_warn": int}``; fingerprints are stripped (the findings carry the
        path lists the review modal needs).
    """
    verdicts = load_verdicts()
    rows = []
    for filename, entry in verdicts["files"].items():
        if entry.get("status") not in ("quarantined", "warn"):
            continue
        row = dict(entry)
        row.pop("fingerprint", None)
        row["filename"] = filename
        rows.append(row)
    rows.sort(key=lambda r: r.get("ts_evaluated") or "", reverse=True)
    return {
        "files": rows,
        "n_quarantined": sum(1 for r in rows if r["status"] == "quarantined"),
        "n_warn": sum(1 for r in rows if r["status"] == "warn"),
    }
