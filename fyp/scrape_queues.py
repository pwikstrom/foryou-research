#!/usr/bin/env python3
"""Per-platform scrape queue files.

Each platform has its own queue — ``to_scrape_<platform>.json`` in the
``"cache"`` storage location — drained by its own ``queue_scraper_<platform>``
worker process. This module is the single owner of queue file naming, the
one-time migration from the legacy single ``to_scrape.json``, and the
load / save / append / prune operations previously duplicated across
``fyp.scrape`` and ``web_interface.run_queue_scraper``.

Queue contents are plain lists of item-id strings; deduplication preserves
first-seen order.
"""

from fyp.logging_setup import get_logger

logger = get_logger(__name__)

LEGACY_QUEUE_FILENAME = "to_scrape.json"
QUEUE_LOCATION = "cache"


def _data_io():
    """Lazy fyp.data_io accessor (avoids the fyp_config import cycle)."""
    import fyp.data_io as data_io

    return data_io






def _contract():
    """Lazy fyp.scrape_contract accessor."""
    import fyp.scrape_contract as sc

    return sc






def default_platform() -> str:
    """Return the contract's default platform (falls back to ``"tiktok"``)."""
    sc = _contract()
    return sc.default_platform(sc.load_contract()) or "tiktok"






def registered_platforms() -> list[str]:
    """Return every platform that owns fields in the scrape contract."""
    sc = _contract()
    plats = sc.platforms(sc.load_contract())
    default = default_platform()
    if default not in plats:
        plats = [default] + plats
    return plats






def queue_filename(platform: str) -> str:
    """Return the queue filename for one platform."""
    return f"to_scrape_{platform}.json"






def _dedup(items: list) -> list[str]:
    """Deduplicate preserving first-seen order, dropping non-string junk."""
    return list(dict.fromkeys(str(v) for v in items if v))






def migrate_legacy_queue(platform: str) -> None:
    """Fold a legacy single ``to_scrape.json`` into the default platform's queue.

    Only acts when ``platform`` is the contract's default platform (the legacy
    queue predates multi-platform support, so its items belong there). If both
    the legacy and the per-platform file exist — possible when the web and
    task-runner services race — the two lists are unioned. Idempotent: after
    the first successful call the legacy file is gone.

    Args:
        platform: The platform whose queue is about to be read.
    """
    data_io = _data_io()
    if platform != default_platform():
        return
    if not data_io.exists(storage_location=QUEUE_LOCATION, filename=LEGACY_QUEUE_FILENAME):
        return
    legacy = data_io.load_json(storage_location=QUEUE_LOCATION, filename=LEGACY_QUEUE_FILENAME)
    legacy_items = _dedup(legacy) if isinstance(legacy, list) else []
    target = queue_filename(platform)
    if data_io.exists(storage_location=QUEUE_LOCATION, filename=target):
        current = data_io.load_json(storage_location=QUEUE_LOCATION, filename=target)
        current_items = _dedup(current) if isinstance(current, list) else []
        merged = _dedup(current_items + legacy_items)
    else:
        merged = legacy_items
    data_io.save_json(data=merged, storage_location=QUEUE_LOCATION, filename=target)
    data_io.remove(storage_location=QUEUE_LOCATION, filename=LEGACY_QUEUE_FILENAME)
    logger.info(f"Migrated legacy {LEGACY_QUEUE_FILENAME} -> {target} ({len(merged)} items)")






def load_scrape_queue(platform: str) -> list[str]:
    """Load one platform's scrape queue (running the legacy migration first).

    Args:
        platform: Platform whose queue to load.

    Returns:
        The queued item ids, or ``[]`` when the queue is missing or invalid.
    """
    data_io = _data_io()
    migrate_legacy_queue(platform)
    target = queue_filename(platform)
    if not data_io.exists(storage_location=QUEUE_LOCATION, filename=target):
        return []
    items = data_io.load_json(storage_location=QUEUE_LOCATION, filename=target)
    if not isinstance(items, list):
        return []
    return _dedup(items)






def save_scrape_queue(platform: str, items: list[str]) -> None:
    """Persist one platform's queue (deduplicated, order-preserving).

    Args:
        platform: Platform whose queue to save.
        items: Item ids to store.
    """
    data_io = _data_io()
    data_io.save_json(
        data=_dedup(items),
        storage_location=QUEUE_LOCATION,
        filename=queue_filename(platform),
    )






def append_to_scrape_queue(platform: str, items: list[str]) -> int:
    """Append items to one platform's queue.

    Args:
        platform: Platform whose queue to extend. Must be registered in the
            scrape contract — an unregistered platform has no worker to drain
            its queue, so appending would strand the items in an orphan file
            invisible to the queue UI.
        items: Item ids to add (duplicates are dropped).

    Returns:
        The queue length after the append.

    Raises:
        ValueError: if ``platform`` is not registered in the scrape contract.
    """
    if platform not in registered_platforms():
        raise ValueError(
            f"Platform '{platform}' is not registered in the scrape contract — "
            f"no queue worker exists to drain its queue."
        )
    merged = _dedup(load_scrape_queue(platform) + list(items))
    save_scrape_queue(platform, merged)
    return len(merged)






def prune_scrape_queue(platform: str, remove_ids: set[str]) -> tuple[int, int]:
    """Remove finished items from one platform's queue.

    Reloads the queue fresh before pruning so ids appended by another process
    while a batch ran are never clobbered.

    Args:
        platform: Platform whose queue to prune.
        remove_ids: Item ids to drop (successes + permanent failures).

    Returns:
        Tuple of ``(pruned_count, remaining_count)``.
    """
    current = load_scrape_queue(platform)
    updated = [v for v in current if v not in remove_ids]
    if len(updated) != len(current):
        save_scrape_queue(platform, updated)
    return len(current) - len(updated), len(updated)






def remove_scrape_queue(platform: str) -> None:
    """Delete one platform's queue file (and any legacy file it would absorb)."""
    data_io = _data_io()
    migrate_legacy_queue(platform)
    target = queue_filename(platform)
    if data_io.exists(storage_location=QUEUE_LOCATION, filename=target):
        data_io.remove(storage_location=QUEUE_LOCATION, filename=target)






def queue_lengths() -> dict[str, int]:
    """Return ``{platform: queue length}`` for every registered platform."""
    return {p: len(load_scrape_queue(p)) for p in registered_platforms()}
