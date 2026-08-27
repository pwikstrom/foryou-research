"""Per-collection scraped/annotated coverage.

Coverage is the share of a collection's VIEW activities (play/observe — never
``total_events``, which counts likes/searches/follows that have no scrapeable
item) whose item is scraped / annotated in ``enrichment_status.parquet``.

Two tables report it and they have to agree: the participant-facing My
Collections table (one user's collections, read with a ``collection_id``
filter) and the admin Edit Collections table (every collection at once). The
per-collection arithmetic lives here so the two cannot drift, and so does the
corpus-wide scan — it reads every row of the recoded parquet, which is why it
is cached and why its consumer fetches it separately from the table it fills.
"""

import time

import pandas as pd

from fyp.core import data_io
from fyp.organize_datasets import COLLECTIONS_LABEL

RECODED_FILENAME = f"{COLLECTIONS_LABEL}_recoded.parquet"

VIEW_TYPES = ["play", "observe"]

# Enrichment moves in worker-sized steps, not continuously, and the Edit
# Collections table is opened repeatedly in a session — so the corpus scan is
# cached well past a single request.
_CORPUS_TTL_S = 300
_corpus_cache: dict[str, tuple[float, dict]] = {}


def coverage_from_activities(df) -> dict[str, dict]:
    """``{collection_id: {pct_scraped, pct_annotated}}`` for an activity frame.

    Args:
        df: Rows carrying ``collection_id`` / ``activity_type`` / ``item_id``.

    Returns:
        One entry per collection with view rows. A missing status table (or a
        collection with no view rows) yields no entry at all rather than a
        zero: the UI renders an em-dash for "not known", where 0% would claim
        the items were checked and found bare.
    """

    coverage: dict[str, dict] = {}
    if df is None or df.empty:
        return coverage

    from . import preview_cache

    status = preview_cache.get_enrichment_status_cached()
    views = df[df["activity_type"].astype(str).isin(VIEW_TYPES)]
    if status is None or not len(views):
        return coverage

    iid_keys = views["item_id"].astype(str).to_numpy()
    scraped, annotated = preview_cache.status_flags(iid_keys, status)
    flags = pd.DataFrame({
        "collection_id": views["collection_id"].astype(str).to_numpy(),
        "scraped": scraped,
        "annotated": annotated,
    })
    for cid, grp in flags.groupby("collection_id", observed=True):
        coverage[str(cid)] = {
            "pct_scraped": round(float(grp["scraped"].mean()), 4),
            "pct_annotated": round(float(grp["annotated"].mean()), 4),
        }
    return coverage


def corpus_coverage(force: bool = False) -> dict[str, dict]:
    """Coverage for every collection in the corpus, TTL-cached.

    One three-column projection of the whole recoded parquet. Any failure
    (no recoded file yet, an unreadable one) reports as "no coverage known"
    and is not cached — the caller's column simply stays em-dashed.
    """

    now = time.time()
    hit = _corpus_cache.get("all")
    if not force and hit and (now - hit[0]) < _CORPUS_TTL_S:
        return hit[1]

    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=RECODED_FILENAME,
            columns=["collection_id", "item_id", "activity_type"],
        )
        coverage = coverage_from_activities(df)
    except Exception as e:
        print(f"[collection_coverage] corpus scan failed: {e}")
        return {}

    _corpus_cache["all"] = (now, coverage)
    return coverage
