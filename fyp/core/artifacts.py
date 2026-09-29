"""Names and readers for the shared artifacts that several layers read.

``enrichment_status.parquet`` (built by consolidation in
:mod:`fyp.analysis.organize_datasets`) is read by the annotation pipeline, the
web services, the routes and the workers; the collections-metadata frame's
columns are stored under ``(group, field)`` tuples on current files and under
flat names on older ones. Both are named once, here. Depends only on
``data_io``, so any layer may import it.
"""

from collections.abc import Iterable

import pandas as pd

import fyp.core.data_io as data_io

ENRICHMENT_STATUS_LOCATION = "recoded"
ENRICHMENT_STATUS_FILE = "enrichment_status.parquet"

# Collections-metadata columns, as stored on current files.
ACCEPTED_COLUMN = ("other", "accepted")
ACTIVE_DAYS_COLUMN = ("personas", "active_days")


def load_enrichment_status() -> pd.DataFrame | None:
    """The whole enrichment-status frame, or None before the first consolidation."""
    if not data_io.exists(
        storage_location=ENRICHMENT_STATUS_LOCATION, filename=ENRICHMENT_STATUS_FILE
    ):
        return None
    return data_io.load_parquet(
        storage_location=ENRICHMENT_STATUS_LOCATION, filename=ENRICHMENT_STATUS_FILE
    )


def metadata_column_names(key: tuple[str, str], *flat_names: str) -> list[str]:
    """On-disk names to request for a metadata field from ``load_parquet_selective``:
    the stringified ``key`` tuple, then its flat fallbacks."""
    return [str(key), *flat_names]


def metadata_column(columns: Iterable, key: tuple[str, str], *flat_names: str):
    """Which column holds a metadata field: ``key`` when present, else the first
    of ``flat_names`` present (older flat files); None when none is."""
    present = set(columns)
    for candidate in (key, *flat_names):
        if candidate in present:
            return candidate
    return None
