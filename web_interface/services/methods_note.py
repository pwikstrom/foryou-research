"""Per-study methods/provenance note — build, persist, read.

Every study refresh writes a ``{study}_methods.json`` sidecar (location
``cache``, sibling to ``{study}_recoded.meta.json``) summarising how the
study's dataset was built: selection filters, sample sizes, the annotation /
scrape / activity contract versions present in the rows, the embedding model
behind any joined niche columns, and refresh dates.

The note is written unconditionally on every refresh — including
short-circuited ones — because the *preferred* annotation version can move in
the registry without any rebuild; registry reads are cheap, so rebuilding the
note is the only way it tracks reality. Both refresh workers
(``run_study_refresh`` and ``run_recode_refresh_studies``) call
:func:`write_methods_note`, mirroring the ``compute_study_dataset_stats``
pattern that keeps the two writers from diverging.

Vocabulary (see DEVELOPING.md): the note reports the **preferred** annotation
version (what studies read), never conflating it with the **active** one
(what the next annotation is stamped with).

The schema is export-ready: machine fields carry plain-language ``*_label``
siblings so the M1 export can render a README by templating, not translating.
"""

from datetime import UTC, datetime

import pandas as pd

import fyp.data_io as data_io
from fyp import __version__ as _fyp_version
from fyp import annotation_versioning
from fyp.logging_setup import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 2
NOTE_LOCATION = "cache"

# Plain-language labels for the SAMPLE_FRAME study setting.
_SAMPLE_FRAME_LABELS = {
    "off": "All matching activity (no sampling)",
    "events": "Sampled from all activity events",
    "activities": "Sampled from all activity events",
    "scraped": "Sampled from videos with scraped metadata",
    "annotated": "Sampled from videos with AI labels",
}


def note_filename(study_name: str) -> str:
    """Return the methods-note filename for a study."""
    return f"{study_name}_methods.json"






def _version_distribution(df: pd.DataFrame | None, column: str) -> dict:
    """Count rows per version id in ``df[column]``.

    Returns ``{version_id: row_count, ...}`` for non-null values plus an
    ``"unversioned"`` bucket for null rows (pre-versioning history). Missing
    column or ``None`` frame → empty dict.
    """
    if df is None or column not in df.columns:
        return {}
    series = df[column]
    counts = {str(k): int(v) for k, v in series.value_counts(dropna=True).items()}
    n_null = int(series.isna().sum())
    if n_null and counts:
        counts["unversioned"] = n_null
    return counts






def _date_window(study_config: dict, df_study: pd.DataFrame | None) -> dict:
    """Configured vs actual date window of the study rows.

    The stored END_DATE is inclusive through the end of that day (the loader
    shifts the bound to the next day's midnight, exclusive) — stated here so a
    reader is never surprised by same-day events after 00:00.
    """
    window: dict = {
        "configured_start": study_config.get("START_DATE"),
        "configured_end": study_config.get("END_DATE"),
        "configured_end_note": "The end date is included in full (through 23:59:59).",
        "actual_min": None,
        "actual_max": None,
    }
    ts_col = "local_timestamp" if (df_study is not None and "local_timestamp" in df_study.columns) else "utc_timestamp"
    if df_study is not None and ts_col in df_study.columns and len(df_study) > 0:
        ts = pd.to_datetime(df_study[ts_col], errors="coerce").dropna()
        if len(ts) > 0:
            window["actual_min"] = ts.min().isoformat()
            window["actual_max"] = ts.max().isoformat()
    return window






def _annotation_block(study_config: dict, df_study: pd.DataFrame | None) -> dict:
    """Annotation provenance: pin, preferred version + descriptor, row mix.

    Reports the *preferred* version (what studies read). A study-level pin
    overrides preference for this study and is reported separately.
    """
    pinned = study_config.get("annotation_version") or None
    block: dict = {
        "pinned_version": pinned,
        "preferred_version": None,
        "version_in_use": None,
        "version_in_use_note": None,
        "version_in_use_descriptor": None,
        "versions_in_rows": _version_distribution(df_study, "annotation_version"),
    }
    try:
        registry = annotation_versioning.load_registry()
        preferred = registry.get("preferred")
        block["preferred_version"] = preferred
        block["version_in_use"] = pinned or preferred
        if pinned:
            block["version_in_use_note"] = (
                "This study is pinned to a specific labelling version; it keeps "
                "reading these labels even when a newer version is promoted."
            )
        elif preferred is None:
            block["version_in_use_note"] = (
                "No version is promoted as preferred; the study reads the latest "
                "available label for each video."
            )
        record = (registry.get("versions") or {}).get(block["version_in_use"] or "", {})
        if record:
            block["version_in_use_descriptor"] = {
                "label": record.get("label"),
                "model": record.get("model"),
                "backend": record.get("backend") or "gemini",
                "variant": record.get("variant"),
                "prompt_hash": record.get("prompt_hash"),
                "schema_hash": record.get("schema_hash"),
                "created_at": record.get("created_at"),
            }
    except Exception as exc:
        logger.warning(f"[MethodsNote] Could not read annotation registry: {exc}")

    versions = {k: v for k, v in block["versions_in_rows"].items() if k != "unversioned"}
    block["mixed_versions"] = len(versions) > 1 or "unversioned" in block["versions_in_rows"]
    if block["mixed_versions"]:
        block["mixed_versions_note"] = (
            "Rows in this study carry labels from more than one labelling "
            "configuration (or from before versioning); newer label fields may "
            "be empty on older rows."
        )
    return block






def _semantic_map_block(df_study: pd.DataFrame | None) -> dict | None:
    """Embedding-map provenance when niche columns are joined into the study."""
    if df_study is None or "niche" not in df_study.columns:
        return None
    try:
        if not data_io.exists(storage_location="recoded", filename="video_map_meta.json"):
            return None
        meta = data_io.load_json(storage_location="recoded", filename="video_map_meta.json")
        if not isinstance(meta, dict):
            return None
        return {
            "embedding_model": meta.get("embedding_model"),
            "built_at": meta.get("built_at"),
            "n_niches": meta.get("n_niches"),
            "note": "Topic-cluster (niche) columns come from a semantic map of video embeddings.",
        }
    except Exception as exc:
        logger.warning(f"[MethodsNote] Could not read video_map_meta.json: {exc}")
        return None






def build_methods_note(
    study_name: str,
    study_config: dict,
    df_study: pd.DataFrame | None,
    df_status: pd.DataFrame | None = None,
    stats: dict | None = None,
    refresh_action: str | None = None,
    refresh_trigger: str | None = None,
) -> dict:
    """Assemble the methods/provenance note for a study.

    Args:
        study_name: The study's name (key in ``studies.json``).
        study_config: The study definition dict.
        df_study: The study's recoded dataset (may be ``None`` — the note then
            carries registry/config facts only and says so).
        df_status: Projected enrichment_status frame (currently unused beyond
            ``stats``, accepted for parity with the refresh workers).
        stats: The freshly computed ``compute_study_dataset_stats`` dict, so
            counts are identical to what ``studies.json`` records.
        refresh_action: ``full_rebuild`` / ``enrichment_patch`` /
            ``short_circuit`` (from the recoded frame's attrs).
        refresh_trigger: ``study_save`` (single-study refresh) or ``pipeline``
            (bulk recode refresh).

    Returns:
        The note dict (JSON-serialisable).
    """
    sample_frame = str(study_config.get("SAMPLE_FRAME", "off"))
    sampling_active = sample_frame != "off"

    selection: dict = {
        "collections_selected": len(study_config.get("SELECTED_COLLECTIONS") or []),
        "date_window": _date_window(study_config, df_study),
        "sample_frame": sample_frame,
        "sample_frame_label": _SAMPLE_FRAME_LABELS.get(sample_frame, sample_frame),
        "sampling_active": sampling_active,
        "activity_filter": "plays and observations only",
        "activity_filter_note": (
            "Other activity types (likes, shares, searches...) are folded into "
            "the matching viewing rows during ingestion rather than kept as "
            "separate rows."
        ),
        "grouping_factors": study_config.get("GROUPING_FACTORS"),
    }
    if sampling_active:
        selection["thresholds"] = {
            "min_activity_per_group": study_config.get("MIN_ACTIVITY_COUNT_PER_GROUP"),
            "max_activity_per_group": study_config.get("MAX_ACTIVITY_COUNT_PER_GROUP"),
            "min_groups_per_collection": study_config.get("MIN_GROUP_COUNT_PER_COLLECTION"),
            "max_groups_per_collection": study_config.get("MAX_GROUP_COUNT_PER_COLLECTION"),
        }
        selection["random_seed"] = 42
        selection["random_seed_note"] = (
            "Sampling uses a fixed random seed, so rebuilding the study from "
            "the same inputs selects the same rows."
        )
        report = (df_study.attrs.get("sampling_report") if df_study is not None else None)
        if isinstance(report, dict):
            selection["sampling_report"] = {
                "collections_excluded_by_thresholds": report.get("n_excluded_collections"),
                "collections_downsampled": report.get("n_downsampled_collections"),
            }

    counts: dict = {}
    if isinstance(stats, dict):
        counts = {
            "activities": stats.get("total_activities"),
            "unique_videos": stats.get("unique_videos"),
            "collections": stats.get("unique_collections"),
            "active_days": stats.get("active_days"),
            "videos_scraped": stats.get("scraped_videos"),
            "videos_annotated": stats.get("annotated_videos"),
        }
    elif df_study is not None:
        counts = {
            "activities": int(len(df_study)),
            "unique_videos": int(df_study["item_id"].nunique()) if "item_id" in df_study.columns else None,
            "collections": int(df_study["collection_id"].nunique()) if "collection_id" in df_study.columns else None,
        }
    counts["counts_note"] = (
        "Counts cover watched/observed videos inside each collection's event window."
    )

    try:
        source_parquet_mtime = data_io.getmtime(
            storage_location="cache", filename=f"{study_name}_recoded.parquet"
        )
    except Exception:
        source_parquet_mtime = None

    note = {
        "schema_version": SCHEMA_VERSION,
        "generator_version": _fyp_version,
        "study": {
            "name": study_name,
            "last_updated": study_config.get("last_updated"),
        },
        "selection": selection,
        "counts": counts,
        "annotation": _annotation_block(study_config, df_study),
        "contracts": {
            "scrape_versions_in_rows": _version_distribution(df_study, "scrape_contract_version"),
            "activity_versions_in_rows": _version_distribution(df_study, "activity_contract_version"),
        },
        "semantic_map": _semantic_map_block(df_study),
        "freshness": {
            "built_at": datetime.now(UTC).isoformat(),
            "source_parquet_mtime": source_parquet_mtime,
            "refresh_action": refresh_action,
            "refresh_trigger": refresh_trigger,
            "row_level_fields_from": "dataframe" if df_study is not None else "unavailable",
        },
    }
    return note






def write_methods_note(
    study_name: str,
    study_config: dict,
    df_study: pd.DataFrame | None,
    df_status: pd.DataFrame | None = None,
    stats: dict | None = None,
    refresh_action: str | None = None,
    refresh_trigger: str | None = None,
) -> dict | None:
    """Build and persist the methods note for a study. Never raises.

    Returns the note written, or ``None`` on failure (the refresh must not be
    blocked by a provenance-note problem).
    """
    try:
        note = build_methods_note(
            study_name=study_name,
            study_config=study_config,
            df_study=df_study,
            df_status=df_status,
            stats=stats,
            refresh_action=refresh_action,
            refresh_trigger=refresh_trigger,
        )
        data_io.save_json(
            data=note,
            storage_location=NOTE_LOCATION,
            filename=note_filename(study_name),
            verbose=False,
        )
        return note
    except Exception as exc:
        logger.warning(f"[MethodsNote] Non-fatal: could not write note for '{study_name}': {exc}")
        return None






def read_methods_note(study_name: str) -> dict | None:
    """Load a study's methods note, or ``None`` if missing/malformed."""
    try:
        if not data_io.exists(storage_location=NOTE_LOCATION, filename=note_filename(study_name)):
            return None
        note = data_io.load_json(
            storage_location=NOTE_LOCATION, filename=note_filename(study_name), verbose=False
        )
        return note if isinstance(note, dict) else None
    except Exception as exc:
        logger.warning(f"[MethodsNote] Could not read note for '{study_name}': {exc}")
        return None






def note_staleness(study_name: str, note: dict) -> dict:
    """Freshness signal: is the note behind the study's recoded parquet?

    Mirrors ``correlations_service.build_status_payload`` — informational only,
    with a 1-second tolerance on the mtime comparison.
    """
    try:
        recoded_mtime = data_io.getmtime(
            storage_location="cache", filename=f"{study_name}_recoded.parquet"
        )
    except Exception:
        recoded_mtime = None
    note_mtime = (note.get("freshness") or {}).get("source_parquet_mtime")
    stale = bool(
        recoded_mtime is not None
        and note_mtime is not None
        and recoded_mtime > note_mtime + 1
    )
    return {"stale": stale, "recoded_updated_at": recoded_mtime}
