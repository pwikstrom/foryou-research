"""Abstract base class and registry for platform scrapers.

Mirrors the collection-ingestion design in :mod:`fyp.ingest`
(``ForYouBaseCollection`` + ``TikTokDDPCollection``): an abstract
:class:`BaseScraper` whose subclasses auto-register at import, a canonical
cross-platform field set loaded from the scrape contract
(``config/scrape_contract.toml``) rather than hard-coded, and shared derivations
every platform reuses (per-K engagement rates, plays-per-day).

A new platform (Instagram Reels, YouTube Shorts, ...) is one subclass that
implements the five platform-specific operations — :meth:`~BaseScraper.item_url`,
:meth:`~BaseScraper.fetch`, :meth:`~BaseScraper.map_to_canonical`,
:meth:`~BaseScraper.classify_error`, :meth:`~BaseScraper.repair_counts` — plus a
``scope="platform"`` block in the contract. No orchestration code changes.
"""

import logging
import threading
from abc import ABC, abstractmethod

import pandas as pd

from fyp import scrape_contract as sc

logger = logging.getLogger(__name__)


# Platform-scraper subclasses live in their own ``fyp/<platform>_dl.py`` modules
# (unlike ingest.py, where base + subclasses share one file). They must be
# imported for ``__init_subclass__`` to register them; ``get_scraper`` imports
# them lazily so this module never imports a subclass at load time (which would
# be circular — each subclass imports ``BaseScraper`` from here).
_SCRAPER_MODULES = ("fyp.tiktok_dl",)




class BaseScraper(ABC):
    """Abstract platform scraper.

    A subclass implements the five platform-specific operations; the base
    provides the canonical field set + PyArrow dtypes (from the scrape contract),
    the shared derivations (:meth:`derive_engagement_rates`,
    :meth:`derive_plays_per_day`), batch canonicalization
    (:meth:`canonicalize_batch`), and the registry consumed by
    :func:`get_scraper`. Subclasses auto-register via ``__init_subclass__``,
    exactly like ``ForYouBaseCollection`` in :mod:`fyp.ingest`.

    Attributes:
        platform: the platform key a subclass owns (e.g. ``"tiktok"``); matched
            against ``[meta].default_platform`` and the contract's
            ``scope="platform"`` fields.
        base_columns: ``{column: pyarrow_dtype}`` for the canonical base fields.
        platform_columns: ``{column: pyarrow_dtype}`` for this platform's fields.
    """

    platform: str | None = None
    _registry: list[type] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.__name__ != "BaseScraper":
            BaseScraper._registry.append(cls)


    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self._contract = sc.load_contract()
        self.base_columns: dict[str, str] = sc.field_dtypes(self._contract)
        self.platform_columns: dict[str, str] = {
            name: dtype
            for name, dtype in sc.field_dtypes(self._contract, self.platform).items()
            if name not in self.base_columns
        }
        self._per_k: dict[str, str] = (
            sc.per_k_sources(self._contract, self.platform) if self.platform else {}
        )


    # -----------------------------------------------------------------
    # Abstract — a new platform implements these five.
    # -----------------------------------------------------------------

    @abstractmethod
    def item_url(self, item_id: str) -> str:
        """Return the canonical web URL for a platform item id."""


    @abstractmethod
    def fetch(
        self,
        item_id: str,
        *,
        save_media: bool,
        save_path: str,
        stream_to_bucket=None,
        verbose: bool = False,
    ) -> pd.DataFrame:
        """Fetch one item's raw single-row frame and, when ``save_media``, store its media.

        Returns a single-row DataFrame in the platform's native column names. On
        failure returns an empty DataFrame carrying ``attrs['error_type']`` and
        ``attrs['error_detail']`` for retry/queue decisions.
        """


    @abstractmethod
    def map_to_canonical(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Rename a raw frame's platform-native columns to the canonical names.

        Pure column algebra (rename only). Derived columns (per-K rates,
        plays_per_day, scrape_status) are added by the base, not here.
        """


    @abstractmethod
    def classify_error(self, error_type: str | None) -> str:
        """Map a fetch error category to a ``scrape_status``.

        Returns ``"ok"`` when ``error_type`` is ``None``, else
        ``"permanent:<reason>"`` or ``"transient:<reason>"`` — the prefix drives
        whether the orchestrator prunes the item from the retry queue.
        """


    @abstractmethod
    def repair_counts(self, df: pd.DataFrame) -> pd.DataFrame:
        """Repair platform count quirks (e.g. 32-bit overflow) before rates.

        Platforms with clean counts return ``df`` unchanged.
        """


    # -----------------------------------------------------------------
    # Overridable — sensible defaults, platform-specific when needed.
    # -----------------------------------------------------------------

    def throttle_limits(self, max_workers: int) -> tuple[int, int, int]:
        """Return ``(initial, minimum, maximum)`` concurrency for a batch.

        Platforms with stricter rate limits (or a single authenticated
        session) override this to cap the ceiling.
        """
        return (max_workers, 2, max(max_workers, 12))


    def health_check(self) -> dict | None:
        """Optional pre-batch health probe (auth/cookies/API quota).

        Returns:
            ``{"status": ..., "message": ...}`` for the orchestrator to log,
            or ``None`` when the platform has nothing to report.
        """
        return None


    # -----------------------------------------------------------------
    # Concrete — shared by every platform.
    # -----------------------------------------------------------------

    def derive_engagement_rates(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add each ``*_per_K_play = raw_count / play_count * 1000``.

        The rate → raw-count mapping comes from ``[perk.<platform>]`` in the
        contract. A rate is NA where ``play_count <= 0`` or the raw count is
        missing (negative sentinel). This is the per-thousand-plays form; the
        ×1000 is the only numeric change from the retired per-play columns.
        """
        if "play_count" not in df.columns:
            return df
        plays = df["play_count"].astype("double[pyarrow]")
        denom = plays.mask(plays <= 0, pd.NA)
        for rate_field, count_col in self._per_k.items():
            if count_col not in df.columns:
                continue
            num = df[count_col].astype("double[pyarrow]")
            num = num.mask(num < 0, pd.NA)
            df[rate_field] = ((num / denom) * 1000).astype("double[pyarrow]")
        return df


    def derive_plays_per_day(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add ``plays_per_day = play_count / days-since-upload`` at scrape time.

        Days = ``(scrape_ts - create_time).days`` clipped to ``>= 0``; the
        divisor is floored at 1 and the result is NA where either operand is
        missing. Computed once per item at scrape time, replacing the
        activity-local-time version that organize_datasets computed per activity.
        """
        if not {"play_count", "create_time", "scrape_ts"}.issubset(df.columns):
            return df
        created = pd.to_datetime(df["create_time"], errors="coerce")
        scraped = pd.to_datetime(df["scrape_ts"], errors="coerce")
        days = (scraped - created).dt.days.clip(lower=0)
        plays = df["play_count"].astype("double[pyarrow]")
        denom = days.clip(lower=1).mask(plays.isna() | days.isna(), pd.NA)
        df["plays_per_day"] = (plays / denom).astype("double[pyarrow]")
        return df


    def ensure_base_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add any missing base column as an all-NA column of its contract dtype.

        Guarantees a complete canonical schema even for partial/failure frames,
        so downstream concatenation and filtering never trip on an absent column.
        """
        for name, dtype in self.base_columns.items():
            if name not in df.columns:
                df[name] = pd.Series(pd.NA, index=df.index, dtype=dtype)
        return df


    def canonicalize_batch(self, df: pd.DataFrame, status: str = "ok") -> pd.DataFrame:
        """Turn a raw batch frame into canonical columns plus derived fields.

        Runs :meth:`map_to_canonical` → :meth:`repair_counts` →
        :meth:`derive_engagement_rates` → :meth:`derive_plays_per_day`, stamps
        ``scrape_status``, and ensures every base column exists. Identity and
        platform columns (``item_id``, ``image_list``, ...) are left in place —
        the var_schema filter in :mod:`fyp.scrape` selects the stored set.

        Args:
            df: the concatenated raw single-row frames for a batch.
            status: the ``scrape_status`` to stamp (``"ok"`` for saved rows).

        Returns:
            The same frame with canonical names and derived columns added.
        """
        df = self.map_to_canonical(df)
        df = self.repair_counts(df)
        df = self.derive_engagement_rates(df)
        df = self.derive_plays_per_day(df)
        df["scrape_status"] = pd.Series(status, index=df.index, dtype="string[pyarrow]")
        df["source_platform"] = pd.Series(self.platform, index=df.index, dtype="string[pyarrow]")
        from fyp import scrape_versioning
        df = scrape_versioning.stamp_version(df)
        df = self.ensure_base_columns(df)
        return df




_THROTTLE_CATEGORIES = {"rate_limited"}


class ThrottleController:
    """Dynamic concurrency controller that reacts to platform rate signals.

    Platform-agnostic: workers call ``acquire()`` before each item and
    ``report_result()`` after with the scraper's error category. The
    controller adjusts the semaphore so that fewer workers run concurrently
    when rate-limit signals arrive, and gradually recovers when things are
    healthy. Per-platform concurrency bounds come from
    :meth:`BaseScraper.throttle_limits`.

    Args:
        initial:  Starting concurrency (default 8).
        minimum:  Floor — never go below this (default 2).
        maximum:  Ceiling — never exceed this (default 12).
        cooldown_successes: How many consecutive clean results before
            growing concurrency by 1 (default 10).
    """

    def __init__(
        self,
        initial: int = 8,
        minimum: int = 2,
        maximum: int = 12,
        cooldown_successes: int = 10,
        on_change: "callable | None" = None,
    ) -> None:
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(initial)
        self._current = initial
        self._minimum = minimum
        self._maximum = maximum
        self._cooldown_successes = cooldown_successes
        self._consecutive_ok = 0
        self._total_throttle_events = 0
        self._on_change = on_change

    # -- public API used by workers --

    def acquire(self) -> None:
        """Block until a concurrency slot is available."""
        self._sem.acquire()

    def release(self) -> None:
        """Return a concurrency slot (call in finally block)."""
        self._sem.release()

    def report_result(self, error_category: str | None) -> None:
        """Report the outcome of one item scrape.

        Args:
            error_category: The error category string from the scraper's
                error classification, or ``None`` for success.
        """
        with self._lock:
            if error_category in _THROTTLE_CATEGORIES:
                self._consecutive_ok = 0
                self._total_throttle_events += 1
                self._shrink()
            else:
                self._consecutive_ok += 1
                if self._consecutive_ok >= self._cooldown_successes:
                    self._consecutive_ok = 0
                    self._grow()

    # -- read-only properties --

    @property
    def current(self) -> int:
        with self._lock:
            return self._current

    @property
    def total_throttle_events(self) -> int:
        with self._lock:
            return self._total_throttle_events

    # -- internal helpers (caller holds _lock) --

    def _shrink(self) -> None:
        target = max(self._minimum, self._current // 2)
        drop = self._current - target
        if drop <= 0:
            return
        logger.warning("Throttle: reducing concurrency %d → %d", self._current, target)
        for _ in range(drop):
            self._sem.acquire(blocking=False)  # drain permits
        self._current = target
        if self._on_change:
            self._on_change(self._current)

    def _grow(self) -> None:
        if self._current >= self._maximum:
            return
        self._current += 1
        self._sem.release()  # add one permit
        logger.info("Throttle: growing concurrency to %d", self._current)
        if self._on_change:
            self._on_change(self._current)




def _ensure_scrapers_imported() -> None:
    """Import platform-scraper modules so their subclasses self-register.

    Lazy (called from :func:`get_scraper`, not at module load) to avoid the
    circular import that would arise from importing a subclass module here while
    that module imports :class:`BaseScraper`.
    """
    for module_name in _SCRAPER_MODULES:
        try:
            __import__(module_name)
        except Exception:
            pass




def get_scraper(platform: str | None = None, verbose: bool = False) -> BaseScraper:
    """Instantiate the registered scraper for a platform.

    Mirrors :func:`fyp.ingest.get_main_collection`'s registry iteration:
    subclasses auto-register at import, so adding a platform needs no edit here.

    Args:
        platform: platform key; defaults to ``[meta].default_platform`` in the
            scrape contract (``"tiktok"``).
        verbose: forwarded to the scraper instance.

    Returns:
        An instance of the matching :class:`BaseScraper` subclass.

    Raises:
        ValueError: if no registered subclass owns ``platform``.
    """
    contract = sc.load_contract()
    target = platform or sc.default_platform(contract)
    _ensure_scrapers_imported()
    for cls in BaseScraper._registry:
        if getattr(cls, "platform", None) == target:
            return cls(verbose=verbose)
    raise ValueError(f"No scraper registered for platform '{target}'")
