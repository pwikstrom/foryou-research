"""Build and publish the Sessions artifacts.

The artifact schemas, one self-chaining batch's build (segment a chunk of
collections, attach play texts, write shard files), the sweep of stale run
files, and the publish / merge-publish that turns a run's shards into the
live ``sessions_index`` / episodes / windows / plays artifacts;
``build_artifacts`` runs the whole build in one process.
"""

import time

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pa_compute

import fyp.core.data_io as data_io
from fyp.analysis import embedding_store, embeddings
from fyp.analysis.sessions import inputs, segment
from fyp.core.logging_setup import get_logger

logger = get_logger(__name__)


def sessions_schema(trend_cols: list[str] | None = None) -> dict[str, pa.DataType]:
    """The sessions-index schema for one build's trend-variable set.

    The base columns plus ``search_text`` and a ``vmin_``/``vmax_`` float pair
    per trend variable (and for dwell). All links of one chained run must use
    the same ``trend_cols`` or the shard concat at publish would see
    mismatched schemas — the worker pins the list at link 0.
    """
    extra: dict[str, pa.DataType] = {"search_text": pa.string()}
    for col in list(trend_cols or []) + ["dwell_s"]:
        extra.setdefault(f"vmin_{col}", pa.float64())
        extra.setdefault(f"vmax_{col}", pa.float64())
    return {**segment._SESSIONS_SCHEMA, **extra}


_WINDOWS_SCHEMA: dict[str, pa.DataType] = {
    "collection_id": pa.string(),
    "session_id": pa.string(),
    "window_idx": pa.int16(),
    "start_ts": pa.string(),
    "end_ts": pa.string(),
    "duration_min": pa.float32(),
    "n_distinct": pa.int16(),
    "mean_cosdist": pa.float32(),
    "entropy_norm": pa.float32(),
    "dominant_niche": pa.string(),
    "member_item_ids": pa.large_list(pa.string()),
    "member_ts": pa.large_list(pa.string()),
    "member_dwell_s": pa.large_list(pa.float32()),
}

_EPISODES_SCHEMA: dict[str, pa.DataType] = {
    "collection_id": pa.string(),
    "session_id": pa.string(),
    "episode_idx": pa.int16(),
    "start_ts": pa.string(),
    "end_ts": pa.string(),
    "duration_min": pa.float32(),
    "n_plays": pa.int32(),
    "n_distinct": pa.int32(),
    "repeat_rate": pa.float32(),
    "n_interleaved": pa.int32(),
    "n_skipped": pa.int32(),
    "focus": pa.float32(),
    "diameter": pa.float32(),
    "step_mean": pa.float32(),
    "straightness": pa.float32(),
    "spectral_entropy_bits": pa.float32(),
    "effective_rank": pa.float32(),
    "direction_p": pa.float32(),
    "dominant_niche": pa.string(),
    "dominant_niche_share": pa.float32(),
    "n_niches": pa.int16(),
    "n_authors": pa.int16(),
    "dominant_author_share": pa.float32(),
    "advertising": pa.string(),
    "advertising_share": pa.float32(),
    "mean_political": pa.float32(),
    "mean_sensitivity": pa.float32(),
    "member_item_ids": pa.large_list(pa.string()),
    "member_ts": pa.large_list(pa.string()),
    "member_dwell_s": pa.large_list(pa.float32()),
    "member_rolling_cosdist": pa.large_list(pa.float32()),
}

_PLAYS_SCHEMA: dict[str, pa.DataType] = {
    "collection_id": pa.string(),
    "session_id": pa.string(),
    "item_id": pa.string(),
    "ts": pa.timestamp("us"),
    "play_duration": pa.float64(),
    "source_platform": pa.string(),
    # Per-item display text, baked in at build time so the detail endpoint
    # never has to pushdown-read the corpus annotation/scrape parquets (those
    # files are not clustered by item_id, so such a "pushdown" decodes the
    # whole text column per request). Null on rows whose item has no text.
    "story": pa.string(),
    "desc": pa.string(),
    "hashtags": pa.string(),
}

# The plays artifact stores display text capped at the same length the detail
# endpoint ships (api_sessions_routes._STORY_CAP): the artifact is a serving
# cache, not an archive — full text stays in the annotation/scrape parquets.
PLAY_TEXT_CAP = 400
_PLAY_TEXT_COLS = ("story", "desc", "hashtags")


def _capped_text(value) -> str | None:
    """A trimmed, ``PLAY_TEXT_CAP``-capped string, or None for empty cells.

    List cells (``desc_hashtags``) are space-joined before capping.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, np.ndarray)):
        parts = [str(v).strip() for v in value if v is not None and str(v).strip()]
        value = " ".join(parts)
    else:
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        value = str(value).strip()
    if not value:
        return None
    return value[:PLAY_TEXT_CAP] + "…" if len(value) > PLAY_TEXT_CAP else value


def attach_play_texts(
    plays: pd.DataFrame, feat: pd.DataFrame, stories: dict[str, str]
) -> pd.DataFrame:
    """Attach capped ``story``/``desc``/``hashtags`` columns to a plays frame.

    Per-item text mapped onto the play rows (repeated plays repeat the text —
    parquet compression within the (collection, ts)-sorted row groups absorbs
    that). Arrow-backed strings, so a link-0-sized batch stays in contiguous
    buffers rather than one Python str per cell.

    Args:
        plays: A :func:`load_plays`-shaped frame.
        feat: :func:`load_video_features` result WITH scrape text
            (``include_scrape_text=True``), item_id-indexed.
        stories: :func:`load_story_texts` result (item_id → story).

    Returns:
        ``plays`` with the three text columns added (all-null when the
        sources are empty).
    """
    if plays is None or not len(plays):
        return plays
    item_ids = [str(i) for i in plays["item_id"].drop_duplicates()]
    text = {
        "story": [_capped_text(stories.get(iid) if stories else None) for iid in item_ids],
        "desc": [None] * len(item_ids),
        "hashtags": [None] * len(item_ids),
    }
    if feat is not None and len(feat):
        sub = feat.reindex(item_ids)
        for col, src in (("desc", "desc"), ("hashtags", "desc_hashtags")):
            if src in sub.columns:
                text[col] = [_capped_text(v) for v in sub[src]]
    text_df = pd.DataFrame({"item_id": pd.array(item_ids, dtype="string[pyarrow]")})
    for col in _PLAY_TEXT_COLS:
        text_df[col] = pd.array(text[col], dtype="string[pyarrow]")
    plays = plays.copy()
    plays["item_id"] = plays["item_id"].astype("string[pyarrow]")
    return plays.merge(text_df, on="item_id", how="left")


def plays_table(plays: pd.DataFrame) -> pa.Table:
    """One batch's play rows as an arrow Table in the plays-artifact schema.

    Rows are sorted by (collection_id, ts) so the published file's row-group
    ``collection_id`` stats stay tight (each chunk owns a disjoint collection
    set, so per-shard sorting yields a globally clustered artifact).

    Args:
        plays: A :func:`load_plays`-shaped frame (``_ts`` parsed, play rows
            only), optionally carrying the :func:`attach_play_texts` text
            columns (absent ones publish as all-null). An empty frame yields
            an empty, schema-correct table.
    """
    if plays is None or not len(plays):
        return pa.table({col: pa.array([], type=typ) for col, typ in _PLAYS_SCHEMA.items()})
    df = plays.sort_values(["collection_id", "_ts"])
    data = {
        "collection_id": pa.array(df["collection_id"].astype("string"), type=pa.string()),
        "session_id": pa.array(df["session_id"].astype("string"), type=pa.string()),
        "item_id": pa.array(df["item_id"].astype("string"), type=pa.string()),
        "ts": pa.array(df["_ts"]).cast(pa.timestamp("us")),
        "play_duration": pa.array(
            pd.to_numeric(df["play_duration"], errors="coerce"), type=pa.float64()
        ),
        "source_platform": pa.array(df["source_platform"].astype("string"), type=pa.string()),
    }
    for col in _PLAY_TEXT_COLS:
        data[col] = (
            pa.array(df[col].astype("string"), type=pa.string())
            if col in df.columns
            else pa.nulls(len(df), type=pa.string())
        )
    return pa.table(data)


def _arrow_frame(rows: list[dict], schema: dict[str, pa.DataType]) -> pd.DataFrame:
    """Build an all-ArrowDtype DataFrame from row dicts with an explicit schema."""
    data = {}
    for col, typ in schema.items():
        values = [r.get(col) for r in rows]
        data[col] = pd.array(
            pa.array(values, type=typ),
            dtype=pd.ArrowDtype(typ),
        )
    return pd.DataFrame(data)


def build_batch(
    cids: list[str],
    model: str,
    corpus_mean: np.ndarray | None,
    index=None,
    params: dict | None = None,
    reporter=None,
    max_vectors: int = inputs.MAX_VECTORS_PER_LINK,
    trend_cols: list[str] | None = None,
    coverage: dict[str, list[list[str]]] | None = None,
    workers=None,
):
    """Segment one batch of collections against the dense embedding sidecar.

    Peak memory is O(batch): only the batch's plays, features, id sets and
    vectors are resident. The vectors are centred on the **global**
    ``corpus_mean`` — never a batch mean — so any partition of the corpus
    into batches yields identical rows (centring and normalisation are
    per-row; everything downstream is within-session; sessions never cross a
    collection boundary).

    Args:
        cids: The batch's collection ids.
        model: Embedding model id.
        corpus_mean: The global corpus mean (None when no store exists —
            sessions still get quality rows, with no episodes/windows).
        index: The model's DenseIndex (None: loaded here / no store).
        params: Segmentation parameter overrides.
        reporter: Optional status reporter (cancellation checks per
            collection).
        max_vectors: Tier gate — a batch whose embedded-distinct union
            exceeds this loads vectors per collection (tier 2) instead of
            once for the union (tier 1).
        trend_cols: Numeric feature columns for the session-extreme
            ``vmin_``/``vmax_`` columns (None resolves the live list — a
            chained worker must pass the list pinned at link 0 instead).
        coverage: Optional per-collection date-window spec (see
            :func:`compute_coverage_spec`). When given, each collection's
            plays are restricted to its intervals before segmentation — a
            collection absent from the spec contributes nothing.
        workers: Segmentation worker processes (see :func:`resolve_workers`;
            None reads the config). Affects wall time only, never the rows.

    Returns:
        ``(session_rows, episode_rows, window_rows, plays, stats)`` — ``plays``
        is the batch's loaded play frame (the plays-artifact shard source, so
        the worker never re-reads it); all None when cancelled mid-batch.
        ``stats`` carries the phase timings (``t_load`` / ``t_vectors`` /
        ``t_segment`` seconds) plus ``workers`` and ``units``.
    """
    p = {**inputs.default_params(), **(params or {})}
    n_workers = segment.resolve_workers(workers)
    log = reporter.log if reporter is not None else None
    stats = {
        "n_plays": 0,
        "n_vectors": 0,
        "tier": 1,
        "workers": n_workers,
        "units": 0,
        "unit_max": 0.0,
        "unit_cpu": 0.0,
        "t_load": 0.0,
        "t_vectors": 0.0,
        "t_segment": 0.0,
    }
    _t = time.perf_counter()
    plays = inputs.load_plays(cids)
    if coverage is not None and not plays.empty:
        keep = pd.Series(False, index=plays.index)
        for cid in plays["collection_id"].drop_duplicates():
            windows = coverage.get(str(cid))
            if not windows:
                continue
            sel = (plays["collection_id"] == cid).to_numpy(dtype=bool)
            keep[sel] = inputs.coverage_mask(plays.loc[sel, "_ts"], windows).to_numpy()
        plays = plays[keep]
    stats["n_plays"] = int(len(plays))
    if plays.empty:
        stats["t_load"] = time.perf_counter() - _t
        return [], [], [], plays, stats

    if trend_cols is None:
        trend_cols = inputs.trend_numeric_columns()
    batch_ids = [str(i) for i in plays["item_id"].drop_duplicates()]
    feat = inputs.load_video_features(
        item_ids=set(batch_ids), extra_map_cols=trend_cols, include_scrape_text=True
    )
    stories = inputs.load_story_texts(set(batch_ids))
    id_sets = inputs.enrichment_id_sets(model, item_ids=set(batch_ids), include_embedded=False)
    # Bake the per-item display text into the plays frame here, so both the
    # chained worker and the in-process driver publish it with no extra reads.
    plays = attach_play_texts(plays, feat, stories)

    if index is None and corpus_mean is not None:
        index = embedding_store.load_index(model)
    if index is not None and corpus_mean is not None:
        _, found = index.lookup(batch_ids)
        n_union = int(found.sum())
    else:
        found = np.zeros(len(batch_ids), dtype=bool)
        n_union = 0
    stats["n_vectors"] = n_union
    stats["t_load"] = time.perf_counter() - _t
    tier1 = n_union <= max_vectors
    use_cache = inputs.vector_cache_enabled()
    stats["vector_cache"] = int(use_cache)

    session_rows: list[dict] = []
    episode_rows: list[dict] = []
    window_rows: list[dict] = []

    if tier1:
        _t = time.perf_counter()
        embedded_ids = [i for i, f in zip(batch_ids, found) if f]
        id2local, U = (
            inputs.load_directional_block(
                model, embedded_ids, corpus_mean, index, local_cache=use_cache
            )
            if n_union
            else ({}, np.empty((0, 1), np.float32))
        )
        id_sets["embedded"] = set(id2local)
        stats["t_vectors"] += time.perf_counter() - _t
        _t = time.perf_counter()
        out = segment._segment_collections(
            cids,
            plays,
            id2local,
            U,
            feat,
            id_sets,
            p,
            trend_cols,
            stories,
            n_workers,
            reporter=reporter,
            log=log,
        )
        stats["t_segment"] += time.perf_counter() - _t
        if out is None:
            return None, None, None, None, None
        session_rows, episode_rows, window_rows, unit_secs = out
        stats["units"] += len(unit_secs)
        stats["unit_max"] = max(stats["unit_max"], max(unit_secs, default=0.0))
        stats["unit_cpu"] += sum(unit_secs)
    else:
        # Tier 2: the union exceeds the budget — load and free per collection.
        stats["tier"] = 2
        for cid in cids:
            _t = time.perf_counter()
            cplays = plays[plays["collection_id"] == cid]
            c_ids = [str(i) for i in cplays["item_id"].drop_duplicates()]
            id2local, U = inputs.load_directional_block(
                model, c_ids, corpus_mean, index, local_cache=use_cache
            )
            id_sets["embedded"] = set(id2local)
            stats["t_vectors"] += time.perf_counter() - _t
            _t = time.perf_counter()
            out = segment._segment_collections(
                [cid],
                cplays,
                id2local,
                U,
                feat,
                id_sets,
                p,
                trend_cols,
                stories,
                n_workers,
                reporter=reporter,
                log=log,
            )
            stats["t_segment"] += time.perf_counter() - _t
            if out is None:
                return None, None, None, None, None
            srows, erows, wrows, unit_secs = out
            session_rows.extend(srows)
            episode_rows.extend(erows)
            window_rows.extend(wrows)
            stats["units"] += len(unit_secs)
            stats["unit_max"] = max(stats["unit_max"], max(unit_secs, default=0.0))
            stats["unit_cpu"] += sum(unit_secs)
            del U, id2local
    segment._FORK_CTX.clear()
    return session_rows, episode_rows, window_rows, plays, stats


def format_batch_timing(chunk: int, n_collections: int, stats: dict) -> str:
    """One ``[TIMING]`` line per batch: where a link's wall time went."""
    return (
        f"[TIMING] sessions_link chunk={chunk} collections={n_collections} "
        f"plays={stats.get('n_plays', 0)} tier={stats.get('tier', 1)} "
        f"load={stats.get('t_load', 0.0):.1f}s "
        f"vectors={stats.get('t_vectors', 0.0):.1f}s "
        f"vcache={stats.get('vector_cache', 0)} "
        f"segment={stats.get('t_segment', 0.0):.1f}s "
        f"workers={stats.get('workers', 1)} units={stats.get('units', 0)} "
        f"unit_max={stats.get('unit_max', 0.0):.1f}s "
        f"unit_cpu={stats.get('unit_cpu', 0.0):.1f}s"
    )


def _publish_type(typ: pa.DataType) -> pa.DataType:
    """Downgrade large_list to list for the on-disk schema.

    The historical save_parquet path applied the same downgrade
    (types.downgrade_large_arrow_columns), so on-disk files always carried
    plain list — keep that contract for the read side. NOTE for future
    consumers: pandas 2.2.x `explode()` silently no-ops on large_list;
    api_sessions_routes reads member lists with `list(value)`, never explode.
    """
    return pa.list_(typ.value_type) if pa.types.is_large_list(typ) else typ


def _arrow_table(rows: list[dict], schema: dict[str, pa.DataType]) -> pa.Table:
    """Rows -> pyarrow Table in the published (list, not large_list) schema."""
    return pa.table(
        {
            col: pa.array([r.get(col) for r in rows], type=_publish_type(typ))
            for col, typ in schema.items()
        }
    )


def shard_filename(kind: str, run_id: str, chunk: int) -> str:
    """Per-link intermediate shard name (deterministic: retries overwrite)."""
    return f"{inputs.SHARD_PREFIXES[kind]}{run_id}__{chunk:04d}.parquet"


def write_batch_shards(
    run_id: str,
    chunk: int,
    session_rows: list[dict],
    episode_rows: list[dict],
    window_rows: list[dict],
    trend_cols: list[str] | None = None,
    plays: pd.DataFrame | None = None,
) -> None:
    """Persist one link's rows as its four deterministic shards.

    ``trend_cols`` must be the same list :func:`build_batch` produced the rows
    with (the worker pins it at link 0), so every shard of a run shares one
    sessions schema. ``plays`` is the batch's play frame from
    :func:`build_batch` (None writes an empty, schema-correct plays shard).
    """
    for kind, schema, rows in (
        ("sessions", sessions_schema(trend_cols), session_rows),
        ("episodes", _EPISODES_SCHEMA, episode_rows),
        ("windows", _WINDOWS_SCHEMA, window_rows),
    ):
        tbl = _arrow_table(rows, schema)
        data_io.write_parquet_stream(
            storage_location=inputs.ARTIFACT_LOCATION,
            filename=shard_filename(kind, run_id, chunk),
            batches=[tbl],
            schema=tbl.schema,
        )
    ptbl = plays_table(plays)
    data_io.write_parquet_stream(
        storage_location=inputs.ARTIFACT_LOCATION,
        filename=shard_filename("plays", run_id, chunk),
        batches=[ptbl],
        schema=ptbl.schema,
    )


def sweep_stale_run_files(current_run_id: str) -> None:
    """Remove intermediate files left by abandoned runs (other run ids)."""
    prefixes = tuple(inputs.SHARD_PREFIXES.values()) + (inputs.PROGRESS_PREFIX,)
    for fn in data_io.listdir(storage_location=inputs.ARTIFACT_LOCATION):
        if not fn.startswith(prefixes):
            continue
        if f"__{current_run_id}__" in fn or fn.endswith(f"{current_run_id}.json"):
            continue
        try:
            data_io.remove(storage_location=inputs.ARTIFACT_LOCATION, filename=fn)
        except Exception:
            pass


def publish_artifacts(
    run_id: str,
    n_chunks: int,
    expected: dict,
    meta: dict,
    reporter=None,
    covered_collections: int | None = None,
    total_collections: int | None = None,
) -> dict:
    """Concatenate the run's shards into the three artifact files + meta.

    Publish order matters: ``sessions_index.parquet`` LAST — its size:mtime
    fingerprint is the tab's freshness gate, so episodes/windows must land
    first or the index would briefly advertise sessions whose detail rows
    don't exist yet. Shards are deleted only after every check below passed.

    Three independent completeness checks, because they fail differently:

    * **Coverage** (``covered_collections`` vs ``total_collections``) — the run
      must have segmented every collection discovery found. This is the one
      that matters under duplicate chains: a Cloud Tasks retry re-delivers the
      same task_args, so concurrent chains share a ``run_id``. The first to
      finish publishes and deletes BOTH the shards and the progress file, so a
      trailing chain rebuilds a progress file covering only its remaining
      chunks and then agrees with its own truncated shard set. Row counts are
      self-consistent in that case and wave it through — a half-corpus artifact
      silently replacing a complete one (observed in production).
      Coverage cannot be reset that way.
    * **Shard-set completeness** — every chunk 0..n_chunks-1 must be present.
    * **Row counts** (``expected``) — catches a shard that failed to write.

    Args:
        run_id: The run whose shards to publish.
        n_chunks: Number of links (shards per kind).
        expected: ``{"sessions": n, "episodes": n, "windows": n}`` totals.
        meta: The ``sessions_meta.json`` payload (counts already in it).
        reporter: Optional status reporter.
        covered_collections: Collections actually segmented by this run.
        total_collections: Collections discovery found at link 0.

    Returns:
        ``meta`` (persisted).

    Raises:
        RuntimeError: when the run is incomplete or a row count disagrees.
            Shards are left for inspection and nothing is published, so the
            previous artifacts stay intact.
    """
    if (
        covered_collections is not None
        and total_collections is not None
        and int(covered_collections) != int(total_collections)
    ):
        raise RuntimeError(
            f"publish: run {run_id} covered {covered_collections} of "
            f"{total_collections} collections — refusing to publish a partial "
            f"artifact (a concurrent chain sharing this run_id most likely "
            f"published and cleaned up first). Shards kept for inspection."
        )

    for kind, final in (
        ("plays", inputs.PLAYS_FILE),
        ("episodes", inputs.EPISODES_FILE),
        ("windows", inputs.WINDOWS_FILE),
        ("sessions", inputs.SESSIONS_FILE),
    ):
        all_shards = [shard_filename(kind, run_id, k) for k in range(n_chunks)]
        shards = [
            s
            for s in all_shards
            if data_io.exists(storage_location=inputs.ARTIFACT_LOCATION, filename=s)
        ]
        if not shards:
            if kind == "plays":
                # A run started before the plays shard existed (mid-run
                # deploy): publish the three original artifacts; the read
                # side falls back to the consolidated activity file.
                if reporter is not None:
                    reporter.log(
                        "No 'plays' shards for this run — skipping "
                        "the plays artifact (pre-upgrade run)."
                    )
                continue
            raise RuntimeError(f"publish: no '{kind}' shards found for run {run_id}")
        if len(shards) != len(all_shards):
            if kind == "plays":
                # Mid-run deploy: early links predate the plays shard. The
                # other three kinds are complete, so publish them and let the
                # read side fall back for plays.
                if reporter is not None:
                    reporter.log(
                        f"Incomplete 'plays' shard set "
                        f"({len(shards)}/{n_chunks}) — skipping the "
                        f"plays artifact (pre-upgrade links)."
                    )
                continue
            raise RuntimeError(
                f"publish: run {run_id} has an incomplete '{kind}' shard set "
                f"({len(all_shards) - len(shards)} of {n_chunks} missing) — "
                f"refusing to publish. Another chain sharing this run_id most "
                f"likely published first."
            )
        if kind == "plays":
            # A schema-widening deploy mid-run leaves early shards without the
            # newer columns; concat binds every shard to the first shard's
            # schema, so a mixed set cannot publish. Same degradation as an
            # absent set: skip plays, read side falls back.
            col_sets = set()
            for s in shards:
                cols = data_io.get_parquet_columns(
                    storage_location=inputs.ARTIFACT_LOCATION, filename=s
                )
                col_sets.add(tuple(sorted(cols or [])))
            if len(col_sets) > 1:
                if reporter is not None:
                    reporter.log(
                        "Mixed 'plays' shard schemas (mid-run deploy) "
                        "— skipping the plays artifact."
                    )
                continue
        n = data_io.concat_parquet_files(
            src_storage_location=inputs.ARTIFACT_LOCATION,
            src_filenames=shards,
            dst_storage_location=inputs.ARTIFACT_LOCATION,
            dst_filename=final,
            # Small row groups keep the plays file's collection_id stats
            # tight, so the detail endpoint's pushdown prunes.
            batch_size=inputs.PLAYS_ROW_GROUP if kind == "plays" else 131_072,
        )
        if kind in expected and n != int(expected[kind]):
            raise RuntimeError(
                f"publish: '{kind}' row count {n} != expected {expected[kind]} "
                f"— shards kept for inspection, artifact NOT trusted"
            )
        if reporter is not None:
            reporter.log(f"Published {final} ({n:,} rows from {len(shards)} shard(s))")

    data_io.save_json(
        data=meta, storage_location=inputs.ARTIFACT_LOCATION, filename=inputs.META_FILE
    )
    for kind in inputs.SHARD_PREFIXES:
        for k in range(n_chunks):
            fn = shard_filename(kind, run_id, k)
            if data_io.exists(storage_location=inputs.ARTIFACT_LOCATION, filename=fn):
                data_io.remove(storage_location=inputs.ARTIFACT_LOCATION, filename=fn)
    return meta


def _target_schema(kind: str, trend_cols: list[str]) -> pa.Schema:
    """The published arrow schema for one artifact kind."""
    if kind == "plays":
        return plays_table(None).schema
    dict_schema = {
        "sessions": sessions_schema(trend_cols),
        "episodes": _EPISODES_SCHEMA,
        "windows": _WINDOWS_SCHEMA,
    }[kind]
    return _arrow_table([], dict_schema).schema


def _align_batch(rb: pa.RecordBatch, schema: pa.Schema) -> pa.RecordBatch:
    """Reorder/cast a batch to ``schema`` (no-op when it already matches)."""
    if rb.schema.equals(schema):
        return rb
    return rb.select(schema.names).cast(schema)


def merge_publish_artifacts(
    run_id: str,
    n_chunks: int,
    refresh_cids: list[str],
    drop_cids: list[str],
    expected: dict,
    meta: dict,
    trend_cols: list[str],
    reporter=None,
    covered_collections: int | None = None,
) -> dict:
    """Fold the run's shards into the existing artifacts, replacing rows.

    The incremental counterpart of :func:`publish_artifacts`: instead of the
    shards *becoming* the artifacts, each artifact is rewritten as (its
    existing rows minus every refreshed/dropped collection) + the run's shard
    rows. Streaming end to end — peak memory is one record batch. The write
    itself stages to a tempfile and lands in one move
    (:func:`fyp.core.data_io.write_parquet_stream`), and the publish order keeps
    ``sessions_index.parquet`` last, so the read side's freshness gate holds.

    Guard differences from the full publish: coverage/row-count totals are
    the **targeted set's**, not the corpus's; every kind's shard set must be
    complete (there is no plays grace-skip — a merge that skipped plays would
    strand stale rows for the refreshed collections, and setup escalates
    schema drift to a full rebuild before a merge run ever starts); and the
    new-row counts are verified from the shard footers **before** any
    artifact is touched.

    Args:
        run_id: The run whose shards to fold in.
        n_chunks: Number of links (shards per kind).
        refresh_cids: Collections this run re-segmented (their old rows go).
        drop_cids: Collections to remove without replacement (left every
            study, or vanished from the data).
        expected: ``{"sessions": n, ...}`` NEW-row totals from the run.
        meta: The ``sessions_meta.json`` payload; its ``n_*`` counts are
            overwritten with the merged totals here.
        trend_cols: The run's pinned trend columns (sessions schema).
        reporter: Optional status reporter.
        covered_collections: Collections actually segmented by this run —
            compared against ``len(refresh_cids)``.

    Returns:
        ``meta`` (persisted, with merged counts).

    Raises:
        RuntimeError: incomplete run, count mismatch, or schema mismatch.
            Nothing is published in that case; the artifacts stay intact.
    """
    total = len(refresh_cids)
    if covered_collections is not None and int(covered_collections) != total:
        raise RuntimeError(
            f"merge publish: run {run_id} covered {covered_collections} of "
            f"{total} targeted collections — refusing to publish a partial "
            f"merge. Shards kept for inspection."
        )

    remove_ids = pa.array(
        sorted({str(c) for c in refresh_cids} | {str(c) for c in drop_cids}), type=pa.string()
    )
    kinds = (
        ("plays", inputs.PLAYS_FILE),
        ("episodes", inputs.EPISODES_FILE),
        ("windows", inputs.WINDOWS_FILE),
        ("sessions", inputs.SESSIONS_FILE),
    )

    # Validate every kind BEFORE touching any artifact: complete shard set,
    # new-row totals (from footers — no data read), old-artifact schema.
    shard_sets: dict[str, list[str]] = {}
    for kind, final in kinds:
        shards = [shard_filename(kind, run_id, k) for k in range(n_chunks)]
        missing = [
            s
            for s in shards
            if not data_io.exists(storage_location=inputs.ARTIFACT_LOCATION, filename=s)
        ]
        if missing:
            raise RuntimeError(
                f"merge publish: run {run_id} has an incomplete '{kind}' "
                f"shard set ({len(missing)} of {n_chunks} missing) — "
                f"refusing to publish."
            )
        new_rows = sum(
            data_io.get_parquet_num_rows(storage_location=inputs.ARTIFACT_LOCATION, filename=s) or 0
            for s in shards
        )
        if kind in expected and new_rows != int(expected[kind]):
            raise RuntimeError(
                f"merge publish: '{kind}' shard rows {new_rows} != expected "
                f"{expected[kind]} — artifacts untouched, shards kept."
            )
        schema = _target_schema(kind, trend_cols)
        old_cols = data_io.get_parquet_columns(
            storage_location=inputs.ARTIFACT_LOCATION, filename=final
        )
        if old_cols is not None and sorted(old_cols) != sorted(schema.names):
            raise RuntimeError(
                f"merge publish: existing {final} columns differ from the "
                f"current schema (mid-run deploy?) — setup should have "
                f"escalated to a full rebuild. Shards kept."
            )
        shard_sets[kind] = shards

    merged_counts: dict[str, int] = {}
    for kind, final in kinds:
        schema = _target_schema(kind, trend_cols)
        old_exists = data_io.exists(storage_location=inputs.ARTIFACT_LOCATION, filename=final)
        counts = {"old_kept": 0, "new": 0}

        def _batches(kind=kind, final=final, schema=schema, old_exists=old_exists, counts=counts):
            if old_exists:
                idx = schema.names.index("collection_id")
                for rb in data_io.iter_parquet_batches(
                    storage_location=inputs.ARTIFACT_LOCATION,
                    filename=final,
                    batch_size=inputs.PLAYS_ROW_GROUP if kind == "plays" else 131_072,
                ):
                    rb = _align_batch(rb, schema)
                    mask = pa_compute.invert(pa_compute.is_in(rb.column(idx), value_set=remove_ids))
                    kept = rb.filter(pa_compute.fill_null(mask, True))
                    if kept.num_rows:
                        counts["old_kept"] += kept.num_rows
                        yield kept
            for s in shard_sets[kind]:
                for rb in data_io.iter_parquet_batches(
                    storage_location=inputs.ARTIFACT_LOCATION,
                    filename=s,
                    batch_size=inputs.PLAYS_ROW_GROUP if kind == "plays" else 131_072,
                ):
                    counts["new"] += rb.num_rows
                    yield _align_batch(rb, schema)

        n = data_io.write_parquet_stream(
            storage_location=inputs.ARTIFACT_LOCATION,
            filename=final,
            batches=_batches(),
            schema=schema,
        )
        if n != counts["old_kept"] + counts["new"]:
            raise RuntimeError(
                f"merge publish: '{kind}' wrote {n} rows != kept "
                f"{counts['old_kept']} + new {counts['new']}"
            )
        merged_counts[kind] = n
        if reporter is not None:
            reporter.log(
                f"Merged {final}: kept {counts['old_kept']:,} rows, "
                f"replaced/added {counts['new']:,} "
                f"({len(refresh_cids)} refreshed, {len(drop_cids)} dropped)"
                + ("" if old_exists else " [no previous artifact]")
            )

    meta["n_sessions"] = merged_counts["sessions"]
    meta["n_episodes"] = merged_counts["episodes"]
    meta["n_windows"] = merged_counts["windows"]
    meta["n_plays"] = merged_counts["plays"]
    meta["n_collections"] = len(meta.get("collections") or {})
    data_io.save_json(
        data=meta, storage_location=inputs.ARTIFACT_LOCATION, filename=inputs.META_FILE
    )
    for kind in inputs.SHARD_PREFIXES:
        for k in range(n_chunks):
            fn = shard_filename(kind, run_id, k)
            if data_io.exists(storage_location=inputs.ARTIFACT_LOCATION, filename=fn):
                data_io.remove(storage_location=inputs.ARTIFACT_LOCATION, filename=fn)
    return meta


def build_artifacts(
    reporter=None,
    params: dict | None = None,
    collections: list[str] | None = None,
    batch_size: int = 8,
    max_vectors: int = inputs.MAX_VECTORS_PER_LINK,
    coverage: dict[str, list[list[str]]] | None = None,
    workers=None,
) -> dict:
    """Build and persist the session + episode artifacts for all collections.

    In-process driver over :func:`build_batch` — the same batch-scoped
    computation the chained Cloud-Task worker runs, looped locally. Peak
    memory is O(batch), never O(corpus).

    Args:
        reporter: Optional status reporter (progress + cancellation).
        params: Optional parameter overrides (see :func:`default_params`).
        collections: Optional collection-id subset (None = every collection).
        batch_size: Collections per batch.
        max_vectors: Per-batch vector budget (see :func:`build_batch`).
        coverage: Optional per-collection date-window spec — discovery and
            segmentation restrict to it, and the meta gains the
            per-collection provenance block (see
            :func:`compute_coverage_spec`).

    Returns:
        A summary dict (the persisted ``sessions_meta.json`` payload).
    """

    def _log(msg: str) -> None:
        if reporter is not None:
            reporter.log(msg)
        else:
            logger.info(msg)

    p = {**inputs.default_params(), **(params or {})}
    backend = embeddings.active_embedding_backend()
    model = backend.model_id()

    _log(f"Preparing dense embedding store (model={model})...")
    try:
        corpus_mean, n_vectors, store_fp = embedding_store.get_corpus_mean(model, reporter=reporter)
        index = embedding_store.load_index(model)
    except (ValueError, embedding_store.CorpusMeanDrift):
        # No vectors for this model — sessions still get quality rows.
        corpus_mean, n_vectors, store_fp, index = None, 0, "", None
    _log(f"  {n_vectors:,} vectors")

    if coverage is not None:
        discovered = inputs.discover_covered_collections(coverage, collections)
    else:
        discovered = inputs.discover_collections(collections)
    cids = [c for c, _ in discovered]
    _log(f"  {len(cids)} collections to segment")
    trend_cols = inputs.trend_numeric_columns()
    _log(f"  session min/max columns for {len(trend_cols)} trend variable(s)")

    all_sessions: list[dict] = []
    all_episodes: list[dict] = []
    all_windows: list[dict] = []
    # Per-batch arrow tables (compact) — streamed into the plays artifact at
    # the end, one row group per batch, so collection_id stats stay tight.
    play_tables: list[pa.Table] = []
    n_plays = 0
    for start in range(0, len(cids), batch_size):
        batch = cids[start : start + batch_size]
        srows, erows, wrows, plays, stats = build_batch(
            batch,
            model,
            corpus_mean,
            index,
            params=p,
            reporter=reporter,
            max_vectors=max_vectors,
            trend_cols=trend_cols,
            coverage=coverage,
            workers=workers,
        )
        if srows is None:
            _log("Cancelled by user.")
            return {"cancelled": True}
        _log(format_batch_timing(start // batch_size, len(batch), stats))
        all_sessions.extend(srows)
        all_episodes.extend(erows)
        all_windows.extend(wrows)
        play_tables.append(plays_table(plays))
        n_plays += int(len(plays))
        done = min(start + batch_size, len(cids))
        if reporter is not None:
            reporter.update_progress(
                int(done / max(len(cids), 1) * 95),
                f"Segmented {done}/{len(cids)} collections "
                f"({len(all_sessions):,} sessions, {len(all_episodes):,} episodes, "
                f"{len(all_windows):,} windows)",
            )

    _log(
        f"Writing artifacts: {len(all_sessions):,} sessions, "
        f"{len(all_episodes):,} episodes, {len(all_windows):,} low-entropy windows"
    )
    empty_plays = plays_table(None)
    data_io.write_parquet_stream(
        storage_location=inputs.ARTIFACT_LOCATION,
        filename=inputs.PLAYS_FILE,
        batches=play_tables or [empty_plays],
        schema=empty_plays.schema,
    )
    data_io.save_parquet(
        df=_arrow_frame(all_sessions, sessions_schema(trend_cols)),
        storage_location=inputs.ARTIFACT_LOCATION,
        filename=inputs.SESSIONS_FILE,
    )
    data_io.save_parquet(
        df=_arrow_frame(all_episodes, _EPISODES_SCHEMA),
        storage_location=inputs.ARTIFACT_LOCATION,
        filename=inputs.EPISODES_FILE,
    )
    data_io.save_parquet(
        df=_arrow_frame(all_windows, _WINDOWS_SCHEMA),
        storage_location=inputs.ARTIFACT_LOCATION,
        filename=inputs.WINDOWS_FILE,
    )
    meta = {
        "built_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "embedding_model": model,
        "embedding_dim": int(index.dim) if index is not None else backend.dim(),
        "corpus_mean_count": n_vectors,
        "store_fingerprint": store_fp,
        "annotations_fingerprint": inputs.annotation_corpus_fingerprint(),
        "params": p,
        "trend_vars": trend_cols,
        "n_collections": len(cids),
        "n_sessions": len(all_sessions),
        "n_episodes": len(all_episodes),
        "n_windows": len(all_windows),
        "n_plays": n_plays,
    }
    if coverage is not None:
        meta["collections"] = inputs.collections_meta_block(
            discovered, coverage, built_at=meta["built_at"]
        )
    data_io.save_json(
        data=meta, storage_location=inputs.ARTIFACT_LOCATION, filename=inputs.META_FILE
    )
    return meta
