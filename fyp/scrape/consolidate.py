"""Scrape consolidation: fold scrape batches into the recoded scrapes frame.

Reads the timestamped per-batch scrape parquets and the enrichment seeds,
normalises legacy columns, de-duplicates and resolves conflicting rows,
computes which items actually changed, and writes the consolidated
``<scrapes>_recoded.parquet`` plus its ledger. Called by the consolidation
pipeline (``organize_datasets``) and by ingest.
"""

import time

import pandas as pd

import fyp.core.data_io as data_io
from fyp.core.logging_setup import get_logger
from fyp.core.runtime import label

# Sibling imports go through the package (never the old-path shims): a
# shim import here could bind a
# partially-initialized shim during the boot cascade (shim-poisoning rule,
# docs/fyp-import-graph.md).
from fyp.scrape import scrape_contract as sc
from fyp.scrape import scrape_versioning
from fyp.scrape.platform_scraper import (
    get_scraper,
)

logger = get_logger(__name__)


def _scrapes_label() -> str:
    """Lazy accessor for the config-derived scrapes label."""
    return label("SCRAPES_LABEL")


def _parse_scrape_filename_ts(filename: str | None) -> "pd.Timestamp":
    """Best-effort parse a ``scrapes_<digits>.parquet`` filename into a Timestamp.

    Legacy raw scrape parquets predate the persisted ``scrape_ts`` column; the
    file's name encodes when the scrape ran (the digits of ``datetime.now()``),
    which serves as a per-file scrape time so ``plays_per_day`` can still be
    derived. Returns ``pd.NaT`` when the name carries no parseable timestamp.

    Args:
        filename: the scrape parquet filename.

    Returns:
        The parsed timestamp, or ``pd.NaT``.
    """
    if not filename:
        return pd.NaT
    digits = "".join(c for c in filename if c.isdigit())
    if len(digits) < 14:
        return pd.NaT
    try:
        return pd.to_datetime(digits[:14], format="%Y%m%d%H%M%S")
    except Exception:
        return pd.NaT


def _coalesce_retired_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Fold retired platform-specific columns into their generic base successors.

    Historical scrape parquets carry the pre-retirement per-platform names
    (``stats_diggCount`` / ``ig_like_count`` / ``yt_author_handle`` / ...); this
    coalesces each into its generic base column (``fave_count`` /
    ``author_handle`` / ...) per ``scrape_contract.RETIRED_TO_GENERIC`` and drops
    the source. A coalesce (not a rename) because several retired columns share
    one target — a rename would create duplicate labels on a mixed-platform
    frame. Values are kept verbatim, including the -1 missing-count sentinels
    (the rate/plays-per-day derivations mask negatives). Per-file parquets on
    disk keep their old names and are re-coalesced on every consolidation, the
    same self-healing convention as the legacy rename.

    Args:
        df: a single raw scrape parquet's frame (mutated and returned).

    Returns:
        The frame with generic columns populated and retired columns dropped.
    """
    present = [c for c in sc.RETIRED_TO_GENERIC if c in df.columns]
    if not present:
        return df
    target_dtypes = sc.field_dtypes(sc.load_contract())
    for src in present:
        tgt = sc.RETIRED_TO_GENERIC[src]
        dtype = target_dtypes.get(tgt)
        source = df[src].astype(dtype) if dtype else df[src]
        if tgt in df.columns:
            df[tgt] = df[tgt].combine_first(source)
            if dtype:
                df[tgt] = df[tgt].astype(dtype)
        else:
            df[tgt] = source
        df = df.drop(columns=[src])
    return df


def _canonicalize_legacy_scrape(
    df: pd.DataFrame, filename: str | None = None, scraper=None
) -> pd.DataFrame:
    """Migrate a legacy (pre-canonical) scrape parquet to the canonical schema.

    New scrape parquets are already saved with canonical column names, per-K
    engagement rates, plays_per_day, and scrape_status, so this is a no-op for
    them. A legacy parquet (TikTok-named columns, no per-K, ``scraped_ok`` bool)
    is renamed to canonical, its counts overflow-repaired, the per-K rates and
    plays_per_day derived, ``scrape_ts`` back-filled from the filename timestamp,
    and ``scrape_status`` back-filled from ``scraped_ok`` — so old and new files
    concatenate into one canonical frame.

    Args:
        df: a single raw scrape parquet's frame.
        filename: the parquet filename (back-fills scrape_ts for legacy frames).
        scraper: a platform scraper instance (created if not provided).

    Returns:
        The canonical frame (the input unchanged when already canonical).
    """
    if not any(c in df.columns for c in sc.LEGACY_COLUMN_ALIASES):
        return df
    df = df.rename(columns=sc.LEGACY_COLUMN_ALIASES)
    if "scrape_ts" not in df.columns or df["scrape_ts"].isna().all():
        df["scrape_ts"] = pd.Series(
            _parse_scrape_filename_ts(filename), index=df.index, dtype="timestamp[ns][pyarrow]"
        )
    if scraper is None:
        scraper = get_scraper(verbose=False)
    df = scraper.repair_counts(df)
    df = scraper.derive_engagement_rates(df)
    df = scraper.derive_plays_per_day(df)
    if "scrape_status" not in df.columns:
        if "scraped_ok" in df.columns:
            ok = df["scraped_ok"].astype("boolean").fillna(False)
            df["scrape_status"] = ok.map({True: "ok", False: "failed"}).astype("string[pyarrow]")
        else:
            df["scrape_status"] = pd.Series("ok", index=df.index, dtype="string[pyarrow]")
    # Complete the canonical schema: base fields the legacy frame never carried
    # (e.g. storage_link — no media path was recorded historically) become all-NA.
    df = scraper.ensure_base_columns(df)
    return df


def _load_enrichment_seeds(verbose: bool = False) -> dict[str, pd.DataFrame]:
    """Load all donated enrichment-seed parquets from the ``recoded`` location.

    The seeds are written per platform by ``fyp.ingest.save_enrichment_seed``
    (``{platform}_{source}_enrichment_seed.parquet``, canonical scrape base
    schema, ``scrape_status="donated"``).

    Returns:
        ``{filename: DataFrame}`` for every non-empty seed file found.
    """
    seeds: dict[str, pd.DataFrame] = {}
    for fn in data_io.listdir(storage_location="recoded"):
        if fn.endswith("_enrichment_seed.parquet"):
            df = data_io.load_parquet(storage_location="recoded", filename=fn)
            if df is not None and len(df) > 0:
                seeds[fn] = df
                if verbose:
                    logger.info(f"    Loaded {len(df):,} donated seed rows from {fn}")
    return seeds


def _ensure_seed_flag(scrape_df: pd.DataFrame) -> pd.DataFrame:
    """Guarantee the ``is_enrichment_seed`` provenance column (False = real row).

    The incremental fold uses this flag to evict every seed row from the
    previous consolidation before re-running the seed anti-join against the
    current seed files — that is what lets a real scrape arriving later win
    over the donated row without rebuilding from all files.
    """
    if "is_enrichment_seed" not in scrape_df.columns:
        scrape_df["is_enrichment_seed"] = pd.Series(
            False, index=scrape_df.index, dtype="bool[pyarrow]"
        )
    else:
        scrape_df["is_enrichment_seed"] = (
            scrape_df["is_enrichment_seed"].fillna(False).astype("bool[pyarrow]")
        )
    return scrape_df


def _merge_enrichment_seeds(
    scrape_df: pd.DataFrame,
    seed_frames: dict[str, pd.DataFrame],
    verbose: bool = False,
) -> pd.DataFrame:
    """Append donated seed rows for items that have no real scrape row.

    Precedence is a plain anti-join on ``(source_platform, item_id)``: any real
    scrape row beats a donated one, and a later consolidation with a real
    scrape drops the donated row — the full rebuild does that by construction,
    and the incremental fold by evicting all ``is_enrichment_seed`` rows before
    re-running this anti-join. Donated rows are stamped ``scraped_ok=False`` /
    ``video_downloaded=False`` so the items stay scrape-eligible while their
    donated caption/author metadata surfaces downstream.
    """
    scrape_df = _ensure_seed_flag(scrape_df)
    if not seed_frames:
        return scrape_df

    seeds = pd.concat(list(seed_frames.values()), ignore_index=True)
    seeds = seeds[seeds["item_id"].notna()].copy()
    seeds = seeds.drop_duplicates(subset=["source_platform", "item_id"], keep="first")

    if len(scrape_df) > 0 and {"source_platform", "item_id"}.issubset(scrape_df.columns):
        real_keys = pd.MultiIndex.from_frame(
            scrape_df[["source_platform", "item_id"]].astype("string[pyarrow]")
        )
        seed_keys = pd.MultiIndex.from_frame(
            seeds[["source_platform", "item_id"]].astype("string[pyarrow]")
        )
        seeds = seeds[~seed_keys.isin(real_keys)].copy()

    if len(seeds) == 0:
        return scrape_df

    seeds["scraped_ok"] = pd.Series(False, index=seeds.index, dtype="bool[pyarrow]")
    seeds["video_downloaded"] = pd.Series(False, index=seeds.index, dtype="bool[pyarrow]")
    seeds["storage_link"] = pd.Series("", index=seeds.index, dtype="string[pyarrow]")
    seeds["is_enrichment_seed"] = pd.Series(True, index=seeds.index, dtype="bool[pyarrow]")

    if verbose:
        logger.info(
            f"    Adding {len(seeds):,} donated seed row(s) for items without a real scrape."
        )
    return pd.concat([scrape_df, seeds], ignore_index=True)


# Backstage/provenance columns that change on every (re-)scrape without altering
# any analysis variable. They are excluded from the consolidation value diff so a
# value-preserving re-scrape (or a plain force re-consolidation) flags nothing,
# while a real backfill — e.g. play_count -1 sentinel → a real count — is caught.
SCRAPE_PROVENANCE_COLS = frozenset(
    {
        "scrape_ts",
        "scrape_contract_version",
        "storage_link",
        # Seed provenance: which store a row came from is not an analysis value,
        # and the column's first appearance (post-deploy full rebuild) must not
        # read as a schema change that flags every item.
        "is_enrichment_seed",
    }
)


def scrape_value_signatures(df: pd.DataFrame, value_cols: list[str]) -> dict[str, str]:
    """Per-item content signature over the given value columns.

    Normalises every cell to a string (so pyarrow/int/bool/datetime dtypes and
    NA hash identically across the freshly-built frame and the frame loaded back
    from parquet), hashes each row, and combines the (order-independent) set of
    row hashes per ``item_id`` into a comparable signature string. Rows with a
    null ``item_id`` are ignored.

    Args:
        df: Consolidated scrape frame (existing or new).
        value_cols: Columns whose values define "changed"; provenance columns
            must already be excluded by the caller.

    Returns:
        ``{item_id: signature}`` — two frames agree on an item iff its signature
        matches.
    """
    if df is None or df.empty or "item_id" not in df.columns:
        return {}

    sub = df.loc[df["item_id"].notna(), ["item_id", *value_cols]]
    if sub.empty:
        return {}

    normalised = sub[value_cols].astype("string").fillna("\x00")
    row_hashes = pd.util.hash_pandas_object(normalised, index=False).to_numpy()
    frame = pd.DataFrame(
        {
            "item_id": sub["item_id"].astype("string").to_numpy(),
            "row_hash": [format(int(h), "x") for h in row_hashes],
        }
    )
    combined = frame.groupby("item_id", sort=False)["row_hash"].agg(
        lambda hashes: ",".join(sorted(hashes))
    )
    return {str(item): sig for item, sig in combined.items()}


def _compute_changed_scrape_ids(
    existing_df: pd.DataFrame | None,
    new_df: pd.DataFrame,
    verbose: bool = False,
    candidate_item_ids: set[str] | None = None,
) -> set[str]:
    """Item_ids whose consolidated scrape row changed vs the previous output.

    Returns the union of brand-new item_ids (present in ``new_df``, absent from
    ``existing_df``) and item_ids present in both whose value columns differ —
    the re-scrape backfill case (updating stale/missing scraped fields for items
    already consolidated) that a pure new-id set-difference silently misses. The
    result drives the consolidation impact analysis, so any study whose member
    items had their enrichment *values* updated is refreshed, not only studies
    that gained or lost members.

    A change in the value-column SET itself (a contract migration renaming or
    coalescing columns, or a new platform's first columns) marks every item as
    changed: the per-row signatures only cover the column intersection, so a
    pure schema change would otherwise diff as "nothing changed" and the
    downstream study refresh would never pick up the new columns.

    Args:
        existing_df: The previously consolidated scrape frame (``None`` on the
            first-ever consolidation).
        new_df: The freshly consolidated scrape frame about to be saved.
        verbose: Print a one-line changed/new/updated breakdown.
        candidate_item_ids: When given, only these item_ids can have changed
            (they came from the new batch files / changed seed files), so the
            per-row signatures are computed over just their rows instead of the
            whole corpus — O(batch) instead of the measured ~270 s full-frame
            pass. ``None`` keeps the full diff. A value-column-set change
            ignores the candidates and still flags every item: the signatures
            only cover the column intersection, so a schema move must not be
            narrowed. Callers must pass ``None`` when items outside the batch
            can change (force re-consolidation, contract-version bump).

    Returns:
        The set of changed item_ids.
    """
    if new_df is None or new_df.empty or "item_id" not in new_df.columns:
        return set()

    if existing_df is None or existing_df.empty or "item_id" not in existing_df.columns:
        return {str(i) for i in new_df.loc[new_df["item_id"].notna(), "item_id"]}

    def _value_col_set(df: pd.DataFrame) -> set[str]:
        return {c for c in df.columns if c != "item_id" and c not in SCRAPE_PROVENANCE_COLS}

    if _value_col_set(new_df) != _value_col_set(existing_df):
        if verbose:
            added = sorted(_value_col_set(new_df) - _value_col_set(existing_df))
            removed = sorted(_value_col_set(existing_df) - _value_col_set(new_df))
            logger.info(
                f"Scrape column set changed (+{added} / -{removed}) — flagging all items as changed."
            )
        return {str(i) for i in new_df.loc[new_df["item_id"].notna(), "item_id"]}

    # Column sets match — from here on, only rows named in the candidate set
    # can differ, so the signature pass shrinks to those rows.
    if candidate_item_ids is not None:
        existing_df = existing_df.loc[existing_df["item_id"].isin(candidate_item_ids)]
        new_df = new_df.loc[new_df["item_id"].isin(candidate_item_ids)]
        if new_df.empty and existing_df.empty:
            return set()

    value_cols = [
        c
        for c in new_df.columns
        if c != "item_id" and c not in SCRAPE_PROVENANCE_COLS and c in existing_df.columns
    ]
    if not value_cols:
        existing_ids = {str(i) for i in existing_df["item_id"] if pd.notna(i)}
        return {str(i) for i in new_df["item_id"] if pd.notna(i) and str(i) not in existing_ids}

    old_sig = scrape_value_signatures(existing_df, value_cols)
    new_sig = scrape_value_signatures(new_df, value_cols)
    changed = {item for item, sig in new_sig.items() if old_sig.get(item) != sig}

    if verbose:
        new_count = sum(1 for item in changed if item not in old_sig)
        logger.info(
            f"Found {len(changed):,} changed scrape item_id(s) "
            f"({new_count:,} new, {len(changed) - new_count:,} re-scraped/updated)."
        )
    return changed


def _write_scrape_ledger(
    dataset_meta: dict,
    files_to_concatenate: list[str],
    seed_row_counts: dict,
    seed_fingerprints: dict,
    current_sv,
) -> None:
    """Record what this consolidation covered. ALWAYS written after the saves —
    a crash before this point replays the same fold/rebuild idempotently; a
    ledger written early could skip files forever."""
    if _scrapes_label() not in dataset_meta:
        dataset_meta[_scrapes_label()] = {}
    dataset_meta[_scrapes_label()]["filenames"] = files_to_concatenate
    dataset_meta[_scrapes_label()]["seed_row_counts"] = seed_row_counts
    dataset_meta[_scrapes_label()]["seed_fingerprints"] = seed_fingerprints
    dataset_meta[_scrapes_label()]["scrape_contract_version"] = current_sv
    _ = data_io.save_json(
        data=dataset_meta, storage_location="recoded", filename="consolidated_enrichment_files.json"
    )


def _normalize_scrape_frame(scrape_df: pd.DataFrame) -> pd.DataFrame:
    """Row-wise normalizations every consolidated scrape frame gets.

    Shared verbatim by the full rebuild and the incremental fold so the two
    paths cannot drift: source_platform backfill (pre-column history is TikTok
    by definition; canonical-era files skip _canonicalize_legacy_scrape's
    rename path, so the fill has to happen here) and the plays_per_day -1
    missing-count sentinel mask (negative is impossible by construction;
    per-file parquets keep the bad values but are re-masked on every load,
    exactly like the legacy-column migration). Both are idempotent.
    """
    backfill_platform = sc.default_platform(sc.load_contract()) or "tiktok"
    if "source_platform" not in scrape_df.columns:
        scrape_df["source_platform"] = pd.NA
    scrape_df["source_platform"] = (
        scrape_df["source_platform"].fillna(backfill_platform).astype("string[pyarrow]")
    )

    if "plays_per_day" in scrape_df.columns:
        scrape_df["plays_per_day"] = scrape_df["plays_per_day"].mask(
            scrape_df["plays_per_day"] < 0, pd.NA
        )
    return scrape_df


def _dedupe_and_resolve_conflicts(scrape_df: pd.DataFrame, verbose: bool = False) -> pd.DataFrame:
    """Per-item dedupe + video_downloaded conflict resolution.

    Shared verbatim by the full rebuild and the incremental fold. The kept row
    per (source_platform, item_id, video_downloaded) key is the newest by
    scrape_ts; storage_link is a deterministic tie-break so equal-timestamp
    duplicates resolve identically regardless of input row order (the fold
    presents rows in a different order than the all-files rebuild). Items
    listed both with and without a downloaded video keep the downloaded row.
    """
    # Sort newest scrape first so a re-scrape supersedes the older row (file
    # order is lexicographic ≈ oldest-first, and keep="first" would otherwise
    # pin the stale row forever).
    if "scrape_ts" in scrape_df.columns:
        sort_cols = ["scrape_ts"] + (
            ["storage_link"] if "storage_link" in scrape_df.columns else []
        )
        scrape_df = scrape_df.sort_values(
            sort_cols, ascending=False, kind="mergesort", na_position="last"
        )
    scrape_df = scrape_df.drop_duplicates(
        subset=["source_platform", "item_id", "video_downloaded"]
    ).copy()
    if verbose:
        logger.info(
            f"    Dropping duplicates based on items and whether the video is downloaded or not: {scrape_df.shape}"
        )

    # identify items with inconsistent video_downloaded status
    items_w_inconsistent_video_download_status = scrape_df["item_id"].value_counts()
    items_w_inconsistent_video_download_status = items_w_inconsistent_video_download_status[
        items_w_inconsistent_video_download_status > 1
    ].index.tolist()

    # use the list generated above to separate items with consistent vs inconsistent video download status
    items_w_consistent_video_download_status = scrape_df[
        ~scrape_df["item_id"].isin(items_w_inconsistent_video_download_status)
    ].copy()
    items_w_inconsistent_video_download_status = scrape_df[
        scrape_df["item_id"].isin(items_w_inconsistent_video_download_status)
    ].copy()
    if verbose:
        logger.info(
            "    Identifying conflicting items in the dataset listed twice - once as video_downloaded and once as not"
        )
        logger.info(
            f"    There are {len(items_w_inconsistent_video_download_status):,} items with such inconsistencies, "
            f"and {len(items_w_consistent_video_download_status):,} that look alright."
        )

    if len(items_w_inconsistent_video_download_status) > 0:
        # for items with inconsistent video download status, only keep the ones where video_downloaded is True
        items_w_inconsistent_video_download_status = items_w_inconsistent_video_download_status[
            items_w_inconsistent_video_download_status["video_downloaded"]
        ].copy()
        if verbose:
            logger.info(
                "    Fixed the inconsistencies by keeping the one of the pairs with video_download=True"
            )
            logger.info(
                f"    This reduces the number of inconsistent items to {len(items_w_inconsistent_video_download_status)}"
            )

        # recombine the two dataframes
        scrape_df = pd.concat(
            [items_w_consistent_video_download_status, items_w_inconsistent_video_download_status]
        )

    return scrape_df


def _fold_scrape_batch(
    dataset_meta: dict,
    files_to_concatenate: list[str],
    new_files: list[str],
    seed_frames: dict[str, pd.DataFrame],
    seed_row_counts: dict,
    seed_fingerprints: dict,
    changed_seed_files: set[str],
    current_sv,
    verbose: bool = False,
):
    """Fold only the new batch files into the previous consolidated frame.

    O(batch) compute, O(corpus) only in blob IO. Correctness argument: the
    previous consolidation kept, per (source_platform, item_id,
    video_downloaded) key, exactly the row the shared transforms select from
    all files; re-running those SAME transforms (:func:`_normalize_scrape_frame`,
    :func:`_dedupe_and_resolve_conflicts`, :func:`_merge_enrichment_seeds`) over
    previous-kept-rows + new-batch-rows therefore selects the same row a full
    rebuild over all files would — the transforms are idempotent per-key
    max-selections. Seed rows are evicted first and re-derived against the
    current seed files, so a real scrape arriving for a seeded key wins exactly
    as in the full rebuild.

    Returns:
        The ``(True, frame, changed_ids)`` result tuple, or ``None`` to decline
        — the caller then runs the unchanged full-rebuild path. Declines when
        the previous frame predates the seed provenance column or the batch
        widens the value-column set (a schema move needs the per-file
        migrations of a full rebuild).
    """
    _t_start = time.perf_counter()
    existing_recoded_fn = f"{_scrapes_label()}_recoded.parquet"
    existing_df = data_io.load_parquet(storage_location="recoded", filename=existing_recoded_fn)
    if existing_df is None or existing_df.empty or "item_id" not in existing_df.columns:
        return None
    if "is_enrichment_seed" not in existing_df.columns:
        logger.info("[CONSOLIDATE] full rebuild: establishing seed provenance column")
        return None
    _t_prev_load = time.perf_counter() - _t_start

    _t_mark = time.perf_counter()
    logger.info(f"Folding {len(new_files)} new scrape file(s) into the previous consolidation...")
    scraper = get_scraper(verbose=False)
    batch_dfs = []
    diff_candidates: set[str] = set()
    for fn in new_files:
        df = data_io.load_parquet(storage_location="scrape", filename=fn)
        df = _coalesce_retired_columns(df)
        df = _canonicalize_legacy_scrape(df, filename=fn, scraper=scraper)
        batch_dfs.append(df)
        if "item_id" in df.columns:
            diff_candidates.update(str(i) for i in df["item_id"].dropna())
        if verbose:
            logger.info(f"{fn} {df.shape}")
    for fn in changed_seed_files:
        seed_df = seed_frames[fn]
        if "item_id" in seed_df.columns:
            diff_candidates.update(str(i) for i in seed_df["item_id"].dropna())

    def _value_col_set(df: pd.DataFrame) -> set[str]:
        return {c for c in df.columns if c != "item_id" and c not in SCRAPE_PROVENANCE_COLS}

    batch_df = None
    if batch_dfs:
        batch_df = _normalize_scrape_frame(pd.concat(batch_dfs, ignore_index=True))
        widened = _value_col_set(batch_df) - _value_col_set(existing_df)
        if widened:
            logger.info(
                f"[CONSOLIDATE] scrape fold declined: batch adds value columns {sorted(widened)}."
            )
            return None
    _t_load = time.perf_counter() - _t_mark

    _t_mark = time.perf_counter()
    existing_real = existing_df[~existing_df["is_enrichment_seed"].fillna(False).astype(bool)]
    if batch_df is not None:
        combined = pd.concat([existing_real, batch_df], ignore_index=True)
    else:
        # Seed-only change: no new batch rows, just re-derive the seed rows.
        combined = existing_real.copy()
    scrape_df = _dedupe_and_resolve_conflicts(combined, verbose=verbose)
    _t_dedupe = time.perf_counter() - _t_mark

    _t_mark = time.perf_counter()
    scrape_df = _merge_enrichment_seeds(scrape_df, seed_frames, verbose=True)
    _t_seeds = time.perf_counter() - _t_mark

    logger.info(
        f"Shape: {scrape_df.shape} | "
        f"Memory usage: {scrape_df.memory_usage(deep=True).sum() / (1024**2):.2f} MB"
    )

    _t_mark = time.perf_counter()
    new_item_ids = _compute_changed_scrape_ids(
        existing_df, scrape_df, verbose=True, candidate_item_ids=diff_candidates
    )
    _t_diff = time.perf_counter() - _t_mark

    logger.info("Saving consolidated scrape data...")
    _t_mark = time.perf_counter()
    _ = data_io.save_parquet(df=scrape_df, storage_location="recoded", filename=existing_recoded_fn)
    _t_save = time.perf_counter() - _t_mark

    _write_scrape_ledger(
        dataset_meta, files_to_concatenate, seed_row_counts, seed_fingerprints, current_sv
    )
    logger.info("...done")
    logger.info(
        f"[CONSOLIDATE][TIMING] scrape FOLD prev_load={_t_prev_load:.1f}s load={_t_load:.1f}s "
        f"concat_dedupe={_t_dedupe:.1f}s seed_merge={_t_seeds:.1f}s diff={_t_diff:.1f}s "
        f"save={_t_save:.1f}s total={time.perf_counter() - _t_start:.1f}s "
        f"new_files={len(new_files)} rows={len(scrape_df):,} changed={len(new_item_ids):,}"
    )
    return True, scrape_df, new_item_ids


def consolidate_and_save_scrape_data(
    force_consolidation: bool = False,
    return_saved_data: bool = True,
    verbose: bool = False,
    incremental: bool = False,
    dry_run: bool = False,
):
    # dry_run: run the full-rebuild reference path but persist NOTHING (no
    # recoded save, no ledger update) — the shadow verifier uses it to build
    # the frame a full rebuild WOULD produce and compare it against the live
    # artifacts. It forces the full path (never the fold).

    top_verbose = True

    # There is no need to look for raw scrape files. Contrary to activity data
    # and annotations, the scrape files are recoded and immediately after the scrape

    if top_verbose:
        logger.info("Checking for new scrape files for consolidation...")

    # check if there are any changes in the relevant folder compared to last time this process was run.
    if data_io.exists(
        storage_location="recoded", filename="consolidated_enrichment_files.json", verbose=verbose
    ):
        dataset_meta = data_io.load_json(
            storage_location="recoded",
            filename="consolidated_enrichment_files.json",
            verbose=verbose,
        )
        if verbose:
            logger.info("Dataset meta loaded")
    else:
        dataset_meta = {_scrapes_label(): {"filenames": []}}

    files_to_concatenate = []
    for fn in data_io.listdir(storage_location="scrape"):
        if fn.startswith(_scrapes_label()) and fn.endswith(".parquet"):
            files_to_concatenate.append(fn)

    # Donated enrichment seeds participate in change detection: a fresh ingest
    # grows a seed file's row count without adding a scrapes_* parquet, and
    # must still trigger consolidation. Row counts alone miss an in-place
    # content edit with the same row count, so the size/mtime fingerprint is
    # compared too (ledgers written before fingerprints existed compare by
    # row count only).
    seed_frames = _load_enrichment_seeds(verbose=verbose)
    seed_row_counts = {fn: len(df) for fn, df in seed_frames.items()}
    seed_fingerprints = {
        fn: data_io.stat(storage_location="recoded", filename=fn) for fn in seed_frames
    }

    latest_filename_list = dataset_meta.get(_scrapes_label(), {}).get("filenames", [])
    latest_seed_row_counts = dataset_meta.get(_scrapes_label(), {}).get("seed_row_counts", {})
    latest_seed_fps = dataset_meta.get(_scrapes_label(), {}).get("seed_fingerprints")
    seeds_unchanged = seed_row_counts == latest_seed_row_counts and (
        latest_seed_fps is None or seed_fingerprints == latest_seed_fps
    )
    # Seed files whose content moved since the last run — their item_ids are
    # changed-id candidates alongside the new batch files' ids.
    changed_seed_files = {
        fn
        for fn in seed_frames
        if seed_row_counts.get(fn) != latest_seed_row_counts.get(fn)
        or (latest_seed_fps is not None and seed_fingerprints.get(fn) != latest_seed_fps.get(fn))
    }
    # A scrape-contract change (new sv_) must rebuild even with no new files:
    # the per-file self-healing migrations (retired-column coalesce, legacy
    # renames) only run inside a rebuild, so skipping would leave the
    # consolidated parquet on the previous contract's column set forever.

    current_sv = scrape_versioning.active_scrape_version()
    latest_sv = dataset_meta.get(_scrapes_label(), {}).get("scrape_contract_version")
    if (
        not force_consolidation
        and set(files_to_concatenate) <= set(latest_filename_list)
        and seeds_unchanged
        and latest_sv == current_sv
    ):
        if top_verbose:
            logger.info("No new scrape files found. No need to consolidate.")
        if return_saved_data:
            if data_io.exists(
                storage_location="recoded", filename=f"{_scrapes_label()}_recoded.parquet"
            ):
                if verbose:
                    logger.info("Returning existing file.")
                return (
                    False,
                    data_io.load_parquet(
                        storage_location="recoded", filename=f"{_scrapes_label()}_recoded.parquet"
                    ),
                    set(),
                )
            if verbose:
                logger.info("No existing consolidated file — returning empty.")
            return False, pd.DataFrame(), set()
        return False, None, set()

    new_files = set(files_to_concatenate) - set(latest_filename_list)

    # ---------------------------------------------------------------
    # Incremental fold: fold ONLY the new batch files into the previous
    # consolidated frame instead of re-reading every scrape parquet. The fold
    # runs the identical normalize/dedupe/seed transforms, so its output equals
    # the full rebuild's; anything it cannot prove equal declines to the full
    # path below (the unchanged reference implementation). Gated off for a
    # force run and for a contract-version bump — both can change values in
    # files already consolidated.
    if (
        incremental
        and not force_consolidation
        and not dry_run
        and latest_sv == current_sv
        and latest_filename_list
        and data_io.exists(
            storage_location="recoded", filename=f"{_scrapes_label()}_recoded.parquet"
        )
    ):
        folded = _fold_scrape_batch(
            dataset_meta=dataset_meta,
            files_to_concatenate=files_to_concatenate,
            new_files=sorted(new_files),
            seed_frames=seed_frames,
            seed_row_counts=seed_row_counts,
            seed_fingerprints=seed_fingerprints,
            changed_seed_files=changed_seed_files,
            current_sv=current_sv,
            verbose=verbose,
        )
        if folded is not None:
            return folded
        logger.info("[CONSOLIDATE] scrape fold declined — taking the full rebuild path.")

    # ---------------------------------------------------------------
    _t_start = time.perf_counter()
    if top_verbose:
        logger.info("Loading scrape files...")
    # Candidate item_ids for the changed-id diff: only rows from new batch
    # files or changed seed files can differ from the previous consolidation,
    # so the signature diff can be restricted to them. A force run or a
    # contract-version bump can change values corpus-wide (per-file migrations
    # re-run), so those keep the full diff.
    diff_candidates: set[str] | None = set()
    if force_consolidation or latest_sv != current_sv:
        diff_candidates = None
    many_scrape_dfs = []
    scraper = get_scraper(verbose=False)
    for fn in files_to_concatenate:
        df = data_io.load_parquet(storage_location="scrape", filename=fn)
        # Fold retired platform-specific columns into their generic successors
        # BEFORE the legacy migration: its rate re-derivation reads the generic
        # count names via the flat [perk] map.
        df = _coalesce_retired_columns(df)
        # Migrate legacy (pre-canonical) parquets to the canonical schema; a no-op
        # for files already saved with canonical names.
        df = _canonicalize_legacy_scrape(df, filename=fn, scraper=scraper)
        many_scrape_dfs.append(df)
        if diff_candidates is not None and fn in new_files and "item_id" in df.columns:
            diff_candidates.update(str(i) for i in df["item_id"].dropna())
        if verbose:
            logger.info(f"{fn} {df.shape}")
    if diff_candidates is not None:
        for fn in changed_seed_files:
            seed_df = seed_frames[fn]
            if "item_id" in seed_df.columns:
                diff_candidates.update(str(i) for i in seed_df["item_id"].dropna())
    _t_load = time.perf_counter() - _t_start

    if top_verbose:
        logger.info(
            f"Consolidating {len(many_scrape_dfs):,} scrape files (dropping duplicate items)..."
        )
    if many_scrape_dfs:
        scrape_df = pd.concat(many_scrape_dfs, ignore_index=True)
    else:
        # No real scrapes yet (e.g. a fresh platform with only donated seeds) —
        # start from an empty frame with the columns downstream steps touch.
        scrape_df = pd.DataFrame(
            {
                "item_id": pd.Series([], dtype="string[pyarrow]"),
                "source_platform": pd.Series([], dtype="string[pyarrow]"),
                "video_downloaded": pd.Series([], dtype="bool[pyarrow]"),
            }
        )

    scrape_df = _normalize_scrape_frame(scrape_df)
    scrape_df = _dedupe_and_resolve_conflicts(scrape_df, verbose=verbose)

    _t_dedupe = time.perf_counter() - _t_start - _t_load

    # ---------------------------------------------------------------
    # Donated enrichment seeds — lowest-precedence fallback rows for
    # items with no real scrape (anti-join on source_platform+item_id).
    # ---------------------------------------------------------------
    scrape_df = _merge_enrichment_seeds(scrape_df, seed_frames, verbose=top_verbose)
    _t_seeds = time.perf_counter() - _t_start - _t_load - _t_dedupe

    memory_per_column = scrape_df.memory_usage(deep=True)
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    if top_verbose:
        logger.info(f"Shape: {scrape_df.shape} | Memory usage: {total_memory_mb:.2f} MB")

    # Count-overflow repair, per-K engagement rates, and plays_per_day are now
    # produced at scrape time (BaseScraper.canonicalize_batch) and back-filled for
    # any legacy parquet by _canonicalize_legacy_scrape at load, so consolidation
    # only needs to concatenate and deduplicate.

    # Compute changed item_ids by diffing against the existing consolidated data.
    # A set-difference on item_id alone only sees brand-new items; a re-scrape
    # that updates the VALUES of an item already consolidated (e.g. an Instagram
    # play_count going from the -1 sentinel to a real count) keeps the same
    # item_id and would be missed, so the study/collection impact analysis would
    # never refresh the studies that item belongs to. Compare the actual row
    # values instead, so any enrichment-value backfill surfaces as a change.
    existing_recoded_fn = f"{_scrapes_label()}_recoded.parquet"
    _t_mark = time.perf_counter()
    existing_df = None
    if data_io.exists(storage_location="recoded", filename=existing_recoded_fn):
        existing_df = data_io.load_parquet(storage_location="recoded", filename=existing_recoded_fn)
    _t_prev_load = time.perf_counter() - _t_mark

    _t_mark = time.perf_counter()
    new_item_ids = _compute_changed_scrape_ids(
        existing_df, scrape_df, verbose=top_verbose, candidate_item_ids=diff_candidates
    )
    _t_diff = time.perf_counter() - _t_mark

    _t_save = 0.0
    if dry_run:
        logger.info("[CONSOLIDATE] dry run — skipping the scrape save and ledger update.")
    else:
        if top_verbose:
            logger.info("Saving consolidated scrape data...")
        _t_mark = time.perf_counter()
        _ = data_io.save_parquet(
            df=scrape_df, storage_location="recoded", filename=existing_recoded_fn
        )
        _t_save = time.perf_counter() - _t_mark

        # update the dataset meta file
        _write_scrape_ledger(
            dataset_meta, files_to_concatenate, seed_row_counts, seed_fingerprints, current_sv
        )

    if top_verbose:
        logger.info("...done")
    logger.info(
        f"[CONSOLIDATE][TIMING] scrape load={_t_load:.1f}s concat_dedupe={_t_dedupe:.1f}s "
        f"seed_merge={_t_seeds:.1f}s prev_load={_t_prev_load:.1f}s diff={_t_diff:.1f}s "
        f"save={_t_save:.1f}s total={time.perf_counter() - _t_start:.1f}s "
        f"files={len(files_to_concatenate)} new_files={len(new_files)} "
        f"rows={len(scrape_df):,} changed={len(new_item_ids):,} "
        f"diff_scope={'full' if diff_candidates is None else 'batch'}"
    )

    return True, scrape_df, new_item_ids
