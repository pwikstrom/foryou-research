"""One-off repair of signed-32-bit-overflowed TikTok counts already persisted.

Repairs the canonical scrapes store (raw counts + recomputed per-play rates) and
any study recoded parquet that carries a wrapped stats_playCount (raw count +
recomputed plays_per_day). The forward fix lives in fyp.scrape.consolidate; this
backfills data written before that fix.
"""
import sys

import pandas as pd

from fyp import fyp_config

fyp_config.initialize()

from fyp import data_io
from fyp.scrape import (
    ENGAGEMENT_PER_PLAY_COLUMNS,
    OVERFLOW_REPAIR_COLUMNS,
    _engagement_per_play,
    repair_overflowed_counts,
)

APPLY = "--apply" in sys.argv


def _negatives(df: pd.DataFrame, col: str = "stats_playCount") -> int:
    if col not in df.columns:
        return 0
    return int((df[col] < -1).fillna(False).sum())


def repair_scrapes_recoded() -> None:
    fn = "scrapes_recoded.parquet"
    if not data_io.exists(storage_location="recoded", filename=fn):
        print(f"[scrapes] {fn} not found; skipping")
        return
    df = data_io.load_parquet(storage_location="recoded", filename=fn)
    before = _negatives(df)
    print(f"[scrapes] {fn}: {before} overflowed stats_playCount before repair (min={df['stats_playCount'].min()})")
    if before == 0:
        return
    df = repair_overflowed_counts(df, verbose=True)
    # Per-play rates were stored as NA for these rows (denominator was negative);
    # recompute them from the recovered play count.
    for new_col, src_col in ENGAGEMENT_PER_PLAY_COLUMNS.items():
        if src_col in df.columns:
            df[new_col] = _engagement_per_play(df[src_col], df["stats_playCount"])
    print(f"[scrapes] after repair: min(stats_playCount)={df['stats_playCount'].min()}, negatives={_negatives(df)}")
    if APPLY:
        data_io.save_parquet(df=df, storage_location="recoded", filename=fn)
        print(f"[scrapes] SAVED {fn}")
    else:
        print("[scrapes] dry-run (pass --apply to write)")


def repair_study(study: str) -> None:
    fn = f"{study}_recoded.parquet"
    if not data_io.exists(storage_location="cache", filename=fn):
        print(f"[study:{study}] {fn} not found; skipping")
        return
    df = data_io.load_parquet(storage_location="cache", filename=fn)
    before = _negatives(df)
    print(f"[study:{study}] {before} overflowed stats_playCount before repair")
    if before == 0:
        return
    df = repair_overflowed_counts(df, verbose=True)
    if "days_since_created" in df.columns:
        days = df["days_since_created"]
        df["plays_per_day"] = (
            df["stats_playCount"] / days.clip(lower=1).mask(df["stats_playCount"].isna() | days.isna(), pd.NA)
        ).astype("double[pyarrow]")
    print(f"[study:{study}] after: min(stats_playCount)={df['stats_playCount'].min()}, "
          f"min(plays_per_day)={df['plays_per_day'].min() if 'plays_per_day' in df.columns else 'n/a'}")
    if APPLY:
        data_io.save_parquet(df=df, storage_location="cache", filename=fn)
        # Drop the cached explorer metadata so it regenerates against fresh data.
        meta_fn = f"{study}_explorer_metadata.json"
        if data_io.exists(storage_location="cache", filename=meta_fn):
            data_io.remove(storage_location="cache", filename=meta_fn)
            print(f"[study:{study}] invalidated {meta_fn}")
        print(f"[study:{study}] SAVED {fn}")
    else:
        print(f"[study:{study}] dry-run (pass --apply to write)")


def scan_all_studies() -> list[str]:
    print("\n=== scanning all *_recoded.parquet study caches for overflow ===")
    affected: list[str] = []
    try:
        files = [f for f in data_io.listdir(storage_location="cache") if f.endswith("_recoded.parquet")]
    except Exception as e:
        print("  could not list cache:", e)
        return affected
    for fn in sorted(files):
        try:
            df = data_io.load_parquet(storage_location="cache", filename=fn, columns=["stats_playCount"])
        except Exception:
            df = data_io.load_parquet(storage_location="cache", filename=fn)
        n = _negatives(df)
        if n:
            study = fn[: -len("_recoded.parquet")]
            affected.append(study)
            print(f"  AFFECTED: {fn} -> {n} overflowed rows (min={df['stats_playCount'].min()})")
    return affected


if __name__ == "__main__":
    repair_scrapes_recoded()
    print()
    affected_studies = scan_all_studies()
    print(f"\nStudy caches to repair: {affected_studies}")
    for study in affected_studies:
        print()
        repair_study(study)
