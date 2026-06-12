"""Two profiling passes that set the study's empirical thresholds.

1. Collection inventory: per-collection play volume, day-span, activity-type mix
   (to flag observe-only baselines) and embedding coverage. Their distributions
   set the eligibility floors for the donor population (design note decision 1).
2. Unembedded-metadata ceiling: how much of the *unembedded* viewing has usable
   scrape text (caption / hashtags / music), which decides whether a fallback
   embedding is feasible at all (decision 4).
"""

import os
import sys

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import data_access

REC = data_access.RECODED_DIR
OUT = os.path.join(os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp")),
                   "collection_inventory.parquet")




def inventory(emb_ids: set) -> pd.DataFrame:
    """Build the per-collection inventory table."""
    t = pq.read_table(
        os.path.join(REC, data_access.COLLECTIONS_FILE),
        columns=["collection_id", "item_id", "local_timestamp", "activity_type"],
    ).to_pandas()
    t["_ts"] = pd.to_datetime(t["local_timestamp"], errors="coerce")
    t["_is_play"] = t["activity_type"] == "play"
    t["_is_obs"] = t["activity_type"] == "observe"
    t["_emb"] = t["item_id"].isin(emb_ids)

    rows = []
    for cid, g in t.groupby("collection_id"):
        play = g[g["_is_play"]]
        n_play = int(len(play))
        days = play["_ts"].dt.normalize()
        emb_play = int(play["_emb"].sum())
        rows.append({
            "collection_id": cid,
            "n_total": int(len(g)),
            "n_play": n_play,
            "n_observe": int(g["_is_obs"].sum()),
            "play_frac": round(n_play / max(len(g), 1), 3),
            "n_days": int(days.nunique()) if n_play else 0,
            "span_days": int((days.max() - days.min()).days) if n_play and days.notna().any() else 0,
            "n_emb_play": emb_play,
            "coverage": round(emb_play / n_play, 3) if n_play else 0.0,
        })
    return pd.DataFrame(rows).sort_values("n_emb_play", ascending=False)




def describe_inventory(inv: pd.DataFrame) -> None:
    """Print the distributions that set the eligibility floors."""
    print(f"\nTotal collections: {len(inv)}")
    obs_only = inv[(inv["n_play"] == 0) | (inv["play_frac"] < 0.2)]
    print(f"Observe-only / low-play (play_frac<0.2 or n_play==0): {len(obs_only)}")
    donors = inv[(inv["n_play"] > 0) & (inv["play_frac"] >= 0.2)]
    print(f"Candidate donor histories: {len(donors)}")

    def pcts(col):
        q = np.percentile(donors[col], [10, 25, 50, 75, 90])
        return "  ".join(f"p{p}={v:,.0f}" for p, v in zip([10, 25, 50, 75, 90], q))
    print("\nAmong candidate donors:")
    for col in ["n_play", "n_days", "span_days", "n_emb_play", "coverage"]:
        print(f"  {col:12} {pcts(col)}")

    print("\nDonors passing candidate floors:")
    for floor in [
        {"n_play": 5000, "n_days": 30, "n_emb_play": 1000},
        {"n_play": 10000, "n_days": 60, "n_emb_play": 2000},
        {"n_play": 20000, "n_days": 90, "n_emb_play": 4000},
    ]:
        m = np.ones(len(donors), dtype=bool)
        for k, v in floor.items():
            m &= donors[k] >= v
        hi = m & (donors["coverage"] >= 0.25)
        print(f"  {floor}: eligible={int(m.sum())}  of which coverage>=0.25: {int(hi.sum())}")




def metadata_ceiling(emb_ids: set) -> None:
    """Measure how much unembedded viewing has usable fallback scrape text."""
    print("\n=== Unembedded-metadata ceiling ===")
    plays = pq.read_table(
        os.path.join(REC, data_access.COLLECTIONS_FILE),
        columns=["item_id"], filters=[("activity_type", "==", "play")],
    ).column("item_id").to_pylist()
    s_all = pd.Series(plays, dtype="string")
    is_emb = s_all.isin(emb_ids)
    unemb_plays = int((~is_emb).sum())
    unemb_items = set(s_all[~is_emb].dropna().unique())
    print(f"Play rows: {len(s_all):,}  unembedded: {unemb_plays:,} ({unemb_plays/len(s_all):.1%})")
    print(f"Distinct unembedded videos: {len(unemb_items):,}")

    # desc_hashtags is an Arrow list column (pandas can't map it directly), so
    # reduce it to a boolean "has hashtags" with pyarrow.compute before the frame.
    tbl = pq.read_table(
        os.path.join(REC, "scrapes_recoded.parquet"),
        columns=["item_id", "desc_raw", "desc_hashtags", "music_title"],
    )
    has_hash = pc.fill_null(pc.greater(pc.list_value_length(tbl["desc_hashtags"]), 0), False)
    sc = pd.DataFrame({
        "item_id": pd.Series(tbl["item_id"].to_pylist(), dtype="string"),
        "desc_raw": pd.Series(tbl["desc_raw"].to_pylist(), dtype="string"),
        "music_title": pd.Series(tbl["music_title"].to_pylist(), dtype="string"),
        "has_hashtags": has_hash.to_pandas(),
    })
    sc = sc.drop_duplicates("item_id")
    sc_un = sc[sc["item_id"].isin(unemb_items)]

    def nonempty(col):
        if col == "desc_hashtags":
            return int(sc_un["has_hashtags"].sum())
        v = sc_un[col].astype("string").fillna("")
        return int((v.str.len() > 0).sum())

    n_scraped = len(sc_un)
    print(f"Unembedded videos with ANY scrape record: {n_scraped:,} ({n_scraped/max(len(unemb_items),1):.1%} of distinct unembedded)")
    if n_scraped:
        print(f"  with non-empty desc_raw (caption): {nonempty('desc_raw'):,}")
        print(f"  with non-empty desc_hashtags:      {nonempty('desc_hashtags'):,}")
        print(f"  with non-empty music_title:        {nonempty('music_title'):,}")
    # Recoverable share of unembedded PLAY VOLUME (not just distinct items).
    recoverable_items = set(sc_un[sc_un["desc_raw"].astype("string").fillna("").str.len() > 0]["item_id"])
    recov_play = int(s_all[~is_emb].isin(recoverable_items).sum())
    print(f"Unembedded PLAY VOLUME recoverable via caption: {recov_play:,} "
          f"({recov_play/max(unemb_plays,1):.1%} of unembedded plays)")




def main() -> None:
    """Run both passes."""
    emb_ids = data_access.embedded_id_set()
    print(f"Embedded videos: {len(emb_ids):,}")
    inv = inventory(emb_ids)
    inv.to_parquet(OUT)
    print(f"Wrote inventory -> {OUT}")
    describe_inventory(inv)
    metadata_ceiling(emb_ids)




if __name__ == "__main__":
    main()
