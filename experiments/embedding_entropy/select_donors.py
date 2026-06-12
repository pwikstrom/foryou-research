"""Rank collections as donors for the embedding-entropy experiment.

Picks collections that have both enough ``play`` volume and enough embedding
coverage that a 60-min window will usually contain several distinct embedded
videos. One pass over the play rows; prints a ranked table and writes the
chosen ids to ``tmp/`` for the runner to consume via ``--collections``.
"""

import os
import sys

import numpy as np
import pyarrow.parquet as pq

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import data_access

# Donor thresholds: enough plays that a dense 21-day span exists, and enough
# coverage that hours are measurable. MIN_EMB_PLAYS is the harder gate.
MIN_PLAYS = 20000
MIN_COVERAGE = 0.12
MIN_EMB_PLAYS = 4000
TOP_N = 20




def main() -> None:
    """Score every collection and print/write the donor shortlist."""
    print("Loading embedded id set...")
    emb = data_access.embedded_id_set()
    print(f"  {len(emb):,} embedded videos")

    print("Streaming play rows...")
    table = pq.read_table(
        os.path.join(data_access.RECODED_DIR, data_access.COLLECTIONS_FILE),
        columns=["collection_id", "item_id"],
        filters=[("activity_type", "==", "play")],
    )
    cids = np.asarray(table.column("collection_id").to_pylist())
    items = table.column("item_id").to_pylist()
    is_emb = np.fromiter((i in emb for i in items), dtype=bool, count=len(items))

    # Aggregate per collection.
    order = np.argsort(cids, kind="stable")
    cids_s = cids[order]
    emb_s = is_emb[order]
    uniq, starts = np.unique(cids_s, return_index=True)
    bounds = list(starts) + [len(cids_s)]
    stats = []
    for i, cid in enumerate(uniq):
        seg = emb_s[bounds[i]:bounds[i + 1]]
        plays = int(seg.size)
        emb_plays = int(seg.sum())
        cov = emb_plays / plays if plays else 0.0
        stats.append((cid, plays, emb_plays, cov))

    chosen = [
        s for s in stats
        if s[1] >= MIN_PLAYS and s[3] >= MIN_COVERAGE and s[2] >= MIN_EMB_PLAYS
    ]
    # Rank by embedded-play volume (a coverage-weighted measure of how much
    # measurable signal the donor contributes).
    chosen.sort(key=lambda s: -s[2])
    chosen = chosen[:TOP_N]

    print(f"\n{len(chosen)} donors (plays>={MIN_PLAYS}, cov>={MIN_COVERAGE}, emb_plays>={MIN_EMB_PLAYS}):")
    print(f"  {'collection_id':36}  {'plays':>8}  {'emb_plays':>9}  {'coverage':>8}")
    for cid, plays, emb_plays, cov in chosen:
        print(f"  {cid:36}  {plays:8d}  {emb_plays:9d}  {cov:8.2f}")

    out = os.path.join(data_access.CACHE_DIR, "embedding_entropy_donors.txt")
    os.makedirs(data_access.CACHE_DIR, exist_ok=True)
    with open(out, "w") as fh:
        fh.write(" ".join(cid for cid, *_ in chosen))
    print(f"\nWrote donor ids -> {out}")




if __name__ == "__main__":
    main()
