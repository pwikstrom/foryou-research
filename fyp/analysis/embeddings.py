"""Dense semantic embeddings for annotated videos.

Builds one labelled text document per annotated video from the Gemini
annotation fields (``recoded/machine_annotations_recoded.parquet``) plus a few
scrape metadata fields (``recoded/scrapes_recoded.parquet``), embeds it with
the Vertex ``gemini-embedding-001`` model (Matryoshka-truncated to
:data:`EMBED_DIM` dimensions), and persists the vectors as append-only sharded
parquet files in the ``recoded`` store.

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
      (``EMBED_DIM * 2`` bytes/row) — about half the size of float32 lists and
      a clean round-trip through :mod:`fyp.data_io`.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import pyarrow as pa

import fyp.core.gemini_client as gemini_client
import fyp.data_io as data_io
from fyp.logging_setup import get_logger
from google import genai
from google.genai.types import EmbedContentConfig

logger = get_logger(__name__)




# Embedding model configuration. gemini-embedding-001 supports Matryoshka
# truncation to 768 / 1536 / 3072 dims; 1536 is the quality/size sweet spot.
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 1536
EMBED_LOCATION = "us-central1"
EMBED_TASK_TYPE = "CLUSTERING"

# Concurrency / batching for the Vertex embedding calls.
_EMBED_BATCH = 20
_EMBED_WORKERS = 8
_EMBED_RETRIES = 4

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

_client: genai.Client | None = None






def _get_client() -> genai.Client:
    """Return a process-wide GenAI client for embedding calls.

    Honours whichever Gemini mode is configured — Vertex AI or the plain Gemini
    API — via :func:`fyp.core.gemini_client.make_client`. In Vertex mode the
    location is pinned to :data:`EMBED_LOCATION`, because the annotation
    client's ``global`` endpoint serves generation, not embeddings; in API-key
    mode the endpoint takes no region and the argument is ignored.

    Returns:
        A cached :class:`google.genai.Client`.

    Raises:
        GeminiNotConfiguredError: When no usable Gemini mode is configured.
    """
    global _client
    if _client is None:
        _client = gemini_client.make_client(location=EMBED_LOCATION)
    return _client






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






def _embed_batch(client: genai.Client, chunk: list[str]) -> list[list[float]] | None:
    """Embed one batch of texts with retry, returning vectors or None on failure."""
    config = EmbedContentConfig(task_type=EMBED_TASK_TYPE, output_dimensionality=EMBED_DIM)
    for attempt in range(_EMBED_RETRIES):
        try:
            resp = client.models.embed_content(model=EMBED_MODEL, contents=chunk, config=config)
            return [e.values for e in resp.embeddings]
        except Exception:
            if attempt == _EMBED_RETRIES - 1:
                return None
            import time
            time.sleep(1.5 * (attempt + 1))
    return None






def embed_texts(texts: list[str], reporter=None) -> np.ndarray:
    """Embed a list of texts into an ``(n, EMBED_DIM)`` float32 matrix.

    Calls the Vertex embedding endpoint in concurrent batches. A batch that
    fails after all retries yields zero-vectors for its rows; the caller is
    expected to detect and skip those item_ids (they retry on the next run).

    Args:
        texts: Documents to embed (empty strings are replaced with a space).
        reporter: Optional status reporter for progress logging.

    Returns:
        A float32 array of shape ``(len(texts), EMBED_DIM)``.
    """
    client = _get_client()
    safe = [t if t else " " for t in texts]
    batches = [(i, safe[i:i + _EMBED_BATCH]) for i in range(0, len(safe), _EMBED_BATCH)]
    out: dict[int, list[list[float]]] = {}
    done = 0

    with ThreadPoolExecutor(max_workers=_EMBED_WORKERS) as ex:
        futures = {ex.submit(_embed_batch, client, chunk): i for i, chunk in batches}
        for fut in as_completed(futures):
            i = futures[fut]
            vecs = fut.result()
            if vecs is None:
                vecs = [[0.0] * EMBED_DIM] * len(safe[i:i + _EMBED_BATCH])
            out[i] = vecs
            done += 1
            if reporter is not None and done % 50 == 0:
                pct = int(done / len(batches) * 100)
                reporter.update_progress(pct, f"Embedded {done}/{len(batches)} batches")

    matrix: list[list[float]] = []
    for i in range(0, len(safe), _EMBED_BATCH):
        matrix.extend(out[i])
    return np.asarray(matrix, dtype=np.float32)






def _encode_matrix(matrix: np.ndarray) -> list[bytes]:
    """Encode an ``(n, dim)`` float matrix to a list of float16 byte strings."""
    m16 = matrix.astype(np.float16)
    return [row.tobytes() for row in m16]






def decode_embeddings(byte_values, dim: int = EMBED_DIM) -> np.ndarray:
    """Decode a column of float16 byte strings back to an ``(n, dim)`` float32 array.

    Args:
        byte_values: Iterable of ``bytes`` (the stored ``embedding`` column).
        dim: Vector dimensionality.

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






def embedded_item_ids() -> set[str]:
    """Return the set of item_ids already present in the embedding store."""
    ids: set[str] = set()
    for shard in _list_shards():
        df = data_io.load_parquet_selective(
            storage_location=STORE_LOCATION, filename=shard, columns=["item_id"],
        )
        if df is not None and len(df) > 0:
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
    return ok["item_id"].astype("string").tolist()






def _write_shard(item_ids: list[str], matrix: np.ndarray) -> str:
    """Persist one batch of embeddings as a new shard; return its filename.

    The frame is built with explicit Arrow dtypes so :func:`data_io.save_parquet`
    takes its all-ArrowDtype fast path (the float16 vectors live in a binary
    column).
    """
    created = pd.Timestamp.now(tz="UTC")
    df = pd.DataFrame({
        "item_id": pd.array(item_ids, dtype="string[pyarrow]"),
        "embedding": pd.array(_encode_matrix(matrix), dtype=pd.ArrowDtype(pa.large_binary())),
        "model": pd.array([EMBED_MODEL] * len(item_ids), dtype="string[pyarrow]"),
        "dim": pd.array([EMBED_DIM] * len(item_ids), dtype="int32[pyarrow]"),
        "created_at": pd.array([created] * len(item_ids), dtype=pd.ArrowDtype(pa.timestamp("ns", tz="UTC"))),
    })
    shard = f"{SHARD_PREFIX}{uuid.uuid4().hex}{SHARD_SUFFIX}"
    data_io.save_parquet(df=df, storage_location=STORE_LOCATION, filename=shard)
    return shard






def embed_pending(batch_size: int = 20000, reporter=None) -> dict:
    """Embed up to ``batch_size`` not-yet-embedded annotated videos.

    Computes the backlog (annotated_ok minus already-embedded), embeds the head
    slice, and writes a new shard. Items whose batch failed (all-zero vectors)
    are skipped so they retry on a later run.

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

    all_ids = annotated_ok_item_ids()
    have = embedded_item_ids()
    todo = [i for i in all_ids if i not in have]
    _log(f"Annotated={len(all_ids):,}  embedded={len(have):,}  pending={len(todo):,}")

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

    _log(f"Embedding {len(docs):,} documents with {EMBED_MODEL}@{EMBED_DIM}...")
    matrix = embed_texts(docs.tolist(), reporter=reporter)

    # Drop rows whose batch failed (all-zero vectors) so they retry next run.
    nonzero = np.abs(matrix).sum(axis=1) > 0
    kept_ids = merged["item_id"].to_numpy()[nonzero].tolist()
    kept_matrix = matrix[nonzero]
    failed = int((~nonzero).sum())
    if failed:
        _log(f"WARNING: {failed} videos failed embedding; will retry on next run.")

    shard = None
    if len(kept_ids) > 0:
        shard = _write_shard(kept_ids, kept_matrix)
        _log(f"Wrote shard {shard} with {len(kept_ids):,} embeddings.")

    remaining = len(todo) - len(slice_ids)
    return {
        "embedded": len(kept_ids),
        "remaining": remaining,
        "total": len(all_ids),
        "shard": shard,
    }






def load_embeddings(reporter=None) -> tuple[list[str], np.ndarray]:
    """Load the full embedding store as ``(item_ids, matrix)``.

    Args:
        reporter: Optional status reporter.

    Returns:
        A tuple of the item_id list and an ``(n, EMBED_DIM)`` float32 matrix.
        Returns ``([], empty array)`` when the store is empty.
    """
    shards = _list_shards()
    if not shards:
        return [], np.empty((0, EMBED_DIM), dtype=np.float32)

    ids: list[str] = []
    parts: list[np.ndarray] = []
    for shard in shards:
        df = data_io.load_parquet_selective(
            storage_location=STORE_LOCATION, filename=shard, columns=["item_id", "embedding"],
        )
        if df is None or len(df) == 0:
            continue
        ids.extend(df["item_id"].astype("string").tolist())
        parts.append(decode_embeddings(df["embedding"].tolist()))
        if reporter is not None:
            reporter.log(f"Loaded shard {shard} ({len(df):,} rows)")

    if not parts:
        return [], np.empty((0, EMBED_DIM), dtype=np.float32)
    return ids, np.vstack(parts)
