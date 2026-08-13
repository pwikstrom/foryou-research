"""Platform-agnostic scraping: orchestration, scrapers, queues, contract.

``fyp/scrape.py`` became this package; the ``__init__`` re-exports the old
module's surface so ``import fyp.scrape`` / ``from fyp.scrape import X`` are
unchanged. It eagerly imports ONLY ``.scrape`` — pulling the ``*_dl`` modules
here would load yt_dlp inside every config boot (scrapers stay lazy via
``platform_scraper._ensure_scrapers_imported``). Sibling imports inside this
package go through the package directly (never the old-path shims): a shim
import mid-cascade could bind a partially-initialized shim (see
docs/fyp-import-graph.md, "shim-poisoning rule").
"""

from fyp.scrape import scrape as _scrape_mod
from fyp.scrape.scrape import (
    CIRCUIT_BREAKER_THRESHOLD,
    _coalesce_retired_columns,
    _compute_changed_scrape_ids,
    _merge_enrichment_seeds,
    check_existing_media,
    consolidate_and_save_scrape_data,
    download_single_video,
    download_video_threads,
    load_failed_scrapes,
    load_failed_scrapes_detail,
    make_slideshow,
    queue_scraper_loop,
    scraper_loop_from_list,
)





def __getattr__(name: str):
    """Forward stragglers — incl. the lazy config constants — to the module."""
    return getattr(_scrape_mod, name)
