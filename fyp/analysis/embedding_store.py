"""Random-access dense sidecar over the append-only embedding shard store.

The parquet shards (``recoded/video_embeddings__*.parquet``) are the source of
truth but are unsuitable for batch-scoped access: each is a single row group
whose item ids span the whole id space, so reading any subset decodes every
shard in full (~7.7 GB per read at 2.5M vectors). This module maintains a pure
**derived cache** per embedding model:

* ``embedding_dense__<model>__part<k>.f16`` — raw little-endian float16 rows,
  byte-identical to what the shards store, **one part per compacted shard** in
  compaction order. Parts are immutable; growth is new parts only (GCS blobs
  are not appendable, and 1:1 shard↔part keeps the append O(new)).
* ``embedding_dense_index__<model>.parquet`` — sorted ``item_id`` → global
  ``row``. When an item id appears in several shards the LAST occurrence wins,
  matching ``load_directional_store``'s ``{iid: i}`` dict comprehension.
* ``embedding_dense_manifest__<model>.json`` — compacted-shard list with a
  size+mtime fingerprint, part boundaries, and the running float64 vector sum
  (so the exact corpus mean over all compacted rows is available without a
  second pass).

Row vectors are read back via :func:`read_vectors`: ``np.memmap`` in local
mode (RSS = touched pages), coalesced ranged reads in GCS mode — never
``local_copy()``, whose temp dir is memory-backed on Cloud Run.

The manifest's corpus mean is persisted through :func:`save_corpus_mean` with
a ``store_fingerprint`` stamp; :func:`load_corpus_mean` refuses a mean whose
fingerprint no longer matches the shard set, which is the guard that keeps a
batched consumer from ever centring on a stale mean.
"""

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

import fyp.data_io as data_io
from fyp.analysis import embeddings
from fyp.logging_setup import get_logger

logger = get_logger(__name__)

STORE_LOCATION = embeddings.STORE_LOCATION
DENSE_BLOB_PREFIX = "embedding_dense__"
DENSE_INDEX_PREFIX = "embedding_dense_index__"
DENSE_MANIFEST_PREFIX = "embedding_dense_manifest__"
CORPUS_MEAN_PREFIX = "embedding_corpus_mean__"

# GCS mode: ranged reads whose byte gap is at most this are merged into one
# request (over-reading the gap). At the corpus's measured ~6% batch density
# most neighbours coalesce; raise to trade bytes for request count.
DEFAULT_COALESCE_BYTES = 65_536






def _safe_model(model: str) -> str:
    """Filesystem-safe form of a model id (mirrors the corpus-mean naming)."""
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in model)






def _manifest_filename(model: str) -> str:
    return f"{DENSE_MANIFEST_PREFIX}{_safe_model(model)}.json"






def _index_filename(model: str) -> str:
    return f"{DENSE_INDEX_PREFIX}{_safe_model(model)}.parquet"






def _part_filename(model: str, k: int) -> str:
    return f"{DENSE_BLOB_PREFIX}{_safe_model(model)}__part{k:04d}.f16"






def corpus_mean_filename(model: str) -> str:
    """Per-model corpus-mean cache filename (shared with session_explorer)."""
    return f"{CORPUS_MEAN_PREFIX}{_safe_model(model)}.json"






def store_fingerprint() -> str:
    """Fingerprint of the shard set: sha256 over sorted (name, size, mtime).

    Model-independent by design — shards are shared across models (rows carry
    per-row ``model`` stamps), so any shard-set change conservatively
    invalidates every model's derived state.
    """
    entries = []
    for shard in sorted(embeddings._list_shards()):
        st = data_io.stat(storage_location=STORE_LOCATION, filename=shard)
        if st is not None:
            entries.append((shard, int(st.get("size", 0)), float(st.get("mtime", 0.0))))
    payload = json.dumps(entries, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()






def save_corpus_mean(model: str, mean: np.ndarray, count: int,
                     store_fp: str | None = None) -> None:
    """Persist the corpus mean for ``model`` (optionally fingerprint-stamped).

    Args:
        model: Embedding model id the mean was computed over.
        mean: The ``(d,)`` mean vector.
        count: Number of vectors the mean was computed over (provenance).
        store_fp: The shard-set fingerprint the mean corresponds to; None
            (legacy writers) omits the stamp, and a fingerprint-validating
            reader will then refuse the payload.
    """
    payload = {
        "model": model,
        "dim": int(mean.shape[0]),
        "count": int(count),
        "mean": [float(v) for v in mean],
    }
    if store_fp is not None:
        payload["store_fingerprint"] = store_fp
    data_io.save_json(data=payload, storage_location=STORE_LOCATION,
                      filename=corpus_mean_filename(model))






def load_corpus_mean(model: str, expected_fp: str | None = None
                     ) -> np.ndarray | None:
    """Load the cached corpus mean for ``model``, or None when absent/stale.

    Args:
        model: Embedding model id.
        expected_fp: When given, the payload must carry a matching
            ``store_fingerprint`` — a missing or different stamp returns None
            (stale mean; recompute). A stale mean silently changes every
            downstream cosine distance, so validation is not optional for
            batched consumers.

    Returns:
        The ``(d,)`` float64 mean, or None.
    """
    fname = corpus_mean_filename(model)
    if not data_io.exists(storage_location=STORE_LOCATION, filename=fname):
        return None
    payload = data_io.load_json(storage_location=STORE_LOCATION, filename=fname)
    if not payload or payload.get("model") != model or not payload.get("mean"):
        return None
    if expected_fp is not None and payload.get("store_fingerprint") != expected_fp:
        return None
    return np.asarray(payload["mean"], dtype=np.float64)






class CorpusMeanDrift(RuntimeError):
    """The shard store changed under a consumer pinned to one corpus mean."""






@dataclass
class DenseIndex:
    """In-memory handle on one model's dense sidecar.

    The only O(corpus) state a batched consumer holds: the sorted id array +
    int32 rows (~27 bytes/vector — 220x smaller than the float32 matrix).
    """

    model: str
    dim: int
    n_rows: int
    ids: pa.Array          # sorted item_ids
    rows: np.ndarray       # int32 global rows, aligned with ids
    parts: list            # [{"filename", "start_row", "rows"}, ...]
    store_fp: str


    def lookup(self, item_ids) -> tuple[np.ndarray, np.ndarray]:
        """Map item ids to global rows.

        Args:
            item_ids: Iterable of item ids (stringified for matching).

        Returns:
            ``(rows, found)`` — int64 global rows (only for found ids, in
            input order) and a boolean mask aligned to the input.
        """
        query = pa.array([str(i) for i in item_ids], type=pa.string())
        pos = pc.index_in(query, value_set=self.ids).fill_null(-1)
        pos_np = pos.to_numpy(zero_copy_only=False).astype(np.int64)
        found = pos_np >= 0
        return self.rows[pos_np[found]].astype(np.int64), found






def load_manifest(model: str) -> dict | None:
    """Load the model's dense-store manifest, or None when absent."""
    fname = _manifest_filename(model)
    if not data_io.exists(storage_location=STORE_LOCATION, filename=fname):
        return None
    manifest = data_io.load_json(storage_location=STORE_LOCATION, filename=fname)
    return manifest if isinstance(manifest, dict) else None






def load_index(model: str) -> DenseIndex | None:
    """Load the model's id → row index plus part layout.

    Returns:
        A :class:`DenseIndex`, or None when the dense store is not built.
    """
    manifest = load_manifest(model)
    if manifest is None:
        return None
    idx_df = data_io.load_parquet_selective(
        storage_location=STORE_LOCATION, filename=_index_filename(model),
        columns=["item_id", "row"])
    if idx_df is None:
        return None
    ids = pa.array(idx_df["item_id"].astype(str).tolist(), type=pa.string())
    rows = idx_df["row"].to_numpy(dtype=np.int32)
    return DenseIndex(
        model=model, dim=int(manifest["dim"]), n_rows=int(manifest["n_rows"]),
        ids=ids, rows=rows, parts=list(manifest.get("parts", [])),
        store_fp=str(manifest.get("store_fingerprint", "")))






def _shard_state() -> list[dict]:
    """Current (name, size, mtime, ...) state of every shard, sorted by name."""
    out = []
    for shard in sorted(embeddings._list_shards()):
        st = data_io.stat(storage_location=STORE_LOCATION, filename=shard)
        if st is not None:
            out.append({"name": shard, "size": int(st.get("size", 0)),
                        "mtime": float(st.get("mtime", 0.0))})
    return out






def _load_shard_for_model(shard: str, model: str):
    """One shard's (item_ids, float16 matrix) for ``model`` (possibly empty)."""
    df = data_io.load_parquet_selective(
        storage_location=STORE_LOCATION, filename=shard,
        columns=["item_id", "embedding", "model"])
    if df is None or len(df) == 0:
        return [], None
    df = df[embeddings._model_mask(df, model)]
    if len(df) == 0:
        return [], None
    ids = df["item_id"].astype(str).tolist()
    mat = embeddings.decode_embeddings_arrow(df["embedding"], dtype=np.float16)
    return ids, mat






def ensure_dense_store(model: str, reporter=None) -> dict:
    """Build or extend the model's dense sidecar; idempotent and O(new shards).

    Compares the manifest's compacted-shard list against the store. Fresh →
    no-op. New shards only (every compacted shard still present with the same
    size+mtime) → append one part per new shard. A compacted shard missing or
    changed → full rebuild with a loud warning (the shard store is meant to be
    append-only; a mutated shard means that invariant broke).

    Also maintains the manifest's running float64 vector sum and persists the
    exact corpus mean via :func:`save_corpus_mean`, fingerprint-stamped.

    Args:
        model: Embedding model id.
        reporter: Optional status reporter for progress logs.

    Returns:
        The up-to-date manifest dict.
    """
    def _log(msg: str) -> None:
        if reporter is not None:
            reporter.log(msg)
        else:
            logger.info(msg)

    current = _shard_state()
    fp = store_fingerprint()
    manifest = load_manifest(model)

    if manifest is not None and manifest.get("store_fingerprint") == fp:
        return manifest

    compacted = list(manifest.get("compacted_shards", [])) if manifest else []
    current_by_name = {s["name"]: s for s in current}
    intact = all(
        c["name"] in current_by_name
        and current_by_name[c["name"]]["size"] == c["size"]
        and current_by_name[c["name"]]["mtime"] == c["mtime"]
        for c in compacted)

    if manifest is None or not intact:
        if manifest is not None:
            logger.warning(
                f"[DENSE] {model}: a compacted shard changed or vanished — the "
                f"shard store is append-only, so this should not happen. "
                f"Rebuilding the dense store from scratch.")
        for part in (manifest or {}).get("parts", []):
            data_io.remove(storage_location=STORE_LOCATION,
                           filename=part["filename"])
        manifest = {
            "model": model, "dim": None, "n_rows": 0, "parts": [],
            "compacted_shards": [], "mean_sum": None,
        }
        compacted = []

    done_names = {c["name"] for c in compacted}
    new_shards = [s for s in current if s["name"] not in done_names]

    n_rows = int(manifest["n_rows"])
    dim = manifest["dim"]
    mean_sum = (np.asarray(manifest["mean_sum"], dtype=np.float64)
                if manifest.get("mean_sum") else None)
    parts = list(manifest["parts"])
    id_frames = []
    next_part = (max((int(p["filename"].rsplit("part", 1)[1].split(".")[0])
                      for p in parts), default=-1) + 1)

    for state in new_shards:
        shard = state["name"]
        ids, mat = _load_shard_for_model(shard, model)
        entry = dict(state)
        entry["rows"] = len(ids)
        if ids:
            if dim is None:
                dim = int(mat.shape[1])
            elif mat.shape[1] != dim:
                raise ValueError(
                    f"[DENSE] {model}: shard '{shard}' has dim {mat.shape[1]}, "
                    f"store has {dim} — refusing to mix widths.")
            part_name = _part_filename(model, next_part)
            data_io.save_bytes(data=mat.tobytes(),
                               storage_location=STORE_LOCATION,
                               filename=part_name)
            parts.append({"filename": part_name, "start_row": n_rows,
                          "rows": len(ids)})
            entry["start_row"] = n_rows
            id_frames.append(pd.DataFrame({
                "item_id": ids,
                "row": np.arange(n_rows, n_rows + len(ids), dtype=np.int32)}))
            s = mat.sum(axis=0, dtype=np.float64)
            mean_sum = s if mean_sum is None else mean_sum + s
            n_rows += len(ids)
            next_part += 1
            _log(f"[DENSE] {model}: compacted {shard} ({len(ids):,} rows)")
        compacted.append(entry)
        del mat

    # Index: existing rows + new, last-occurrence-wins, sorted by item_id
    # (parity with load_directional_store's {iid: i} dict — last wins).
    if id_frames or manifest.get("n_rows", 0) != n_rows or not data_io.exists(
            storage_location=STORE_LOCATION, filename=_index_filename(model)):
        old_idx = None
        if manifest.get("n_rows", 0) and data_io.exists(
                storage_location=STORE_LOCATION, filename=_index_filename(model)):
            old_idx = data_io.load_parquet_selective(
                storage_location=STORE_LOCATION,
                filename=_index_filename(model), columns=["item_id", "row"])
        frames = ([old_idx] if old_idx is not None else []) + id_frames
        if frames:
            idx = pd.concat(frames, ignore_index=True)
            # Later global rows shadow earlier ones for a duplicated id.
            idx = idx.sort_values("row", kind="stable")
            idx = idx[~idx["item_id"].duplicated(keep="last")]
            idx = idx.sort_values("item_id", kind="stable")
            idx = idx.astype({"item_id": "string[pyarrow]", "row": "int32[pyarrow]"})
            data_io.save_parquet(df=idx.reset_index(drop=True),
                                 storage_location=STORE_LOCATION,
                                 filename=_index_filename(model))

    manifest.update({
        "model": model,
        "dim": int(dim) if dim is not None else None,
        "n_rows": n_rows,
        "parts": parts,
        "compacted_shards": compacted,
        "mean_sum": [float(v) for v in mean_sum] if mean_sum is not None else None,
        "store_fingerprint": fp,
        "built_at": pd.Timestamp.now(tz="UTC").isoformat(),
    })
    data_io.save_json(data=manifest, storage_location=STORE_LOCATION,
                      filename=_manifest_filename(model))

    if mean_sum is not None and n_rows:
        save_corpus_mean(model, mean_sum / n_rows, n_rows, store_fp=fp)
    _log(f"[DENSE] {model}: dense store up to date — {n_rows:,} rows, "
         f"{len(parts)} part(s), dim={dim}")
    return manifest






def _dense_cache_dir() -> str:
    """Root of the per-machine dense-part cache (see :func:`_cached_part_path`)."""
    return os.environ.get("FYP_DENSE_CACHE_DIR") or os.path.join(
        tempfile.gettempdir(), "fyp_dense_cache")




def _fetch_part_bytes(filename: str) -> bytes | bytearray:
    """Download one dense part from GCS (parallel ranged GETs for big blobs)."""
    _, _, _, blob_name = data_io._resolve_paths(STORE_LOCATION, filename)
    return data_io._download_blob_bytes(data_io._get_bucket(), blob_name)




def _cached_part_path(model: str, part: dict, store_fp: str, dim: int) -> str:
    """Local path of one dense part, downloading it into the cache if absent.

    Why a whole-part cache exists next to the ranged reads: a sessions link
    asks for 3-15% of the corpus rows scattered over every part, and at that
    density the coalesced ranges cover nearly the whole 1.9 GB store — ~30 s
    per link however small the batch (measured 2026-09-02). A task-runner
    instance serves many links in a row, so paying that once per instance
    and memory-mapping thereafter takes the phase to ~0 for every later link.

    Layout: ``<cache>/<model>/<fingerprint>/<part filename>``. Parts under one
    store fingerprint are immutable (a shard-set change rebuilds or appends
    under a new fingerprint), so a present file is trusted after a size check;
    other fingerprints' directories for the model are evicted so the cache
    holds one store at a time. Downloads go to a temp file and are renamed
    into place, so a concurrent reader never sees a partial part.
    """
    safe = _safe_model(model)
    root = os.path.join(_dense_cache_dir(), safe, (store_fp or "nofp")[:16])
    path = os.path.join(root, part["filename"])
    expected = int(part["rows"]) * dim * 2
    if os.path.exists(path) and os.path.getsize(path) == expected:
        return path
    os.makedirs(root, exist_ok=True)
    t0 = time.perf_counter()
    buf = _fetch_part_bytes(part["filename"])
    if len(buf) != expected:
        raise OSError(f"[DENSE] {part['filename']}: downloaded {len(buf)} bytes, "
                      f"expected {expected} ({part['rows']} rows x dim {dim})")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(buf)
    os.replace(tmp, path)
    logger.info(f"[DENSE] cached {part['filename']} locally "
                f"({len(buf) / 1e6:.0f} MB in {time.perf_counter() - t0:.1f}s)")
    # Evict other fingerprints' parts for this model: the store moved on, and
    # the cache lives in memory-backed /tmp on Cloud Run.
    model_root = os.path.join(_dense_cache_dir(), safe)
    for entry in os.listdir(model_root):
        if entry != os.path.basename(root):
            shutil.rmtree(os.path.join(model_root, entry), ignore_errors=True)
    return path




def read_vectors(model: str, rows: np.ndarray, index: DenseIndex,
                 dtype=np.float32,
                 coalesce_bytes: int = DEFAULT_COALESCE_BYTES,
                 local_cache: bool = False) -> np.ndarray:
    """Fetch specific global rows from the dense store.

    Local mode memory-maps each part (RSS = touched pages); GCS mode issues
    coalesced ranged reads — adjacent requested rows whose byte gap is at most
    ``coalesce_bytes`` share one request, over-reading the gap — unless
    ``local_cache`` is set, in which case each touched part is downloaded
    whole into the per-machine cache once and memory-mapped like local mode
    (see :func:`_cached_part_path`; for dense, many-part reads only).

    Args:
        model: Embedding model id.
        rows: Global row numbers (any order, duplicates allowed).
        index: The model's :class:`DenseIndex` (for dim + part layout).
        dtype: Output dtype (vectors are stored float16).
        coalesce_bytes: GCS-mode gap threshold.
        local_cache: GCS mode: serve parts from the whole-part cache.

    Returns:
        ``(len(rows), dim)`` array of ``dtype``, aligned to ``rows`` order.
    """
    rows = np.asarray(rows, dtype=np.int64)
    dim = index.dim
    row_bytes = dim * 2
    out = np.empty((len(rows), dim), dtype=dtype)
    if len(rows) == 0:
        return out

    order = np.argsort(rows, kind="stable")
    starts = np.array([p["start_row"] for p in index.parts], dtype=np.int64)
    ends = starts + np.array([p["rows"] for p in index.parts], dtype=np.int64)

    pos = 0
    while pos < len(order):
        row = rows[order[pos]]
        part_i = int(np.searchsorted(ends, row, side="right"))
        if part_i >= len(index.parts) or row < starts[part_i]:
            raise IndexError(f"[DENSE] {model}: row {row} outside all parts")
        part = index.parts[part_i]
        # All requested rows falling in this part (sorted order is contiguous).
        stop = pos
        while stop < len(order) and rows[order[stop]] < ends[part_i]:
            stop += 1
        local = rows[order[pos:stop]] - starts[part_i]
        dest = order[pos:stop]

        primary, _, mode, _ = data_io._resolve_paths(
            STORE_LOCATION, part["filename"])
        if mode == 'gcs' and local_cache:
            primary = _cached_part_path(model, part, index.store_fp, dim)
            mode = 'local'
        if mode == 'gcs':
            _read_part_ranged(part["filename"], local, dest, out, row_bytes,
                              dim, coalesce_bytes)
        else:
            mm = np.memmap(primary, dtype=np.float16, mode="r",
                           shape=(part["rows"], dim))
            out[dest] = mm[local]
            del mm
        pos = stop
    return out






def _read_part_ranged(filename: str, local_rows: np.ndarray,
                      dest: np.ndarray, out: np.ndarray, row_bytes: int,
                      dim: int, coalesce_bytes: int) -> None:
    """Fill ``out[dest]`` from one part via coalesced GCS ranged reads."""
    # Coalesce sorted local rows into runs whose byte gap <= coalesce_bytes.
    runs: list[tuple[int, int]] = []  # (first_row, last_row) inclusive
    run_start = prev = int(local_rows[0])
    for r in local_rows[1:]:
        r = int(r)
        if (r - prev - 1) * row_bytes <= coalesce_bytes:
            prev = r
        else:
            runs.append((run_start, prev))
            run_start = prev = r
    runs.append((run_start, prev))

    ranges = [(first * row_bytes, (last - first + 1) * row_bytes)
              for first, last in runs]
    blobs = data_io.read_byte_ranges(storage_location=STORE_LOCATION,
                                     filename=filename, ranges=ranges)

    run_i = 0
    first, last = runs[0]
    block = np.frombuffer(blobs[0], dtype=np.float16).reshape(-1, dim)
    for local, d in zip(local_rows, dest):
        local = int(local)
        while local > last:
            run_i += 1
            first, last = runs[run_i]
            block = np.frombuffer(blobs[run_i], dtype=np.float16).reshape(-1, dim)
        out[d] = block[local - first]






def get_corpus_mean(model: str, expected_fp: str | None = None,
                    reporter=None) -> tuple[np.ndarray, int, str]:
    """The validated global corpus mean for ``model``.

    Args:
        model: Embedding model id.
        expected_fp: A fingerprint the store must still match — raises
            :class:`CorpusMeanDrift` when the store moved (e.g. an
            embeddings_refresh appended a shard mid-chain).
        reporter: Optional status reporter for a rebuild's progress.

    Returns:
        ``(mean, count, fingerprint)``.
    """
    fp = store_fingerprint()
    if expected_fp is not None and fp != expected_fp:
        raise CorpusMeanDrift(
            f"embedding store changed (fingerprint {expected_fp[:12]} -> "
            f"{fp[:12]}) — the pinned corpus mean no longer matches")
    mean = load_corpus_mean(model, expected_fp=fp)
    if mean is None:
        manifest = ensure_dense_store(model, reporter=reporter)
        if not manifest.get("n_rows") or not manifest.get("mean_sum"):
            raise ValueError(f"[DENSE] {model}: no vectors in store")
        mean = (np.asarray(manifest["mean_sum"], dtype=np.float64)
                / manifest["n_rows"])
    count = load_manifest(model).get("n_rows", 0)
    return mean, int(count), fp
