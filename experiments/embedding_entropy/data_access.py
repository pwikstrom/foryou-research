"""Local-parquet data access for the embedding-entropy experiment.

Deliberately standalone: reads the ``recoded`` parquet files straight off the
local disk with PyArrow (no Flask app, no ``data_io`` GCS abstraction) so the
experiment can run as a plain script. The only coupling to the package is
:func:`fyp.embeddings.decode_embeddings`, reused so the float16 byte layout
stays in one place.
"""

import glob
import os

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from fyp.embeddings import EMBED_DIM, decode_embeddings

# Local store layout. RECODED_DIR is resolved from the config default; override
# with the FYP_RECODED_DIR env var if the data lives elsewhere.
RECODED_DIR = os.environ.get("FYP_RECODED_DIR", "/Users/<user>/fyp_local/recoded")
SHARD_GLOB = "video_embeddings__*.parquet"
COLLECTIONS_FILE = "collections_recoded.parquet"
MAP_FILE = "video_map.parquet"

# Cache the streamed corpus mean so repeat runs skip the full decode pass.
CACHE_DIR = os.environ.get("FYP_EXPERIMENT_TMP", "/Users/<user>/GitHub_main/fyp_main_v02/tmp")
CORPUS_MEAN_CACHE = os.path.join(CACHE_DIR, f"embedding_corpus_mean_{EMBED_DIM}.npy")




def _shard_paths() -> list[str]:
    """Return absolute paths to every embedding shard in the store."""
    return sorted(glob.glob(os.path.join(RECODED_DIR, SHARD_GLOB)))




def corpus_mean(force: bool = False) -> np.ndarray:
    """Return the global mean embedding, streaming the shards once and caching.

    The corpus mean removes ``gemini-embedding-001``'s anisotropic common
    offset before any angle is measured. It is computed over **every** stored
    embedding (not just the experiment's collections) so the geometry matches
    the full Semantic Space.

    Args:
        force: Recompute and overwrite the cache even if it exists.

    Returns:
        A ``(EMBED_DIM,)`` float64 mean vector.
    """
    if not force and os.path.exists(CORPUS_MEAN_CACHE):
        return np.load(CORPUS_MEAN_CACHE)

    total = np.zeros(EMBED_DIM, dtype=np.float64)
    count = 0
    for path in _shard_paths():
        table = pq.read_table(path, columns=["embedding"])
        vecs = decode_embeddings(table.column("embedding").to_pylist())
        total += vecs.astype(np.float64).sum(axis=0)
        count += vecs.shape[0]
    if count == 0:
        raise RuntimeError("No embeddings found; cannot compute corpus mean.")
    mean = total / count
    os.makedirs(CACHE_DIR, exist_ok=True)
    np.save(CORPUS_MEAN_CACHE, mean)
    return mean




def embedded_id_set() -> set[str]:
    """Return the set of all embedded item_ids (item_id column only, no decode).

    Cheap enough to call eagerly: lets the runner pick the densest window by
    *embedded*-play count and report coverage without touching any vectors.

    Returns:
        The set of item_ids present anywhere in the embedding store.
    """
    ids: set[str] = set()
    for path in _shard_paths():
        ids.update(pq.read_table(path, columns=["item_id"]).column("item_id").to_pylist())
    return ids




def load_embeddings_for(item_ids: set[str]) -> dict[str, np.ndarray]:
    """Load raw embeddings for a specific set of item_ids.

    Streams every shard but materialises only the rows whose ``item_id`` is in
    ``item_ids`` (filter pushdown avoids decoding the rest), so the working set
    stays proportional to the experiment's collections rather than the whole
    260k-video store.

    Args:
        item_ids: The item_ids to fetch vectors for.

    Returns:
        A dict mapping ``item_id`` to its ``(EMBED_DIM,)`` float32 vector.
    """
    wanted = list(item_ids)
    out: dict[str, np.ndarray] = {}
    if not wanted:
        return out
    for path in _shard_paths():
        table = pq.read_table(
            path, columns=["item_id", "embedding"],
            filters=[("item_id", "in", wanted)],
        )
        if table.num_rows == 0:
            continue
        ids = table.column("item_id").to_pylist()
        vecs = decode_embeddings(table.column("embedding").to_pylist())
        for i, iid in enumerate(ids):
            out[iid] = vecs[i]
    return out




def load_plays(collection_ids: list[str]) -> "pq.lib.Table":
    """Load the ``play`` rows for the given collections.

    Args:
        collection_ids: Collections to load.

    Returns:
        A PyArrow table with ``collection_id``/``item_id``/``local_timestamp``/
        ``play_duration``/``session_id`` for ``activity_type == 'play'`` rows.
    """
    return pq.read_table(
        os.path.join(RECODED_DIR, COLLECTIONS_FILE),
        columns=["collection_id", "item_id", "local_timestamp", "play_duration", "session_id"],
        filters=[("collection_id", "in", collection_ids), ("activity_type", "==", "play")],
    )




def load_video_features() -> pd.DataFrame:
    """Load per-video content features for episode characterisation.

    Joins the denormalised map fields (niche, category, annotation scalars) with
    the scrape ``author_uniqueId`` (kept out of the embeddings, so it is an
    independent signal for the same-/cross-author question). Returned for the
    whole embedded corpus once; the caller indexes into it per episode.

    Returns:
        A DataFrame indexed by ``item_id`` with ``niche_name``, ``category``,
        ``political_score``, ``sensitivity_score``, ``advertising``, ``aigc``,
        and ``author``.
    """
    # Build via to_pylist rather than to_pandas: the fyp environment defaults
    # pandas to a pyarrow dtype backend, under which to_pandas chokes on any
    # list-typed column in the file.
    mp = pq.read_table(
        os.path.join(RECODED_DIR, MAP_FILE),
        columns=["item_id", "niche_name", "category", "political_score",
                 "sensitivity_score", "advertising", "aigc"],
    )
    feat = pd.DataFrame({
        "item_id": pd.Series(mp["item_id"].to_pylist(), dtype="string"),
        "niche_name": mp["niche_name"].to_pylist(),
        "category": mp["category"].to_pylist(),
        "political_score": pd.to_numeric(pd.Series(mp["political_score"].to_pylist()), errors="coerce"),
        "sensitivity_score": pd.to_numeric(pd.Series(mp["sensitivity_score"].to_pylist()), errors="coerce"),
        "advertising": mp["advertising"].to_pylist(),
        "aigc": mp["aigc"].to_pylist(),
    })
    at = pq.read_table(
        os.path.join(RECODED_DIR, "scrapes_recoded.parquet"),
        columns=["item_id", "author_uniqueId"],
    )
    auth = pd.DataFrame({
        "item_id": pd.Series(at["item_id"].to_pylist(), dtype="string"),
        "author": at["author_uniqueId"].to_pylist(),
    }).drop_duplicates("item_id")
    return feat.merge(auth, on="item_id", how="left").set_index("item_id")




def load_video_labels(item_ids: set[str]) -> dict[str, dict]:
    """Load human-readable niche name + story for flagged videos.

    Used only to make the lowest-entropy windows legible in the report; the
    metrics never see these labels.

    Args:
        item_ids: The item_ids to describe.

    Returns:
        A dict mapping ``item_id`` to ``{"niche_name", "category", "story"}``.
    """
    wanted = list(item_ids)
    if not wanted:
        return {}
    path = os.path.join(RECODED_DIR, MAP_FILE)
    table = pq.read_table(
        path, columns=["item_id", "niche_name", "category", "story"],
        filters=[("item_id", "in", wanted)],
    )
    df = table.to_pandas()
    out: dict[str, dict] = {}
    for _, row in df.iterrows():
        out[str(row["item_id"])] = {
            "niche_name": row.get("niche_name"),
            "category": row.get("category"),
            "story": row.get("story"),
        }
    return out
