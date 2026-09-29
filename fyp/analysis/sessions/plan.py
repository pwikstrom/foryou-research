"""The Sessions refresh planner: decide what a refresh must rebuild.

Compares the stored build's fingerprints (embedding shards, annotations,
coverage spec) with the current ones, works out whether an enrichment change
is local (appended vectors under the drift budget, mapped to the collections
that contain them) and returns the refresh plan — nothing, the touched
collections, or a full rebuild.
"""

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pa_compute

import fyp.core.data_io as data_io
from fyp.analysis import embedding_store, embeddings
from fyp.analysis.organize_datasets import COLLECTIONS_LABEL

# ---------------------------------------------------------------------------
# Enrichment-change scoping
#
# The store and annotation fingerprints tell the planner THAT enrichment
# moved; these helpers work out WHERE, so a routine append (a batch of newly
# annotated videos, embedded and folded in) re-segments only the collections
# that contain the touched videos instead of every covered collection.
# Measured without scoping: 50 annotations → 46 new vectors → 99 collections
# rebuilt, ~8 min, of which 15 collections actually held one of the videos.
#
# A change is LOCAL when: the embedding shards were only appended to (every
# shard the previous build saw is still present, byte-identical), the vectors
# appended since the last FULL build stay under the drift budget (untouched
# collections keep the corpus mean they were centred on — the drift is
# statistically negligible for an append, but it must not accumulate
# forever), and every touched video can be mapped to its collections. Anything
# else falls back to the full rebuild the planner always did.
# ---------------------------------------------------------------------------

REBASELINE_FRACTION_DEFAULT = 0.05
_SCOPE_MAX_ITEMS = 50_000


def rebaseline_fraction() -> float:
    """``[sessions] rebaseline_fraction`` — appended-vector share that forces a
    full rebuild (re-centres every collection on the current corpus mean)."""
    from fyp.core.fyp_config import fyp_cf

    cfg = fyp_cf.get("sessions", {})
    value = (
        cfg.get("rebaseline_fraction", REBASELINE_FRACTION_DEFAULT)
        if isinstance(cfg, dict)
        else REBASELINE_FRACTION_DEFAULT
    )
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return REBASELINE_FRACTION_DEFAULT


def shards_appended_only(old_entries, new_entries) -> bool:
    """True when every shard of ``old_entries`` is still in ``new_entries``
    with the same size and mtime — the store only grew."""
    if not old_entries:
        return False
    current = {str(e[0]): (int(e[1]), float(e[2])) for e in new_entries}
    for e in old_entries:
        if current.get(str(e[0])) != (int(e[1]), float(e[2])):
            return False
    return True


def new_vector_item_ids(index, old_count: int) -> set[str]:
    """Items whose dense row is at or past ``old_count`` — the rows appended
    since a build that saw ``old_count`` vectors (rows are never reordered).
    A re-embedded id that moved to a later row counts as new, which is the
    safe direction."""
    rows = np.asarray(index.rows)
    mask = rows >= int(old_count)
    if not mask.any():
        return set()
    picked = pa_compute.filter(index.ids, pa.array(mask.tolist(), type=pa.bool_()))
    return {str(v) for v in picked.to_pylist() if v is not None}


def annotation_corpus_max_ts() -> str | None:
    """The latest ``inference_ts`` in the annotation corpus (ISO, UTC), or None."""
    _, max_ts = annotation_items_changed_since(None)
    return max_ts


def annotation_items_changed_since(watermark: str | None) -> tuple[set[str] | None, str | None]:
    """Items annotated after ``watermark`` (their ``inference_ts`` is later),
    and the corpus's latest ``inference_ts``.

    Returns ``(None, max_ts)`` when there is no watermark to compare against:
    with no record of what the previous build saw, every row would count and
    the caller should fall back to a full rebuild — while recording ``max_ts``
    so the next build can scope. Rows without an ``inference_ts`` never count
    as changed (legacy rows).
    """
    fn = embeddings.ANNOTATIONS_FILE
    if not data_io.exists(storage_location=embeddings.STORE_LOCATION, filename=fn):
        return (set() if watermark else None), None
    wm_s: float | None = None
    if watermark:
        try:
            wm_s = (
                pd.Timestamp(watermark).tz_convert("UTC").timestamp()
                if pd.Timestamp(watermark).tzinfo
                else pd.Timestamp(watermark, tz="UTC").timestamp()
            )
        except (ValueError, TypeError):
            wm_s = None
    changed: set[str] = set()
    max_s: float | None = None
    # Columns are read straight from Arrow: to_pandas() on this file's batches
    # trips over the pandas metadata of its list-typed columns even when they
    # are not selected, and inference_ts is an epoch, not a datetime.
    for rb in data_io.iter_parquet_batches(
        storage_location=embeddings.STORE_LOCATION,
        filename=fn,
        columns=["item_id", "inference_ts"],
        batch_size=1_048_576,
    ):
        secs = _epoch_seconds(rb.column("inference_ts"))
        valid = ~np.isnan(secs)
        if valid.any():
            batch_max = float(secs[valid].max())
            max_s = batch_max if max_s is None else max(max_s, batch_max)
        if wm_s is not None:
            sel = valid & (secs > wm_s)
            if sel.any():
                ids = rb.column("item_id").to_pylist()
                changed.update(str(ids[i]) for i in np.flatnonzero(sel) if ids[i] is not None)
    max_iso = pd.Timestamp(max_s, unit="s", tz="UTC").isoformat() if max_s is not None else None
    if wm_s is None:
        return None, max_iso
    return changed, max_iso


def _epoch_seconds(col) -> np.ndarray:
    """An Arrow column of timestamps as float64 epoch seconds (NaN for null).

    The annotation corpus stores ``inference_ts`` as an int64 Unix epoch in
    seconds; a timestamp-typed column or a millisecond epoch is handled too.
    """
    if pa.types.is_timestamp(col.type):
        ns = pa_compute.cast(col, pa.timestamp("ns")).cast(pa.int64())
        arr = np.asarray(ns.to_numpy(zero_copy_only=False), dtype="float64")
        arr[np.asarray(ns.is_null().to_numpy(zero_copy_only=False))] = np.nan
        return arr / 1e9
    if pa.types.is_string(col.type) or pa.types.is_large_string(col.type):
        parsed = pd.to_datetime(pd.Series(col.to_pylist()), utc=True, errors="coerce")
        return np.array([t.timestamp() if pd.notna(t) else np.nan for t in parsed], dtype="float64")
    arr = np.asarray(col.to_numpy(zero_copy_only=False), dtype="float64")
    nulls = np.asarray(col.is_null().to_numpy(zero_copy_only=False), dtype=bool)
    arr[nulls] = np.nan
    finite = arr[~np.isnan(arr)]
    if finite.size and np.nanmedian(finite) > 1e11:  # milliseconds
        arr = arr / 1e3
    return arr


def collections_containing(
    item_ids: set[str], allow: set[str], max_items: int = _SCOPE_MAX_ITEMS
) -> set[str] | None:
    """The covered collections (``allow``) in which any of ``item_ids`` occurs,
    or None when the mapping is not worth doing (too many ids, or no activity
    file) — the caller then rebuilds everything."""
    if not item_ids:
        return set()
    if len(item_ids) > max_items:
        return None
    fn = f"{COLLECTIONS_LABEL}_recoded.parquet"
    if not data_io.exists(storage_location=embeddings.STORE_LOCATION, filename=fn):
        return None
    found: set[str] = set()
    for rb in data_io.iter_parquet_batches(
        storage_location=embeddings.STORE_LOCATION,
        filename=fn,
        columns=["collection_id", "item_id"],
        filters=[("item_id", "in", sorted(item_ids))],
        batch_size=1_048_576,
    ):
        found.update(str(v) for v in rb.column("collection_id").to_pylist() if v is not None)
    return found & set(allow)


def enrichment_change_scope(
    meta: dict | None,
    store_fp: str,
    n_vectors: int,
    annotations_fp: str,
    model: str,
    covered: set[str],
    fraction: float | None = None,
) -> dict:
    """Work out which collections this run's enrichment changes touch.

    Returns a dict with ``local`` (True when the change can be scoped),
    ``affected`` (the covered collections to re-segment; None when not
    local), ``reason`` (one line for the log), ``annotations_max_ts`` (the
    corpus watermark, when it was scanned) and the two counts. Pure apart
    from reading the shard listing, the dense index, the annotation corpus
    and the activity file — no writes.
    """
    out = {
        "local": False,
        "affected": None,
        "reason": "",
        "annotations_max_ts": None,
        "n_new_vectors": 0,
        "n_changed_annotations": 0,
    }
    if not isinstance(meta, dict) or not meta:
        out["reason"] = "no previous build"
        return out
    store_moved = bool(store_fp) and str(meta.get("store_fingerprint") or "") != store_fp
    anno_moved = (
        bool(annotations_fp) and str(meta.get("annotations_fingerprint") or "") != annotations_fp
    )
    if not store_moved and not anno_moved:
        out.update(local=True, affected=set(), reason="enrichment unchanged")
        return out

    affected: set[str] = set()
    if store_moved:
        old_shards = meta.get("store_shards")
        old_count = meta.get("corpus_mean_count")
        if not old_shards or old_count is None:
            out["reason"] = (
                "the previous build recorded no shard set — recording one "
                "now so the next append can be scoped"
            )
            return out
        if not shards_appended_only(old_shards, embedding_store.shard_entries()):
            out["reason"] = "embedding shards were rewritten or removed, not appended"
            return out
        baseline = int(meta.get("baseline_corpus_count") or old_count or 0)
        frac = rebaseline_fraction() if fraction is None else float(fraction)
        if baseline and n_vectors and (int(n_vectors) - baseline) / baseline > frac:
            out["reason"] = (
                f"{int(n_vectors) - baseline:,} vectors appended since the last "
                f"full build exceed the {frac:.0%} drift budget — re-baselining "
                "every collection on the current corpus mean"
            )
            return out
        index = embedding_store.load_index(model)
        if index is None:
            out["reason"] = "dense index unavailable"
            return out
        new_ids = new_vector_item_ids(index, int(old_count))
        out["n_new_vectors"] = len(new_ids)
        cids = collections_containing(new_ids, covered)
        if cids is None:
            out["reason"] = f"could not map {len(new_ids):,} new vectors to collections"
            return out
        affected |= cids

    if anno_moved:
        changed, max_ts = annotation_items_changed_since(meta.get("annotations_max_ts"))
        out["annotations_max_ts"] = max_ts
        if changed is None:
            out["reason"] = (
                "the previous build recorded no annotation watermark — "
                "recording one now so the next batch can be scoped"
            )
            return out
        out["n_changed_annotations"] = len(changed)
        cids = collections_containing(changed, covered)
        if cids is None:
            out["reason"] = f"could not map {len(changed):,} changed annotations to collections"
            return out
        affected |= cids

    out.update(local=True, affected=affected)
    out["reason"] = (
        f"{out['n_new_vectors']:,} new vector(s) and "
        f"{out['n_changed_annotations']:,} changed annotation(s) touch "
        f"{len(affected)} covered collection(s)"
    )
    return out


def compute_refresh_plan(
    discovered: list[tuple[str, int]],
    coverage: dict[str, list[list[str]]],
    meta: dict | None,
    params: dict,
    model: str,
    trend_cols: list[str],
    artifacts_exist: bool,
    plays_schema_ok: bool = True,
    scope: set[str] | None = None,
    store_fp: str = "",
    annotations_fp: str = "",
    enrichment_scope: dict | None = None,
) -> dict:
    """Decide what a sessions refresh must rebuild. Pure — no I/O.

    A collection is **stale** when its current fingerprint — coverage
    windows and in-window play count — differs from the one recorded in the
    meta's per-collection block (or it has no record). A collection recorded
    in the meta but absent from ``discovered``
    is **dropped** (it left every study, or its data is gone). Global
    invalidators escalate to a full rebuild because a merge would mix
    incompatible rows: a different embedding model, different segmentation
    params, a different trend-column set (sessions schema drift), a changed
    plays-artifact schema, or missing artifacts/meta (including a meta from
    before the per-collection block existed — the migration path).

    **Enrichment invalidators**. A changed embedding-store or
    annotation-corpus fingerprint also escalates to full. The per-collection
    fingerprint cannot see either: it is computed from the activity file,
    which carries no enrichment columns at all. An ``n_annotated`` term there
    is structurally always 0 for exactly that reason, so every ``stale_only``
    run would return "noop" no matter how many annotations landed (6,000 newly
    annotated items and 5,891 new vectors once rebuilt nothing). These two scalars
    are the coarse but honest substitute — they cost one stat call each, and
    being wrong in the cheap direction (an unnecessary full rebuild) is the
    point. Without an ``enrichment_scope`` proving the change local (see
    :func:`enrichment_change_scope`), the trade is deliberate: any enrichment
    change rebuilds every covered collection, not just the ones it touched.
    With one, only the touched collections join the refresh set.

    This supersedes the previous decision to tolerate corpus-mean drift. That
    reasoning — the mean over the full store is statistically stable across
    appends, so collections centred on the old mean coexist with the new —
    is still true of the *mean*, but the fingerprint was also the only
    available signal that the vector *set* grew, and new vectors change which
    plays the segmenter can see.

    Args:
        discovered: Current ``(cid, n_plays)`` fingerprint side.
        coverage: Current per-collection windows.
        meta: The existing ``sessions_meta.json`` payload (None when absent).
        params: The run's resolved segmentation params.
        model: The active embedding model id.
        trend_cols: The run's pinned trend-column list.
        artifacts_exist: Whether all published artifact files exist.
        plays_schema_ok: False when the on-disk plays artifact's columns
            differ from the current plays schema (mid-deploy drift).
        scope: Optional allow-list intersecting the refresh set (never the
            drop set — drops are corpus facts, not scoped requests).
        store_fp: The run's embedding-store fingerprint. Empty means the
            store could not be read (a transient corpus-mean failure yields
            ``""``), which must NOT escalate: a full rebuild with no vectors
            would replace real episodes with none.
        annotations_fp: The run's annotation-corpus fingerprint, with the
            same empty-means-unknown contract.
        enrichment_scope: Optional result of :func:`enrichment_change_scope`;
            when it marks the change local, only the affected collections are
            refreshed for an enrichment move.

    Returns:
        ``{"mode": "full"|"merge"|"noop", "reason": str,
           "refresh": [cid...], "drop": [cid...]}`` — ``refresh`` keeps the
        discovery order (biggest first); in full mode it is every discovered
        collection and ``drop`` is empty (a full publish overwrites).
    """
    all_cids = [cid for cid, _ in discovered]

    def _full(reason: str) -> dict:
        return {"mode": "full", "reason": reason, "refresh": all_cids, "drop": []}

    if not artifacts_exist:
        return _full("artifacts missing")
    if not isinstance(meta, dict) or not meta:
        return _full("no meta")
    known = meta.get("collections")
    if not isinstance(known, dict):
        return _full("meta has no per-collection block (pre-upgrade build)")
    if str(meta.get("embedding_model") or "") != str(model):
        return _full(f"embedding model changed ({meta.get('embedding_model')} -> {model})")
    if meta.get("params") != params:
        return _full("segmentation params changed")
    if sorted(meta.get("trend_vars") or []) != sorted(trend_cols):
        return _full("trend-column set changed (sessions schema drift)")
    if not plays_schema_ok:
        return _full("plays artifact schema changed (deploy drift)")
    # Enrichment moved. With a scope that proves the change local (see
    # enrichment_change_scope) the touched collections join the refresh set;
    # without one, the historical answer — rebuild everything — stands.
    scoped = (
        isinstance(enrichment_scope, dict)
        and enrichment_scope.get("local")
        and enrichment_scope.get("affected") is not None
    )
    touched: set[str] = set(enrichment_scope["affected"]) if scoped else set()
    enrichment_moved = False
    if store_fp and str(meta.get("store_fingerprint") or "") != store_fp:
        if not scoped:
            return _full("embedding store changed (new or rewritten shards)")
        enrichment_moved = True
    if annotations_fp and str(meta.get("annotations_fingerprint") or "") != annotations_fp:
        if not scoped:
            return _full("annotation corpus changed")
        enrichment_moved = True

    refresh: list[str] = []
    n_stale = 0
    for cid, n_plays in discovered:
        rec = known.get(cid)
        stale = (
            not isinstance(rec, dict)
            or rec.get("windows") != coverage.get(cid, [])
            or int(rec.get("n_plays", -1)) != int(n_plays)
        )
        if stale:
            n_stale += 1
        if stale or (enrichment_moved and cid in touched):
            refresh.append(cid)
    if scope is not None:
        refresh = [cid for cid in refresh if cid in scope]
    drop = sorted(set(known) - {cid for cid, _ in discovered})

    if not refresh and not drop:
        if enrichment_moved:
            # Nothing to segment, but the fingerprints must be recorded or
            # every later run re-derives this same scope: an empty merge
            # publishes the meta and nothing else.
            return {
                "mode": "merge",
                "reason": "new enrichment touched no covered collection — "
                "recording the fingerprints",
                "refresh": [],
                "drop": [],
            }
        return {"mode": "noop", "reason": "all collections up to date", "refresh": [], "drop": []}
    reason = f"{n_stale} stale, {len(drop)} removed"
    if enrichment_moved:
        n_touched = len([cid for cid in refresh if cid in touched])
        reason = f"{n_stale} stale, {n_touched} touched by new enrichment, {len(drop)} removed"
    return {"mode": "merge", "reason": reason, "refresh": refresh, "drop": drop}
