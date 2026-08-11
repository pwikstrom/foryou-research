"""Pilot an experimental embedding-document variant on one collection.

Embeds every already-annotated video a collection has played under an
alternative document builder (see ``embeddings.DOC_VARIANTS``, e.g. "v2" =
symbols/brands + people + description added, transcript cap reduced), then
optionally replays the binge segmenter on one of the collection's sessions
under BOTH spaces so the boundary differences are directly inspectable.

Vectors are written to a local scratch parquet by default; ``--write-shards``
publishes them into the real shard store under the ``<model>+doc<variant>``
model key instead (the model-scoped store keeps them fully separate from the
live space — no reader sees them unless it asks for that key).

Pilot caveat: distances in the variant space are centred on the PILOT SET's
mean, not a full-corpus mean (which does not exist until the whole corpus is
embedded under the variant). Within-collection comparisons are still
meaningful; absolute distances are not comparable to the live space's.

Usage (local, against prod GCS):
    export FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> GCP_PROJECT_ID=<proj>
    python scripts/adhoc/embed_doc_variant_pilot.py \
        --display-id AIO-00060 --variant v2 --session-suffix __211
"""

import argparse
import os

import numpy as np
import pandas as pd

import fyp.data_io as data_io
from fyp.analysis import embedding_store, embeddings, session_explorer
from fyp.organize_datasets import COLLECTIONS_LABEL

SCRATCH_DEFAULT = os.path.join("tmp", "doc_variant_pilot")






def resolve_collection(display_id: str | None, collection_id: str | None) -> str:
    """Resolve a display id like ``AIO-00060`` to the raw collection UUID."""
    if collection_id:
        return collection_id
    tags = data_io.load_json(
        storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json") or {}
    matches = [cid for cid, ann in tags.items()
               if ann.get("display_collection_id") == display_id]
    if len(matches) != 1:
        raise SystemExit(f"display id {display_id!r}: {len(matches)} matches")
    return matches[0]






def collection_played_ids(cid: str) -> list[str]:
    """All distinct item_ids the collection has played."""
    df = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION,
        filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
        columns=["item_id"],
        filters=[("collection_id", "==", cid), ("activity_type", "==", "play")],
    )
    if df is None or df.empty:
        return []
    return df["item_id"].astype("string").dropna().unique().tolist()






def build_variant_matrix(item_ids: list[str], variant: str,
                         batch: int = 2000) -> tuple[list[str], np.ndarray, str, int]:
    """Build variant documents for ``item_ids`` and embed them.

    Returns:
        ``(kept_ids, matrix, store_model, dim)`` — failed rows (all-zero
        vectors) are dropped.
    """
    _, anno_cols, scrape_cols = embeddings.DOC_VARIANTS[variant]
    id_set = set(item_ids)

    anno = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION,
        filename=embeddings.ANNOTATIONS_FILE, columns=anno_cols)
    anno["item_id"] = anno["item_id"].astype("string")
    anno = anno[anno["item_id"].isin(id_set) & (anno["annotated_ok"] == True)]

    scrape = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION,
        filename=embeddings.SCRAPES_FILE, columns=scrape_cols)
    scrape["item_id"] = scrape["item_id"].astype("string")
    merged = anno.merge(scrape.drop_duplicates("item_id"), on="item_id", how="left")

    docs = embeddings.build_documents(merged, variant=variant)
    print(f"Built {len(docs)} {variant} documents; sample:\n---\n{docs.iloc[0]}\n---")

    backend = embeddings.active_embedding_backend()
    model, dim = backend.model_id(), backend.dim()
    parts = []
    for lo in range(0, len(docs), batch):
        chunk = docs.iloc[lo:lo + batch].tolist()
        print(f"Embedding {lo + len(chunk)}/{len(docs)} with {model}@{dim}...")
        parts.append(backend.embed_texts(chunk))
    matrix = np.vstack(parts) if parts else np.empty((0, dim), dtype=np.float32)

    nonzero = np.abs(matrix).sum(axis=1) > 0
    kept = merged["item_id"].to_numpy()[nonzero].tolist()
    if int((~nonzero).sum()):
        print(f"WARNING: {int((~nonzero).sum())} rows failed embedding (dropped).")
    return kept, matrix[nonzero], embeddings.variant_store_model(model, variant), dim






def replay_session(cid: str, session_suffix: str, id2row: dict,
                   U: np.ndarray, label: str) -> None:
    """Replay the binge segmenter on one session and print its episodes."""
    df = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION,
        filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
        columns=["item_id", "local_timestamp", "play_duration", "session_id"],
        filters=[("collection_id", "==", cid), ("activity_type", "==", "play")],
    )
    df["_ts"] = pd.to_datetime(df["local_timestamp"], errors="coerce")
    sid = cid + session_suffix if session_suffix.startswith("__") else session_suffix
    g = (df[df["session_id"].astype("string") == sid]
         .dropna(subset=["_ts"]).sort_values("_ts"))
    g["item_id"] = g["item_id"].astype("string")
    emb = g[g["item_id"].isin(id2row)]
    seq = [(iid, id2row[iid], ts, du) for iid, ts, du in
           zip(emb["item_id"], emb["_ts"], emb["play_duration"])]

    p = session_explorer.default_params()
    eps = session_explorer.segment_session(
        seq, U, p["cut"], p["mem"], p["min_videos"], p["min_minutes"],
        max_skip=p["max_skip"], flick_seconds=p["flick_seconds"])
    print(f"\n[{label}] session {sid}: {len(g)} plays, {len(seq)} embedded, "
          f"{len(eps)} binge(s)")
    for e in eps:
        print(f"  {e['start_ts']} -> {e['end_ts']}  members={len(e['idx'])}  "
              f"skipped={e['n_skipped']}  ids={e['ids'][:4]}...")






def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--display-id", help="display collection id, e.g. AIO-00060")
    ap.add_argument("--collection", help="raw collection UUID (overrides --display-id)")
    ap.add_argument("--variant", default="v2", choices=sorted(embeddings.DOC_VARIANTS))
    ap.add_argument("--session-suffix", help="session to replay, e.g. __211")
    ap.add_argument("--write-shards", action="store_true",
                    help="publish vectors into the real shard store under the "
                         "variant model key (default: local scratch parquet)")
    ap.add_argument("--scratch", default=SCRATCH_DEFAULT)
    args = ap.parse_args()

    cid = resolve_collection(args.display_id, args.collection)
    ids = collection_played_ids(cid)
    print(f"Collection {cid}: {len(ids)} distinct played items")

    kept, matrix, store_model, dim = build_variant_matrix(ids, args.variant)
    print(f"Embedded {len(kept)} items under store model {store_model!r}")

    if args.write_shards:
        shard = embeddings._write_shard(kept, matrix, model=store_model, dim=dim)
        print(f"Wrote shard {shard}")
    else:
        os.makedirs(args.scratch, exist_ok=True)
        out = os.path.join(args.scratch, f"pilot_{args.variant}_{cid[:8]}.parquet")
        pd.DataFrame({
            "item_id": pd.array(kept, dtype="string[pyarrow]"),
            "embedding": [v.tobytes() for v in matrix.astype(np.float16)],
            "model": store_model, "dim": dim,
        }).to_parquet(out)
        print(f"Wrote {out}")

    if not args.session_suffix:
        return

    # Variant space: directionalise on the PILOT mean (see module docstring).
    Uv = matrix.astype(np.float32).copy()
    session_explorer._directionalise(Uv, Uv.mean(axis=0))
    replay_session(cid, args.session_suffix,
                   {iid: i for i, iid in enumerate(kept)}, Uv,
                   f"{args.variant} (pilot mean)")

    # Live space, for the side-by-side.
    live_model = embeddings.active_embedding_backend().model_id()
    mean, _, _ = embedding_store.get_corpus_mean(live_model)
    index = embedding_store.load_index(live_model)
    id2row, U1 = session_explorer.load_directional_block(live_model, ids, mean, index=index)
    replay_session(cid, args.session_suffix, id2row, U1, "v1 (live, corpus mean)")




if __name__ == "__main__":
    main()
