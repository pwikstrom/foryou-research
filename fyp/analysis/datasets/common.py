"""Shared names for the dataset build: column names, config-derived labels, sampling thresholds, and the niche-map constants."""

import pandas as pd

# Shared memory-probe implementations (fyp.core.memory); the module-private
# aliases keep this file's many existing call sites and the
# [RECODE][MEM]/[ENRICH PATCH][MEM] log lines unchanged.
from fyp.core.runtime import cf as _cf

collection_id_column = "collection_id"
timestamp_column = "local_timestamp"
event_type_column = "activity_type"


# Sentinel for an uncapped sampling maximum. A blank ('' / '-') max in a study
# definition means "no cap"; it is parsed to this value, which is larger than any real
# per-cell or per-collection count, so min(count, SAMPLE_NO_CAP) == count.
SAMPLE_NO_CAP = 10**12


def parse_sample_threshold(value, default: int, uncapped: bool = False) -> int:
    """Parse a sampling threshold from a study definition.

    A missing key falls back to `default` (preserving legacy behaviour). An explicitly
    blank value ('' or '-') means "no minimum" (0) for a min threshold, or "no cap"
    (SAMPLE_NO_CAP) for a max threshold (`uncapped=True`). Unparseable values fall back
    to `default`.

    Args:
        value: Raw config value (str / int / None).
        default: Fallback for a missing or unparseable value.
        uncapped: True for max thresholds, where a blank value means no cap.

    Returns:
        An integer threshold.
    """

    if value is None:
        return default
    s = str(value).strip()
    if s in ("", "-"):
        return SAMPLE_NO_CAP if uncapped else 0
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


def _scrapes_label() -> str:
    """Lazy accessor for the config-derived scrapes label."""
    return _cf()["labels"]["SCRAPES_LABEL"]


def _machine_annotations_label() -> str:
    """Lazy accessor for the config-derived machine-annotations label."""
    return _cf()["labels"]["MACHINE_ANNOTATIONS_LABEL"]


def _collections_label() -> str:
    """Lazy accessor for the config-derived collections label."""
    return _cf()["labels"]["COLLECTIONS_LABEL"]


# Embeddings-derived niche map (see fyp.analysis.video_map). The niche columns are
# joined into each study's recoded dataset on item_id so they surface as
# ordinary analysis variables; the map is rebuilt out-of-band, so its
# fingerprint guards study-cache freshness.
_VIDEO_MAP_LOCATION = "recoded"
_VIDEO_MAP_FILE = "video_map.parquet"
# Written by build_niche_map beside the map; carries niche_assignment_hash.
_VIDEO_MAP_META_FILE = "video_map_meta.json"
_NICHE_COLUMNS = ("niche", "niche_name", "typicality_pct", "niche_isolation_pct")
_NICHE_UNMAPPED = "unmapped"
# Backfill dtype + value per joined column, for rows the map does not cover and
# for a map file too old to carry the column at all. Only the readable niche
# label gets a stand-in value: an unmapped video has no honest typicality or
# isolation, and inventing one (0, or the corpus mean) would be a fabricated
# measurement in an analysis variable.
_NICHE_COLUMN_BACKFILL = {
    "niche": ("int32[pyarrow]", pd.NA),
    "niche_name": ("string[pyarrow]", _NICHE_UNMAPPED),
    "typicality_pct": ("double[pyarrow]", pd.NA),
    "niche_isolation_pct": ("double[pyarrow]", pd.NA),
}
