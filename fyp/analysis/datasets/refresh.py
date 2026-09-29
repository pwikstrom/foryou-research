"""Incremental study refresh: input fingerprints, the per-study sidecar, and the refresh plan.

A study's recoded dataset is rebuilt only when its inputs changed. The
sidecar records the fingerprints of every input file (collections, scrapes,
annotations, the niche map) and the study's config hash; ``plan_refresh``
compares them with the current ones and decides between reusing the cached
dataset, patching only the enrichment columns, or a full rebuild.
"""

import datetime as _dt
import hashlib
import json

import pandas as pd

import fyp.core.data_io as data_io
from fyp.analysis.datasets import common
from fyp.analysis.studies import init_study_defs
from fyp.annotation.recode_variables import (
    compute_var_schema_hash,
)
from fyp.core.artifacts import ENRICHMENT_STATUS_FILE
from fyp.core.logging_setup import get_logger

# Shared memory-probe implementations (fyp.core.memory); the module-private
# aliases keep this file's many existing call sites and the
# [RECODE][MEM]/[ENRICH PATCH][MEM] log lines unchanged.
from fyp.core.runtime import cf as _cf
from fyp.core.utils import VIDEO_VIEW_TYPES
from fyp.scrape.failures import load_failed_scrapes

logger = get_logger(__name__)

# ============================================================================
# Refresh fingerprinting — sidecar metadata for incremental refresh
# ============================================================================
#
# Each `{study}_recoded.parquet` gets a sidecar `{study}_recoded.meta.json`
# that records fingerprints of every input whose change could invalidate the
# cached output. On refresh, the entry point compares current fingerprints to
# the sidecar to decide between full rebuild, incremental patch, and short-
# circuit (skip entirely). Missing/malformed sidecar -> full rebuild.


def _fingerprint_input_files() -> dict:
    """Return the fingerprint-input map (label-derived, so config-lazy)."""
    return {
        "collections_fp": ("recoded", f"{common._collections_label()}_recoded.parquet"),
        "scrapes_fp": ("recoded", f"{common._scrapes_label()}_recoded.parquet"),
        "annotations_fp": ("recoded", f"{common._machine_annotations_label()}_recoded.parquet"),
        "video_map_fp": (common._VIDEO_MAP_LOCATION, common._VIDEO_MAP_FILE),
    }


def _sidecar_filename(study_name: str) -> str:
    """Return the sidecar filename for a given study's recoded dataset."""
    return f"{study_name}_recoded.meta.json"


def compute_study_config_hash(study_name: str) -> str:
    """Return a deterministic SHA-256 hash of a study's configuration.

    Covers every study-definition field that can change the set of rows or the
    recoded column values: selected collections, date range, sampling mode and
    thresholds. Ordered JSON serialisation keeps the digest stable across
    Python-dict insertion order.
    """

    if "study_defs" not in _cf():
        init_study_defs()
    cfg = _cf()["study_defs"].get(study_name, {}) or {}
    # Explicit key list: adding a new key should require a deliberate bump here,
    # and we don't want transient UI-only fields (stats, last_updated) to affect the hash.
    relevant_keys = [
        "SELECTED_COLLECTIONS",
        "START_DATE",
        "END_DATE",
        "SAMPLE_FRAME",
        "MIN_ACTIVITY_COUNT_PER_GROUP",
        "MAX_ACTIVITY_COUNT_PER_GROUP",
        "MIN_GROUP_COUNT_PER_COLLECTION",
        "MAX_GROUP_COUNT_PER_COLLECTION",
        "GROUPING_FACTORS",
    ]
    ordered = {k: cfg.get(k) for k in relevant_keys}
    # SELECTED_COLLECTIONS order shouldn't matter
    if isinstance(ordered.get("SELECTED_COLLECTIONS"), list):
        ordered["SELECTED_COLLECTIONS"] = sorted(str(x) for x in ordered["SELECTED_COLLECTIONS"])
    payload = json.dumps(ordered, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _video_map_fingerprint():
    """The niche map's fingerprint: its ASSIGNMENT, not its file stat.

    ``build_niche_map`` rewrites video_map.parquet on every run — fresh t-SNE
    coordinates and a new ``built_at`` — so a file stat reports "changed" after a
    rebuild that moved no video between niches, and every study would rebuild
    for a join whose result is byte-identical. The map's meta file carries a
    hash over its ``(item_id, niche)`` pairs; that is the thing a study cache
    actually depends on. Falls back to the stat for a map written before the
    hash existed, or when the meta file cannot be read — a spurious rebuild is
    the safe failure here, a skipped one is not.
    """
    fallback = data_io.stat(
        storage_location=common._VIDEO_MAP_LOCATION, filename=common._VIDEO_MAP_FILE
    )
    if fallback is None:
        return None
    try:
        if not data_io.exists(
            storage_location=common._VIDEO_MAP_LOCATION, filename=common._VIDEO_MAP_META_FILE
        ):
            return fallback
        meta = (
            data_io.load_json(
                storage_location=common._VIDEO_MAP_LOCATION, filename=common._VIDEO_MAP_META_FILE
            )
            or {}
        )
        digest = meta.get("niche_assignment_hash")
        if digest:
            return {"niche_assignment_hash": str(digest)}
    except Exception:
        pass
    return fallback


def compute_input_fingerprints() -> dict:
    """Stat each core input parquet and return a dict of fingerprint dicts.

    A missing file maps to None in the returned dict so callers can distinguish
    "file not present" from "file unchanged". The niche map is fingerprinted by
    content rather than by stat — see :func:`_video_map_fingerprint`.
    """

    fps = {
        key: data_io.stat(storage_location=loc, filename=fn)
        for key, (loc, fn) in _fingerprint_input_files().items()
    }
    fps["video_map_fp"] = _video_map_fingerprint()
    return fps


# Marker file recording the input fingerprints that produced the current
# enrichment_status.parquet. Written right after the status save; compared by
# _status_inputs_unchanged() so a consolidation with nothing new can skip the
# full status rebuild (measured at 75-310 s). The marker deliberately trails
# the status file: a crash between the two leaves it stale, which only costs
# one extra rebuild — never a skipped one.
_STATUS_INPUTS_MARKER = "enrichment_status_inputs.json"
# video_map_fp is part of compute_input_fingerprints() but irrelevant to the
# status file (no niche columns in it), so it is excluded from the marker.
_STATUS_FP_KEYS = ("collections_fp", "scrapes_fp", "annotations_fp")


def _write_status_inputs_marker(verbose: bool = False) -> None:
    """Persist the current status-input fingerprints. Never raises."""
    try:
        fps = compute_input_fingerprints()
        payload = {key: fps.get(key) for key in _STATUS_FP_KEYS}
        payload["failed_scrapes_fp"] = compute_failed_scrapes_fingerprint()
        data_io.save_json(
            data=payload,
            storage_location="recoded",
            filename=_STATUS_INPUTS_MARKER,
            verbose=verbose,
        )
    except Exception as exc:
        logger.warning(f"    Could not write the status-inputs marker: {exc}")


def _status_inputs_unchanged(verbose: bool = False) -> bool:
    """True when enrichment_status.parquet is already up to date with its inputs.

    Compares the persisted marker against the current input fingerprints
    (collections/scrapes/annotations recoded stat + failed-scrapes hash). Any
    read problem or mismatch returns False — the full rebuild is always the
    safe answer.
    """
    try:
        if not data_io.exists(storage_location="recoded", filename=ENRICHMENT_STATUS_FILE):
            return False
        if not data_io.exists(storage_location="recoded", filename=_STATUS_INPUTS_MARKER):
            return False
        marker = data_io.load_json(
            storage_location="recoded", filename=_STATUS_INPUTS_MARKER, verbose=verbose
        )
        if not isinstance(marker, dict):
            return False
        fps = compute_input_fingerprints()
        for key in _STATUS_FP_KEYS:
            if not _fp_equal(marker.get(key), fps.get(key)):
                return False
        return marker.get("failed_scrapes_fp") == compute_failed_scrapes_fingerprint()
    except Exception as exc:
        logger.warning(f"    Status-inputs marker check failed (forcing rebuild): {exc}")
        return False


def compute_failed_scrapes_fingerprint() -> dict:
    """Return a lightweight fingerprint of the failed-scrapes JSON set.

    `load_failed_scrapes` consolidates multiple JSON files into a single set of
    item_ids; fingerprint is (count, hash-of-sorted-ids). Cheap enough that we
    can compute it at refresh-planning time without paying the file read twice.
    """

    try:
        items = sorted(str(x) for x in load_failed_scrapes(verbose=False))
    except Exception as exc:
        logger.warning(f"    [FP] Could not load failed_scrapes for fingerprint: {exc}")
        return {"count": 0, "hash": ""}
    digest = hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()
    return {"count": len(items), "hash": digest}


def _hash_item_ids(df: pd.DataFrame) -> str:
    """Return a stable hash of the unique item_ids in a recoded dataset."""
    if df is None or df.empty or "item_id" not in df.columns:
        return "empty"
    ids = sorted(set(df["item_id"].dropna().astype(str).tolist()))
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def _extract_selected_cells(recoded_df: pd.DataFrame) -> dict[str, list[str]]:
    """Return {collection_id: [local_date, ...]} for play events in the recoded df.

    The (collection_id, local_date) cells the recoded parquet contains are
    exactly the cells the study admitted post-sampling; the timeline endpoint
    uses this map to filter per-collection day series down to the study view.
    """
    if recoded_df is None or recoded_df.empty:
        return {}
    cols = {"collection_id", "local_date"}
    if not cols.issubset(recoded_df.columns):
        return {}

    df = recoded_df
    if common.event_type_column in df.columns:
        df = df[df[common.event_type_column].isin(VIDEO_VIEW_TYPES)]
    if df.empty:
        return {}

    pairs = df[["collection_id", "local_date"]].dropna().drop_duplicates()
    if pairs.empty:
        return {}

    pairs = pairs.assign(
        collection_id=pairs["collection_id"].astype(str),
        local_date=pd.to_datetime(pairs["local_date"]).dt.strftime("%Y-%m-%d"),
    )
    return {
        cid: sorted(group["local_date"].tolist())
        for cid, group in pairs.groupby("collection_id", sort=False)
    }


def build_sidecar(study_name: str, recoded_df: pd.DataFrame) -> dict:
    """Assemble the sidecar payload for a freshly (re)built recoded dataset."""

    cfg = _cf().get("study_defs", {}).get(study_name, {}) or {}
    sampling_active = str(cfg.get("SAMPLE_FRAME", "off")) != "off"

    fps = compute_input_fingerprints()

    sidecar = {
        "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "sidecar_version": 3,
        "study_name": study_name,
        "study_config_hash": compute_study_config_hash(study_name),
        "var_schema_hash": compute_var_schema_hash(),
        "sampling_active": sampling_active,
        "collections_fp": fps.get("collections_fp"),
        "scrapes_fp": fps.get("scrapes_fp"),
        "annotations_fp": fps.get("annotations_fp"),
        "video_map_fp": fps.get("video_map_fp"),
        "failed_scrapes_fp": compute_failed_scrapes_fingerprint(),
        "item_ids_hash": _hash_item_ids(recoded_df),
        "row_count": len(recoded_df) if recoded_df is not None else 0,
    }

    if sampling_active:
        sidecar["selected_cells"] = _extract_selected_cells(recoded_df)

    return sidecar


def save_sidecar(study_name: str, recoded_df: pd.DataFrame, verbose: bool = False) -> dict:
    """Build and persist the sidecar for a study; return the payload written."""

    sidecar = build_sidecar(study_name, recoded_df)
    data_io.save_json(
        data=sidecar,
        storage_location="cache",
        filename=_sidecar_filename(study_name),
        verbose=verbose,
    )
    if verbose:
        logger.info(
            f"    [Sidecar] Wrote {_sidecar_filename(study_name)} (rows={sidecar['row_count']})"
        )
    return sidecar


def load_sidecar(study_name: str, verbose: bool = False) -> dict | None:
    """Load the sidecar for a study, or None if missing/malformed."""

    filename = _sidecar_filename(study_name)
    if not data_io.exists(storage_location="cache", filename=filename):
        return None
    try:
        payload = data_io.load_json(storage_location="cache", filename=filename, verbose=verbose)
        if not isinstance(payload, dict):
            return None
        return payload
    except Exception as exc:
        logger.warning(f"    [Sidecar] Could not load '{filename}': {exc}")
        return None


def _fp_equal(a: dict | None, b: dict | None) -> bool:
    """Return True when two stat/fingerprint dicts compare as equal (both None is equal)."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a == b


def plan_refresh(study_name: str, verbose: bool = False) -> dict:
    """Decide the cheapest correct refresh path for a study.

    Compares current input fingerprints against the sidecar and returns a plan:

    - `action`: "short_circuit" | "enrichment_patch" | "full_rebuild"
    - `reasons`: list[str] — human-readable explanation for logging
    - `changed`: dict[str, bool] — which fingerprint categories drifted
    - `old_sidecar`, `current_fps`: raw inputs so callers can reuse them

    Any change on the activity side (collections, study config, var schema)
    yields "full_rebuild"; there is no incremental video-set path. Callers
    should treat unknown actions as "full_rebuild" for safety.
    """

    reasons: list[str] = []
    changed = {
        "cache_missing": False,
        "sidecar_missing": False,
        "var_schema": False,
        "study_config": False,
        "collections": False,
        "scrapes": False,
        "annotations": False,
        "failed_scrapes": False,
        "video_map": False,
    }

    cache_filename = f"{study_name}_recoded.parquet"
    cache_exists = data_io.exists(storage_location="cache", filename=cache_filename)
    sidecar = load_sidecar(study_name, verbose=verbose)

    # Compute current fingerprints once — reused by the caller when a patch path runs.
    current_fps = compute_input_fingerprints()
    current_failed_fp = compute_failed_scrapes_fingerprint()
    current_var_hash = compute_var_schema_hash()
    current_cfg_hash = compute_study_config_hash(study_name)
    cfg = _cf().get("study_defs", {}).get(study_name, {}) or {}
    sampling_active = str(cfg.get("SAMPLE_FRAME", "off")) != "off"

    bundle = {
        "current_fps": current_fps,
        "current_failed_fp": current_failed_fp,
        "current_var_hash": current_var_hash,
        "current_cfg_hash": current_cfg_hash,
        "old_sidecar": sidecar,
        "sampling_active": sampling_active,
    }

    if not cache_exists:
        reasons.append("cache parquet missing")
        changed["cache_missing"] = True
        return {"action": "full_rebuild", "reasons": reasons, "changed": changed, **bundle}

    if sidecar is None:
        reasons.append("sidecar missing")
        changed["sidecar_missing"] = True
        return {"action": "full_rebuild", "reasons": reasons, "changed": changed, **bundle}

    if sidecar.get("var_schema_hash") != current_var_hash:
        reasons.append("var_schema changed")
        changed["var_schema"] = True

    if sidecar.get("study_config_hash") != current_cfg_hash:
        reasons.append("study_config changed")
        changed["study_config"] = True

    if not _fp_equal(sidecar.get("collections_fp"), current_fps.get("collections_fp")):
        reasons.append("collections parquet changed")
        changed["collections"] = True

    if not _fp_equal(sidecar.get("scrapes_fp"), current_fps.get("scrapes_fp")):
        reasons.append("scrapes parquet changed")
        changed["scrapes"] = True

    if not _fp_equal(sidecar.get("annotations_fp"), current_fps.get("annotations_fp")):
        reasons.append("annotations parquet changed")
        changed["annotations"] = True

    if not _fp_equal(sidecar.get("video_map_fp"), current_fps.get("video_map_fp")):
        reasons.append("video_map parquet changed")
        changed["video_map"] = True

    if not _fp_equal(sidecar.get("failed_scrapes_fp"), current_failed_fp):
        reasons.append("failed_scrapes list changed")
        changed["failed_scrapes"] = True

    if not any(changed.values()):
        reasons.append("all fingerprints match")
        return {"action": "short_circuit", "reasons": reasons, "changed": changed, **bundle}

    # Enrichment-only patch: scrapes / annotations / failed_scrapes changed, but
    # the activity side (collections parquet, study config, var schema) is
    # unchanged. Safe only when SAMPLE_FRAME does not depend on enrichment
    # state — "scraped" / "annotated" modes pick the sample frame from
    # enrichment_status, so any enrichment change can shift which activity rows
    # are kept, which breaks the assumption that we can reuse the cached rows.
    sample_frame = str(cfg.get("SAMPLE_FRAME", "off"))
    enrichment_driven_sampling = sample_frame in ("scraped", "annotated")

    enrichment_bits_changed = (
        changed["scrapes"] or changed["annotations"] or changed["failed_scrapes"]
    )
    activity_bits_changed = (
        changed["var_schema"] or changed["study_config"] or changed["collections"]
    )

    # The enrichment-only patch re-merges scrapes/annotations AND re-joins the
    # niche columns onto the cached activity rows, so it also refreshes a
    # video-map rebuild. A niche remap never shifts which activity rows are
    # sampled, so it stays patch-eligible even under enrichment-driven sampling;
    # only scrape/annotation changes can invalidate that sampling.
    patch_eligible = enrichment_bits_changed or changed["video_map"]

    if patch_eligible and not activity_bits_changed:
        if enrichment_bits_changed and enrichment_driven_sampling:
            reasons.append(
                f"sampling='{sample_frame}' depends on enrichment — forcing full rebuild"
            )
        else:
            return {"action": "enrichment_patch", "reasons": reasons, "changed": changed, **bundle}

    # Anything else (an activity-side change) needs a full rebuild.
    return {"action": "full_rebuild", "reasons": reasons, "changed": changed, **bundle}
