"""The failed-scrapes ledger: record items that failed to scrape, and read them back.

A batch's final failures are appended to timestamped JSON records, which are
merged into one ``{item_id: category}`` view; only final categories count as
failed for the queues.
"""

from datetime import datetime

import fyp.core.data_io as data_io
from fyp.core.logging_setup import get_logger
from fyp.core.runtime import label

logger = get_logger(__name__)


def _failed_scrapes_label() -> str:
    """Lazy accessor for the config-derived failed-scrapes label."""
    return label("FAILED_SCRAPES_LABEL")


def _merge_failed_scrape_records(records: dict[str, str | None], raw: list) -> None:
    """Merge one loaded failed-scrapes file into ``records`` in place.

    Two on-disk shapes coexist. Records written before the category was
    recorded are bare item-id strings carrying no reason; newer ones are
    ``{"item_id": ..., "category": "permanent:ip_blocked"}`` dicts. A known
    category always wins over a legacy id for the same item.

    Args:
        records: Accumulator mapping item id to category (``None`` if unknown).
        raw: The parsed contents of one failed-scrapes JSON file.
    """
    for entry in raw:
        if isinstance(entry, dict):
            item_id = entry.get("item_id")
            if item_id is not None:
                records[str(item_id)] = entry.get("category")
        else:
            records.setdefault(str(entry), None)


def record_failed_scrapes(failed_items: list[dict], verbose: bool = False) -> None:
    """Write one failed-scrapes ledger file recording the given items.

    Args:
        failed_items: ``{"item_id": ..., "category": ...}`` dicts — the same
            shape ``download_video_threads`` writes. The next
            ``_load_failed_scrape_records`` folds the file into the
            consolidated ledger, where the category overwrites any earlier
            record for the same item.
        verbose: Log the write.
    """
    if not failed_items:
        return
    fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
    data_io.save_json(
        data=failed_items,
        storage_location="scrape",
        filename=f"{_failed_scrapes_label()}_{fine_ts}.json",
        verbose=verbose,
    )


def _load_failed_scrape_records(verbose=False, super_verbose=False) -> dict[str, str | None]:
    """Load every recorded failed scrape as ``{item_id: category}``.

    Consolidates multiple on-disk records into one file and archives the
    originals, exactly as before; the consolidated file is written in the
    category-carrying shape, so a legacy id whose reason was never recorded
    survives consolidation with a ``None`` category rather than being dropped.

    Args:
        verbose: Log progress.
        super_verbose: Log each file name as it is read.

    Returns:
        Mapping of item id to its recorded category, ``None`` when unknown.
    """
    if verbose:
        logger.info("Loading failed scrapes...")

    # Oldest first, so that when an item has several records the latest wins
    # (the file names carry the time they were written).
    failed_scrapes_files = sorted(
        gg
        for gg in data_io.listdir(storage_location="scrape", verbose=verbose)
        if gg.startswith(_failed_scrapes_label())
    )

    records: dict[str, str | None] = {}
    for fn in failed_scrapes_files:
        if super_verbose:
            logger.info(fn)
        some_dict = data_io.load_json(storage_location="scrape", filename=fn, verbose=verbose)
        if some_dict is not None:
            _merge_failed_scrape_records(records, some_dict)

    if len(failed_scrapes_files) > 1:
        fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
        if verbose:
            logger.info(
                f"{len(records):,} of these are unique and will be saved as a new consolidated file {_failed_scrapes_label()}_{fine_ts}.json."
            )

        payload = [
            {"item_id": item_id, "category": category} for item_id, category in records.items()
        ]
        result = data_io.save_json(
            data=payload,
            storage_location="scrape",
            filename=f"{_failed_scrapes_label()}_{fine_ts}.json",
            verbose=verbose,
        )

        if result == 0:
            for fn in failed_scrapes_files:
                data_io.move(
                    src_storage_location="scrape",
                    dst_storage_location="archive",
                    filename=fn,
                    verbose=verbose,
                )
                if verbose:
                    logger.info(f"Moved {fn} to archive")

    if verbose:
        logger.info(f"Loaded list of all failed scrapes: {len(records):,}")

    return records


def _is_final_failure(category: str | None) -> bool:
    """Whether a recorded failure category means the scraper has given up.

    A record written before categories were stored carries none; it was only
    ever read as a final failure, so it still is.
    """
    return category is None or str(category).startswith("permanent")


def load_failed_scrapes(verbose=False, super_verbose=False):
    """Item ids whose most recent recorded fetch failure is final.

    The record also stores failures the scraper will retry (a timeout, a batch
    aborted by a storm guard, a rate limit), so that one kind of failure can be
    singled out later. Those items stay in the scrape queue and must not read
    as failed: this list feeds ``scrape_fail`` in the status table, which the
    enrichment plan and the scrape-queue builder skip and the coverage bar
    counts as failed for good. When an item has several records the latest
    wins, so a retryable failure after an earlier final one makes the item
    eligible again.

    Args:
        verbose: Log progress.
        super_verbose: Log each file name as it is read.

    Returns:
        The failed item ids, as strings.
    """
    records = _load_failed_scrape_records(verbose=verbose, super_verbose=super_verbose)
    return [item_id for item_id, category in records.items() if _is_final_failure(category)]


def load_failed_scrapes_detail(verbose=False, super_verbose=False) -> dict[str, str | None]:
    """Load failed scrapes with the reason each one failed.

    Use this instead of :func:`load_failed_scrapes` to select one kind of
    failure — e.g. re-queueing only the ``permanent:ip_blocked`` items after
    the scraper gains a different vantage point.

    Args:
        verbose: Log progress.
        super_verbose: Log each file name as it is read.

    Returns:
        Mapping of item id to its recorded category. ``None`` marks a record
        written before categories were stored, whose reason is unrecoverable.
    """
    return _load_failed_scrape_records(verbose=verbose, super_verbose=super_verbose)
