"""Dense semantic embeddings for annotated videos.

Builds one labelled text document per annotated video from the machine
annotation fields (``recoded/machine_annotations_recoded.parquet``) plus a few
scrape metadata fields (``recoded/scrapes_recoded.parquet``), embeds it with
the active embedding backend (see :mod:`fyp.analysis.embedding_backends` —
Gemini by default, or a local Qwen3-Embedding model), and persists the vectors
as append-only sharded parquet files in the ``recoded`` store.

Design notes:
    * Vectors are stored **raw** (not mean-centred). Mean-centring and
      L2-normalisation depend on the whole corpus and belong to the downstream
      analysis/clustering step, so keeping the store raw lets it stay
      append-only and stable as new videos are annotated over time.
    * Storage is **sharded**: each embedding run writes one new shard
      (``video_embeddings__<uuid>.parquet``) holding only its newly-embedded
      rows. This keeps every incremental run O(new items) instead of rewriting
      the full store, and makes the backfill restartable.
    * Each vector is stored as float16 raw bytes in a binary column
      (``dim * 2`` bytes/row) — about half the size of float32 lists and a
      clean round-trip through :mod:`fyp.data_io`.
    * The store is **model-scoped**: every row carries the ``model`` and
      ``dim`` that produced it, and all readers filter to one model. Switching
      the embedding backend therefore re-embeds the corpus under the new model
      into new shards while the old model's shards stay untouched — switching
      back costs nothing.
"""

import uuid

import numpy as np
import pandas as pd
import pyarrow as pa

import fyp.data_io as data_io
from fyp.analysis.embedding_backends import active_backend_name, get_backend
from fyp.core.utils import DEMO_ITEM_ID_PREFIX
from fyp.logging_setup import get_logger

logger = get_logger(__name__)




# Store layout in the "recoded" named location.
STORE_LOCATION = "recoded"
SHARD_PREFIX = "video_embeddings__"
SHARD_SUFFIX = ".parquet"
ANNOTATIONS_FILE = "machine_annotations_recoded.parquet"
SCRAPES_FILE = "scrapes_recoded.parquet"

# Annotation columns pulled into each document, and the scrape columns merged
# in by item_id. Three classes of field are deliberately excluded: author
# identity (makes videos cluster by creator), the boilerplate CRA/IA/FA fields
# (heavy LLM boilerplate that fabricates clusters), and content_category — an
# annotation-assigned label that is kept out so it stays an independent
# yardstick for validating the map and a clean colour overlay, rather than a
# circular input the embedding is partly built from.
ANNO_DOC_COLS = [
    "item_id", "annotated_ok", "video_story", "transcript_no_repetitions",
    "objects", "text_overlays", "main_activity",
    "type_of_story", "notable_sounds", "background_music",
]
SCRAPE_DOC_COLS = ["item_id", "music_title", "desc_hashtags"]

# Per-field character caps keep each document well under the model's
# per-instance token limit and stop any single long transcript / overlay list
# from dominating the vector.
_CAP_STORY = 1200
_CAP_SPOKEN = 800
_CAP_OVERLAYS = 400
_CAP_OBJECTS = 300
_CAP_HASHTAGS = 200
_CAP_SHORT = 120






def active_embedding_backend():
    """Return the admin-selected embedding backend instance.

    Returns:
        The active :class:`fyp.analysis.embedding_backends.EmbeddingBackend`.
    """
    return get_backend(active_backend_name())






def _text(value, cap: int) -> str:
    """Flatten a scalar annotation cell to a capped plain string."""
    if value is None or value is pd.NA:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)[:cap]






def _list(value, cap: int) -> str:
    """Flatten a list/array annotation cell to a capped space-joined string."""
    if value is None or not hasattr(value, "__len__") or isinstance(value, str):
        return _text(value, cap)
    return " ".join(str(v) for v in value if v is not None)[:cap]






def build_document(row: pd.Series) -> str:
    """Build one labelled embedding document from a merged annotation+scrape row.

    A labelled template (``Story: … / Spoken: … / Objects: …``) is used rather
    than the bag-of-words "repeat text to upweight" trick, so the dense model
    sees field structure and gracefully handles missing modalities.

    Args:
        row: A row carrying :data:`ANNO_DOC_COLS` and :data:`SCRAPE_DOC_COLS`.

    Returns:
        The assembled document string.
    """
    story = _text(row.get("video_story"), _CAP_STORY)
    activity = f'{_text(row.get("main_activity"), 40)}; {_text(row.get("type_of_story"), 40)}'
    spoken = _text(row.get("transcript_no_repetitions"), _CAP_SPOKEN) or "(none)"
    overlays = _list(row.get("text_overlays"), _CAP_OVERLAYS) or "(none)"
    objects = _list(row.get("objects"), _CAP_OBJECTS)
    sounds = (
        f'{_list(row.get("notable_sounds"), _CAP_SHORT)}; '
        f'{_list(row.get("background_music"), 60)}; '
        f'{_text(row.get("music_title"), 80)}'
    )
    hashtags = _list(row.get("desc_hashtags"), _CAP_HASHTAGS)
    return (
        f"Story: {story}\nActivity: {activity}\n"
        f"Spoken: {spoken}\nOn-screen text: {overlays}\nObjects: {objects}\n"
        f"Sounds/music: {sounds}\nHashtags: {hashtags}"
    )






def build_documents(df: pd.DataFrame) -> pd.Series:
    """Build labelled embedding documents for every row of ``df``.

    Args:
        df: Merged annotation+scrape frame containing the document columns.

    Returns:
        A string Series aligned to ``df.index``.
    """
    return df.apply(build_document, axis=1)






def _encode_matrix(matrix: np.ndarray) -> list[bytes]:
    """Encode an ``(n, dim)`` float matrix to a list of float16 byte strings."""
    m16 = matrix.astype(np.float16)
    return [row.tobytes() for row in m16]






def decode_embeddings(byte_values, dim: int | None = None) -> np.ndarray:
    """Decode a column of float16 byte strings back to an ``(n, dim)`` float32 array.

    Args:
        byte_values: Iterable of ``bytes`` (the stored ``embedding`` column).
        dim: Vector dimensionality (kept for documentation/validation — the
            float16 rows carry their own length; None accepts whatever the
            bytes decode to).

    Returns:
        A float32 array of shape ``(n, dim)``.
    """
    rows = [np.frombuffer(b, dtype=np.float16) for b in byte_values]
    return np.vstack(rows).astype(np.float32)






def _list_shards() -> list[str]:
    """Return the filenames of all embedding shards in the store."""
    return [
        fn for fn in data_io.listdir(storage_location=STORE_LOCATION)
        if fn.startswith(SHARD_PREFIX) and fn.endswith(SHARD_SUFFIX)
    ]






def _model_mask(df: pd.DataFrame, model: str) -> pd.Series:
    """Boolean mask of the shard rows produced by ``model``.

    Every shard written since the store's introduction stamps ``model``
    per-row; a hypothetical shard without the column is attributed to the
    original Gemini model rather than dropped.

    Args:
        df: A shard frame (or its column subset) to filter.
        model: The embedding model id to match.

    Returns:
        A boolean Series aligned to ``df``.
    """
    if "model" not in df.columns:
        return pd.Series(model == "gemini-embedding-001", index=df.index)
    return df["model"].astype("string") == model






def embedded_item_ids(model: str | None = None) -> set[str]:
    """Return the item_ids already embedded **by the given model**.

    Args:
        model: Embedding model id to scope to; None = the active backend's.
            Scoping means a backend switch sees an empty store and re-embeds
            the corpus under the new model (old shards are kept).

    Returns:
        The set of item_ids with a stored vector from ``model``.
    """
    if model is None:
        model = active_embedding_backend().model_id()
    ids: set[str] = set()
    for shard in _list_shards():
        df = data_io.load_parquet_selective(
            storage_location=STORE_LOCATION, filename=shard, columns=["item_id", "model"],
        )
        if df is None or len(df) == 0:
            continue
        df = df[_model_mask(df, model)]
        if len(df) > 0:
            ids.update(df["item_id"].astype("string").tolist())
    return ids






def annotated_ok_item_ids() -> list[str]:
    """Return the item_ids of all successfully-annotated videos.

    An install where annotation has never run has no annotations parquet yet —
    that is an empty backlog, not an error.
    """
    if not data_io.exists(storage_location=STORE_LOCATION, filename=ANNOTATIONS_FILE):
        return []
    df = data_io.load_parquet_selective(
        storage_location=STORE_LOCATION, filename=ANNOTATIONS_FILE,
        columns=["item_id", "annotated_ok"],
    )
    if df is None or "item_id" not in df.columns or "annotated_ok" not in df.columns:
        return []
    ok = df[df["annotated_ok"] == True]
    ids = ok["item_id"].astype("string")
    # Synthetic demo items never enter the embedding store: the semantic map
    # and niche clustering are corpus-global, and fabricated captions would
    # perturb the real corpus's structure.
    ids = ids[~ids.str.startswith(DEMO_ITEM_ID_PREFIX, na=False)]
    return ids.tolist()






def _write_shard(item_ids: list[str], matrix: np.ndarray, model: str, dim: int) -> str:
    """Persist one batch of embeddings as a new shard; return its filename.

    The frame is built with explicit Arrow dtypes so :func:`data_io.save_parquet`
    takes its all-ArrowDtype fast path (the float16 vectors live in a binary
    column).

    Args:
        item_ids: Item ids aligned to ``matrix`` rows.
        matrix: The ``(n, dim)`` embedding matrix.
        model: Embedding model id stamped per-row (scopes the store).
        dim: Vector dimensionality stamped per-row.

    Returns:
        The new shard's filename.
    """
    created = pd.Timestamp.now(tz="UTC")
    df = pd.DataFrame({
        "item_id": pd.array(item_ids, dtype="string[pyarrow]"),
        "embedding": pd.array(_encode_matrix(matrix), dtype=pd.ArrowDtype(pa.large_binary())),
        "model": pd.array([model] * len(item_ids), dtype="string[pyarrow]"),
        "dim": pd.array([dim] * len(item_ids), dtype="int32[pyarrow]"),
        "created_at": pd.array([created] * len(item_ids), dtype=pd.ArrowDtype(pa.timestamp("ns", tz="UTC"))),
    })
    shard = f"{SHARD_PREFIX}{uuid.uuid4().hex}{SHARD_SUFFIX}"
    data_io.save_parquet(df=df, storage_location=STORE_LOCATION, filename=shard)
    return shard






def embed_pending(batch_size: int = 20000, reporter=None) -> dict:
    """Embed up to ``batch_size`` not-yet-embedded annotated videos.

    Computes the backlog (annotated_ok minus already-embedded-by-the-active-
    model), embeds the head slice with the active backend, and writes a new
    shard. Items whose batch failed (all-zero vectors) are skipped so they
    retry on a later run.

    Args:
        batch_size: Maximum number of videos to embed in this call.
        reporter: Optional status reporter.

    Returns:
        Dict with ``embedded`` (count written), ``remaining`` (backlog left
        after this call), ``total`` (annotated_ok population), and ``shard``.
    """
    def _log(msg: str) -> None:
        if reporter is not None:
            reporter.log(msg)
        else:
            logger.info(msg)

    backend = active_embedding_backend()
    model = backend.model_id()
    dim = backend.dim()

    all_ids = annotated_ok_item_ids()
    have = embedded_item_ids(model=model)
    todo = [i for i in all_ids if i not in have]
    _log(f"Annotated={len(all_ids):,}  embedded={len(have):,}  pending={len(todo):,}  (model={model})")

    if not todo:
        return {"embedded": 0, "remaining": 0, "total": len(all_ids), "shard": None}

    slice_ids = todo[:batch_size]
    slice_set = set(slice_ids)

    # Load just the document columns for the whole corpus, then filter to this
    # slice — cheaper than a huge pyarrow `in` filter on the item_id column.
    anno = data_io.load_parquet_selective(
        storage_location=STORE_LOCATION, filename=ANNOTATIONS_FILE, columns=ANNO_DOC_COLS,
    )
    anno["item_id"] = anno["item_id"].astype("string")
    anno = anno[anno["item_id"].isin(slice_set)]

    scrape = data_io.load_parquet_selective(
        storage_location=STORE_LOCATION, filename=SCRAPES_FILE, columns=SCRAPE_DOC_COLS,
    )
    scrape["item_id"] = scrape["item_id"].astype("string")
    scrape = scrape.drop_duplicates("item_id")
    merged = anno.merge(scrape, on="item_id", how="left")

    _log(f"Building documents for {len(merged):,} videos...")
    docs = build_documents(merged)

    _log(f"Embedding {len(docs):,} documents with {model}@{dim}...")
    matrix = backend.embed_texts(docs.tolist(), reporter=reporter)

    # Drop rows whose batch failed (all-zero vectors) so they retry next run.
    nonzero = np.abs(matrix).sum(axis=1) > 0
    kept_ids = merged["item_id"].to_numpy()[nonzero].tolist()
    kept_matrix = matrix[nonzero]
    failed = int((~nonzero).sum())
    if failed:
        _log(f"WARNING: {failed} videos failed embedding; will retry on next run.")

    shard = None
    if len(kept_ids) > 0:
        shard = _write_shard(kept_ids, kept_matrix, model=model, dim=dim)
        _log(f"Wrote shard {shard} with {len(kept_ids):,} embeddings.")

    remaining = len(todo) - len(slice_ids)
    return {
        "embedded": len(kept_ids),
        "remaining": remaining,
        "total": len(all_ids),
        "shard": shard,
    }






def load_embeddings(reporter=None, model: str | None = None) -> tuple[list[str], np.ndarray]:
    """Load the embedding store for one model as ``(item_ids, matrix)``.

    Only shards (and rows) whose ``model`` stamp matches are loaded — mixing
    vectors from different embedding models in one matrix would be
    geometrically meaningless. Each shard decodes at its own stored ``dim``.

    Args:
        reporter: Optional status reporter.
        model: Embedding model id to load; None = the active backend's.

    Returns:
        A tuple of the item_id list and an ``(n, dim)`` float32 matrix.
        Returns ``([], empty array)`` when the store holds nothing for the
        model.
    """
    backend = active_embedding_backend()
    if model is None:
        model = backend.model_id()
    empty_dim = backend.dim() if model == backend.model_id() else 0

    shards = _list_shards()
    if not shards:
        return [], np.empty((0, empty_dim), dtype=np.float32)

    ids: list[str] = []
    parts: list[np.ndarray] = []
    for shard in shards:
        df = data_io.load_parquet_selective(
            storage_location=STORE_LOCATION, filename=shard,
            columns=["item_id", "embedding", "model"],
        )
        if df is None or len(df) == 0:
            continue
        df = df[_model_mask(df, model)]
        if len(df) == 0:
            continue
        ids.extend(df["item_id"].astype("string").tolist())
        parts.append(decode_embeddings(df["embedding"].tolist()))
        if reporter is not None:
            reporter.log(f"Loaded shard {shard} ({len(df):,} rows)")

    if not parts:
        return [], np.empty((0, empty_dim), dtype=np.float32)
    return ids, np.vstack(parts)
