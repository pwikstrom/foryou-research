"""Reproduce the established within-session begin->end finding from full data.

Standalone harness to validate the assembly recipe before wiring it into the
Cloud Task worker. Run: python tests/repro_session_profile.py paper_three
"""

import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import fyp.data_io as data_io
from fyp import niche_detection, session_profile
from fyp.fyp_config import fyp_cf, initialize
from fyp.studies import init_study_defs

VIEWING_ACTIVITY_TYPES = ("play", "observe")

# Scrape carries the metric columns AND the description/music text fields that
# niche_detection.TEXT_FIELDS expects (these are NOT in the annotation table).
SCRAPE_COLS = ["item_id", "video_duration", "author_id", "stats_playCount",
               "desc_hashtags", "desc_not_hashtags", "music_title"]
ANNOT_TEXT_COLS = [
    "video_story", "main_activity", "objects", "text_overlays",
    "symbols_and_brands", "transcript_no_repetitions",
]
ANNOT_FEATURE_COLS = [
    "political_score", "sensitivity_score", "main_gender",
    "advertising", "aigc", "trend",
]


def _load_annotations() -> pd.DataFrame:
    ann = data_io.load_parquet(
        storage_location="recoded", filename="machine_annotations_recoded.parquet",
    )
    if "annotated_ok" in ann.columns:
        ann = ann[ann["annotated_ok"] == True].copy()  # noqa: E712
    keep_ann = ["item_id"] + [c for c in ANNOT_TEXT_COLS + ANNOT_FEATURE_COLS
                              if c in ann.columns]
    return ann[keep_ann].drop_duplicates(subset="item_id")


def _global_niche_map(ann: pd.DataFrame) -> pd.Series:
    """Fit one niche vocabulary on the full annotated corpus; map item_id->niche."""
    t1 = time.perf_counter()
    labels, _, _ = niche_detection.detect_niches(ann, n_niches=150)
    out = pd.Series(labels.values, index=ann["item_id"].values)
    print(f"  GLOBAL niches: {ann['item_id'].nunique():,} videos, "
          f"{out.nunique()} niches ({time.perf_counter()-t1:.1f}s)")
    return out


def assemble(study_name: str, ann: pd.DataFrame, global_niche: pd.Series | None) -> pd.DataFrame:
    cols = [str(c).strip() for c in
            fyp_cf["study_defs"][study_name].get("SELECTED_COLLECTIONS", [])]
    print(f"  {study_name}: {len(cols)} collections")

    t0 = time.perf_counter()
    plays = data_io.load_parquet_selective(
        storage_location="recoded", filename="collections_recoded.parquet",
        columns=["collection_id", "item_id", "activity_type",
                 "play_duration", "utc_timestamp", "session_id"],
    )
    plays = plays[plays["collection_id"].astype(str).str.strip().isin(set(cols))].copy()
    plays = plays[plays["activity_type"].isin(VIEWING_ACTIVITY_TYPES)].copy()
    print(f"  loaded+filtered plays: {len(plays):,} rows ({time.perf_counter()-t0:.1f}s)")

    scr = data_io.load_parquet_selective(
        storage_location="recoded", filename="scrapes_recoded.parquet",
        columns=SCRAPE_COLS,
    ).drop_duplicates(subset="item_id")
    annotated_items = set(ann["item_id"])

    df = plays.merge(scr, on="item_id", how="left").merge(ann, on="item_id", how="left")

    df["video_duration"] = pd.to_numeric(df["video_duration"], errors="coerce")
    df["play_duration"] = pd.to_numeric(df["play_duration"], errors="coerce")
    df["dwell"] = df["play_duration"]
    df["completion"] = df["play_duration"] / df["video_duration"]
    df["log_playcount"] = np.log1p(pd.to_numeric(df["stats_playCount"], errors="coerce"))

    df["utc_timestamp"] = pd.to_datetime(df["utc_timestamp"])
    df = df.sort_values(["collection_id", "utc_timestamp"], kind="mergesort")
    df["feed_position"] = df.groupby("collection_id", sort=False).cumcount()

    annot_mask = df["item_id"].isin(annotated_items)
    df_annot = df[annot_mask].copy()
    if global_niche is not None:
        df["niche"] = df["item_id"].map(global_niche)
    else:
        t1 = time.perf_counter()
        niche_labels, _, _ = niche_detection.detect_niches(df_annot, n_niches=150)
        df_annot["niche"] = niche_labels.values
        niche_by_item = df_annot.drop_duplicates("item_id").set_index("item_id")["niche"]
        df["niche"] = df["item_id"].map(niche_by_item)
        print(f"  per-study niches: {df['niche'].notna().sum():,} annotated rows, "
              f"{df['niche'].nunique()} niches ({time.perf_counter()-t1:.1f}s)")
    return df


def main(study_name: str, use_global: bool) -> None:
    initialize()
    init_study_defs()
    ann = _load_annotations()
    global_niche = _global_niche_map(ann) if use_global else None
    df = assemble(study_name, ann, global_niche)
    metrics = session_profile.build_session_metrics(df)
    data_io.save_parquet(df=metrics, storage_location="cache",
                         filename=f"_repro_{study_name}_metrics.parquet")
    print(f"\n  sessions in band: {len(metrics):,}, participants: "
          f"{metrics['collection_id'].nunique()}")
    profile = session_profile.compute_profile(metrics)

    agg = {a["feature"]: a for a in profile["aggregate"]}
    print("\n  === SANITY CHECK (expected: completion -0.124, dwell -3.2, "
          "entropy +0.044, 97% up; 25% sessions narrow) ===")
    for f in ["completion", "dwell", "entropy", "top_share"]:
        if f in agg:
            a = agg[f]
            print(f"  {f:12s} early={a['early']:.3f} late={a['late']:.3f} "
                  f"delta={a['delta']:+.4f} pct_up={a['pct_up']:.2%} "
                  f"fdr={a.get('fdr')}")
    sd = profile["session_distributions"]
    print(f"\n  pct_narrowing={sd['pct_narrowing']:.2%} "
          f"pct_engagement_rising={sd['pct_engagement_rising']:.2%}")
    pv = profile["participant_variation"]
    print(f"  n_participants={pv['n_participants']} "
          f"n_narrowing={pv['n_narrowing']} "
          f"n_engagement_rising={pv['n_engagement_rising']}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    study = args[0] if args else "paper_three"
    main(study, use_global="--global" in sys.argv)
