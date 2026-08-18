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

# Additional columns for the EXPERIMENTAL "docv2" document (see
# build_document_v2): visible symbols/brands, the dominant on-screen
# gender/ethnicity, and the caption text (minus hashtags, which have their own
# line). docv2 is not wired into the live pipeline — it exists for the
# side-by-side shard experiment driven by a local pilot script (unpublished).
ANNO_DOC_COLS_V2 = ANNO_DOC_COLS + [
    "symbols_and_brands", "main_gender", "main_ethnicity",
]
SCRAPE_DOC_COLS_V2 = SCRAPE_DOC_COLS + ["desc_not_hashtags"]

# Per-field character caps keep each document well under the model's
# per-instance token limit and stop any single long transcript / overlay list
# from dominating the vector.
_CAP_STORY = 1200
_CAP_SPOKEN = 800
_CAP_OVERLAYS = 400
_CAP_OBJECTS = 300
_CAP_HASHTAGS = 200
_CAP_SHORT = 120

# docv2 rebalances the transcript: at 800 chars the spoken word dominated the
# few-word fields (objects, symbols, hashtags), pulling talk-heavy videos
# together regardless of what they showed. Audio caps stay unchanged.
_CAP_SPOKEN_V2 = 300
_CAP_DESC_V2 = 300
_CAP_SYMBOLS_V2 = 150






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






def build_document_v2(row: pd.Series) -> str:
    """EXPERIMENTAL document variant ("docv2") — not used by the live pipeline.

    Differences from :func:`build_document` (2026-08-11, prompted by binge
    boundaries disagreeing with human perceptual grouping on session
    AIO-00060):

    * Adds ``Symbols/brands`` (flags and brands carry scene/topic context the
      story often omits), ``People`` (dominant on-screen gender/ethnicity —
      perceptual continuity the text fields cannot see), and ``Description``
      (the caption minus hashtags).
    * The transcript cap drops 800 → 300 so spoken word stops drowning the
      few-word visual fields. Story and the audio fields are unchanged.

    Vectors from this document are only comparable to other docv2 vectors —
    the pilot script stamps them under a ``<model>+docv2`` store key so the
    model-scoped shard store keeps the two spaces fully separate.

    Args:
        row: A row carrying :data:`ANNO_DOC_COLS_V2` and
            :data:`SCRAPE_DOC_COLS_V2`.

    Returns:
        The assembled document string.
    """
    story = _text(row.get("video_story"), _CAP_STORY)
    activity = f'{_text(row.get("main_activity"), 40)}; {_text(row.get("type_of_story"), 40)}'
    spoken = _text(row.get("transcript_no_repetitions"), _CAP_SPOKEN_V2) or "(none)"
    overlays = _list(row.get("text_overlays"), _CAP_OVERLAYS) or "(none)"
    objects = _list(row.get("objects"), _CAP_OBJECTS)
    symbols = _list(row.get("symbols_and_brands"), _CAP_SYMBOLS_V2) or "(none)"
    people = f'{_text(row.get("main_gender"), 40)}; {_text(row.get("main_ethnicity"), 60)}'
    sounds = (
        f'{_list(row.get("notable_sounds"), _CAP_SHORT)}; '
        f'{_list(row.get("background_music"), 60)}; '
        f'{_text(row.get("music_title"), 80)}'
    )
    hashtags = _list(row.get("desc_hashtags"), _CAP_HASHTAGS)
    desc = _list(row.get("desc_not_hashtags"), _CAP_DESC_V2) or "(none)"
    return (
        f"Story: {story}\nActivity: {activity}\n"
        f"Spoken: {spoken}\nOn-screen text: {overlays}\nObjects: {objects}\n"
        f"Symbols/brands: {symbols}\nPeople: {people}\n"
        f"Sounds/music: {sounds}\nHashtags: {hashtags}\nDescription: {desc}"
    )




# Document variants: name -> (builder, annotation columns, scrape columns).
# "v1" is the live document; experimental variants embed under a
# "<model>+doc<variant>" store key and never touch the live space.
DOC_VARIANTS = {
    "v1": (build_document, ANNO_DOC_COLS, SCRAPE_DOC_COLS),
    "v2": (build_document_v2, ANNO_DOC_COLS_V2, SCRAPE_DOC_COLS_V2),
}




def variant_store_model(model: str, variant: str) -> str:
    """The shard-store ``model`` key for a document variant.

    ``v1`` is the live document and keeps the bare model id (its shards ARE
    the live store); any other variant is suffixed so the model-scoped store
    treats it as a separate corpus.
    """
    return model if variant == "v1" else f"{model}+doc{variant}"




def build_documents(df: pd.DataFrame, variant: str = "v1") -> pd.Series:
    """Build labelled embedding documents for every row of ``df``.

    Args:
        df: Merged annotation+scrape frame containing the document columns.
        variant: Document variant name from :data:`DOC_VARIANTS`.

    Returns:
        A string Series aligned to ``df.index``.
    """
    builder = DOC_VARIANTS[variant][0]
    return df.apply(builder, axis=1)






def _encode_matrix(matrix: np.ndarray) -> list[bytes]:
    """Encode an ``(n, dim)`` float matrix to a list of float16 byte strings."""
    m16 = matrix.astype(np.float16)
    return [row.tobytes() for row in m16]






def decode_embeddings(byte_values, dim: int | None = None) -> np.ndarray:
    """Decode a column of float16 byte strings back to an ``(n, dim)`` float32 array.

    Args:
        byte_values: Iterable of ``bytes`` (the stored ``embedding`` column).
        dim: Vector dimensionality; None takes it from the first row.

    Returns:
        A float32 array of shape ``(n, dim)``.
    """
    byte_values = list(byte_values)
    if not byte_values:
        return np.empty((0, dim or 0), dtype=np.float32)
    if dim is None:
        dim = len(byte_values[0]) // 2
    # Preallocate-and-fill: the historical list-of-rows -> vstack -> astype
    # chain held ~3.6x the result transiently.
    out = np.empty((len(byte_values), dim), dtype=np.float32)
    for i, b in enumerate(byte_values):
        out[i] = np.frombuffer(b, dtype=np.float16)
    return out






def decode_embeddings_arrow(embedding_col, out: np.ndarray | None = None,
                            offset: int = 0,
                            dtype=np.float32) -> np.ndarray:
    """Decode a pyarrow-backed binary embedding column into float32 rows.

    Fast path: every real shard stores fixed-width float16 blobs, so the
    Arrow ``large_binary`` offsets are uniform and the whole column is one
    zero-copy ``frombuffer``/``reshape`` view upcast directly into the output
    — no per-row bytes objects, no intermediate matrix. Non-uniform offsets
    (never observed; a hypothetical mixed-dim shard) fall back to a per-row
    loop that still writes straight into the output.

    Args:
        embedding_col: A pandas Series with ``ArrowDtype`` (large_)binary
            values, or a pyarrow (Chunked)Array of them.
        out: Optional preallocated ``(>= offset+n, dim)`` array to fill;
            None allocates one exactly ``(n, dim)`` of ``dtype``.
        offset: First row of ``out`` to write.
        dtype: Output dtype when allocating (``out``'s own dtype wins when
            given). float16 lets the dense-sidecar compaction extract the
            stored width without a float32 round-trip.

    Returns:
        The filled array (``out`` when given).
    """
    arr = getattr(getattr(embedding_col, "array", None), "_pa_array", embedding_col)
    if isinstance(arr, pa.ChunkedArray):
        arr = arr.combine_chunks()
    n = len(arr)
    if n == 0:
        return out if out is not None else np.empty((0, 0), dtype=np.float32)

    buffers = arr.buffers()
    validity = buffers[0]
    offsets_dtype = np.int64 if pa.types.is_large_binary(arr.type) else np.int32
    offs = np.frombuffer(buffers[1], dtype=offsets_dtype)[arr.offset:arr.offset + n + 1]
    row_bytes = int(offs[1] - offs[0]) if n else 0
    uniform = (validity is None and row_bytes > 0 and row_bytes % 2 == 0
               and bool(np.all(np.diff(offs) == row_bytes)))

    if uniform:
        data = np.frombuffer(buffers[2], dtype=np.uint8)
        flat = data[offs[0]:offs[-1]].view(np.float16).reshape(n, row_bytes // 2)
        if out is None:
            # astype always copies here, so the result never aliases the
            # (refcounted, possibly short-lived) Arrow buffer.
            return flat.astype(dtype)
        out[offset:offset + n] = flat  # implicit upcast to out's dtype
        return out

    # Fallback: per-row decode straight into the output.
    first = next(v for v in arr if v.is_valid)
    dim = len(first.as_py()) // 2
    if out is None:
        out = np.empty((n, dim), dtype=dtype)
        offset = 0
    for i, v in enumerate(arr):
        out[offset + i] = np.frombuffer(v.as_py(), dtype=np.float16)
    return out






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

    # Re-read the embedded set just before writing: a concurrent run (e.g. a
    # Cloud Tasks redelivery of a still-running batch) may have landed the
    # same slice while this one was embedding. Dropping the overlap narrows
    # the duplicate-shard window to the seconds between this check and the
    # write; readers additionally dedupe as a backstop.
    if len(kept_ids) > 0:
        have_now = embedded_item_ids(model=model)
        fresh = [i not in have_now for i in kept_ids]
        n_overlap = len(kept_ids) - sum(fresh)
        if n_overlap:
            _log(f"WARNING: {n_overlap:,} of this batch's items were embedded "
                 f"by a concurrent run while this one worked; dropping them "
                 f"from the shard.")
            kept_ids = [i for i, f in zip(kept_ids, fresh) if f]
            kept_matrix = kept_matrix[np.asarray(fresh, dtype=bool)]

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

    Duplicate item_ids across shards (e.g. two concurrent embed runs writing
    the same backlog slice) are collapsed to the **last** occurrence in
    shard-listing order — the same winner the dense sidecar's index picks
    (``ensure_dense_store`` drops duplicates with ``keep="last"``), so both
    read paths return the same vector for a duplicated item.

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

    # Pass 1 — sizes and ids only (a few MB even at millions of rows), so the
    # result matrix can be preallocated once. The historical accumulate-then-
    # vstack held every per-shard matrix AND the concatenated result at the
    # return (exactly 2x), on top of a ~3.6x per-shard decode transient.
    ids: list[str] = []
    plan: list[tuple[str, int]] = []
    dim: int | None = None
    for shard in shards:
        df = data_io.load_parquet_selective(
            storage_location=STORE_LOCATION, filename=shard,
            columns=["item_id", "model", "dim"],
        )
        if df is None or len(df) == 0:
            continue
        df = df[_model_mask(df, model)]
        if len(df) == 0:
            continue
        ids.extend(df["item_id"].astype("string").tolist())
        plan.append((shard, len(df)))
        if dim is None and "dim" in df.columns:
            dim = int(df["dim"].iloc[0])

    if not plan:
        return [], np.empty((0, empty_dim), dtype=np.float32)
    if dim is None:
        # Hypothetical pre-`dim`-column shard: derive the width from one row.
        probe = data_io.load_parquet_selective(
            storage_location=STORE_LOCATION, filename=plan[0][0],
            columns=["embedding", "model"],
        )
        probe = probe[_model_mask(probe, model)]
        dim = len(probe["embedding"].iloc[0]) // 2
        del probe

    # Duplicate ids collapse to their last occurrence (see docstring). The
    # mask is computed up front so the result matrix is allocated at its
    # final, deduplicated size.
    keep = ~pd.Index(ids).duplicated(keep="last")
    n_dupes = int(len(ids) - keep.sum())
    if n_dupes:
        logger.warning(
            f"Embedding store holds {n_dupes:,} duplicate item_id rows for "
            f"model {model}; keeping the last occurrence of each."
        )

    # Pass 2 — decode each shard's vectors straight into the preallocated
    # matrix (zero-copy Arrow view on the uniform-offset fast path). A shard
    # containing dropped duplicates decodes via a shard-sized temp instead.
    kept_ids = [iid for iid, k in zip(ids, keep) if k]
    out = np.empty((len(kept_ids), dim), dtype=np.float32)
    row = 0
    wrote = 0
    for shard, n_rows in plan:
        df = data_io.load_parquet_selective(
            storage_location=STORE_LOCATION, filename=shard,
            columns=["embedding", "model"],
        )
        df = df[_model_mask(df, model)]
        shard_keep = keep[row:row + n_rows]
        n_kept = int(shard_keep.sum())
        if n_kept == n_rows:
            decode_embeddings_arrow(df["embedding"], out=out, offset=wrote)
        elif n_kept:
            tmp = np.empty((n_rows, dim), dtype=np.float32)
            decode_embeddings_arrow(df["embedding"], out=tmp, offset=0)
            out[wrote:wrote + n_kept] = tmp[shard_keep]
            del tmp
        row += n_rows
        wrote += n_kept
        del df
        if reporter is not None:
            reporter.log(f"Loaded shard {shard} ({n_rows:,} rows)")
    return kept_ids, out
