"""Sessions refresh: batch-and-chain build of the Sessions-tab artifacts.

On Cloud Run each link segments COLLECTIONS_PER_BATCH collections against the
dense embedding sidecar, writes its rows as per-link shards, and self-chains;
the final link concatenates the shards into the three single artifact files
(sessions_index / session_episodes / session_windows in "cache") — so the
read side (api_sessions_routes) never changes. Peak memory per link is
O(batch), never O(corpus).

The chain is pinned to one corpus-mean fingerprint at link 0: if the shard
store moves mid-run (an embeddings_refresh appended a shard), later links see
CorpusMeanDrift and restart the chain from scratch — bounded at
MAX_CHAIN_RESTARTS — rather than publish an artifact whose halves were
centred on different means.

Locally it loops the same links in one subprocess.
"""

import json
import sys
import time
import uuid
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter

# Collections per chain link. The binding constraint is the vector working
# set (see session_explorer.MAX_VECTORS_PER_LINK), which build_batch enforces
# independently; this only sets the plays/feature batch width.
COLLECTIONS_PER_BATCH = 8
_DISPATCH_DEADLINE = 1800
MAX_CHAIN_RESTARTS = 2

# Segmentation override keys accepted from the UI / CLI and carried verbatim
# through every link (so a chain restart re-resolves with the same inputs).
_OVERRIDE_KEYS = (("cut", float), ("mem", int), ("min_videos", int),
                  ("min_minutes", float), ("max_skip", int),
                  ("window_n", int), ("max_windows", int))






def _progress_filename(run_id: str) -> str:
    from fyp.analysis import session_explorer

    return f"{session_explorer.PROGRESS_PREFIX}{run_id}.json"






def _claim_chain_dispatch(run_id: str, chunk: int) -> bool:
    """Atomically claim the right to dispatch link ``chunk + 1``.

    A link that outlives its Cloud Tasks dispatch deadline is retried by the
    platform while the original execution keeps running — both eventually try
    to chain, and without this claim the chain FORKS into concurrent
    duplicates (2026-08-11/12: four chains re-segmented the corpus in
    parallel; the anti-partial-publish guard then failed the stragglers).
    The first execution to CAS its chunk into the progress file's
    ``dispatched`` set chains; every other execution of the same link stops.

    Args:
        run_id: The chain's run id (names the progress file).
        chunk: The link that wants to dispatch its successor.

    Returns:
        True when this execution won the claim.
    """
    import fyp.data_io as data_io
    from fyp.analysis import session_explorer

    claimed = {"won": False}

    def _mutate(progress):
        progress = progress if isinstance(progress, dict) else {}
        dispatched = progress.setdefault("dispatched", {})
        claimed["won"] = str(chunk) not in dispatched
        dispatched[str(chunk)] = True
        return progress

    data_io.update_json(
        storage_location=session_explorer.ARTIFACT_LOCATION,
        filename=_progress_filename(run_id), mutate=_mutate, default=None)
    return claimed["won"]






def run_sessions_refresh(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Segment one batch of collections and optionally chain to the next.

    Args:
        reporter: Status reporter (GCS on Cloud Run, stdout locally).
        task_args: Optional dict. User inputs: ``collections`` (comma-separated
            allow-list), ``batch_size``, plus the segmentation overrides
            ``cut``/``mem``/``min_videos``/``min_minutes``/``max_skip``/
            ``window_n``/``max_windows``. Chain-internal: ``chunk_index``,
            ``remaining_collections``, ``run_id``, ``params_json``,
            ``embedding_model``, ``corpus_mean_fp``, ``total_collections``,
            ``chain_restarts``.

    Returns:
        A chain dict when more batches remain, else None.
    """
    import pandas as pd

    import fyp.data_io as data_io
    from fyp.analysis import embedding_store, embeddings, session_explorer
    from fyp.memory import mem_probe

    task_args = task_args or {}
    chunk = int(task_args.get("chunk_index", 0))
    batch_size = int(task_args.get("batch_size") or COLLECTIONS_PER_BATCH)
    restarts = int(task_args.get("chain_restarts", 0))
    _t_run_start = time.perf_counter()

    overrides: dict = {}
    for key, cast in _OVERRIDE_KEYS:
        if task_args.get(key) is not None:
            overrides[key] = cast(task_args[key])
    collections_str = task_args.get("collections")

    def _restart_args() -> dict:
        base = {k: task_args.get(k) for k, _ in _OVERRIDE_KEYS
                if task_args.get(k) is not None}
        if collections_str:
            base["collections"] = collections_str
        base["batch_size"] = batch_size
        base["chunk_index"] = 0
        base["chain_restarts"] = restarts + 1
        return base

    def _chain_args(next_chunk: int, remaining: list[str], run_id: str,
                    params: dict, trend_cols: list[str], model: str,
                    store_fp: str, total: int) -> dict:
        args = {
            "chunk_index": next_chunk,
            "batch_size": batch_size,
            # \x1f (unit separator): collection ids are user-derived strings,
            # so a comma join would be ambiguous.
            "remaining_collections": "\x1f".join(remaining),
            "run_id": run_id,
            "params_json": json.dumps(params),
            "trend_cols_json": json.dumps(trend_cols),
            "embedding_model": model,
            "corpus_mean_fp": store_fp,
            "total_collections": total,
            "chain_restarts": restarts,
        }
        for key, _ in _OVERRIDE_KEYS:
            if task_args.get(key) is not None:
                args[key] = task_args[key]
        if collections_str:
            args["collections"] = collections_str
        return args

    # The initial dispatch (no run_id yet) is SETUP-ONLY: discovery, corpus-
    # mean pinning, stale-file sweep — then it chains immediately. It must
    # never process a batch: the initial task runs under the Cloud Tasks
    # dispatch deadline (1800s max for HTTP targets), and a batch link can
    # exceed that (44 min observed 2026-08-12), which makes the platform
    # retry the "failed" dispatch and fork a duplicate chain while the
    # original keeps running. Setup completes in seconds, so the deadline is
    # trivially met and retries of the initial task can no longer fork.
    if "run_id" not in task_args:
        reporter.log("Starting Sessions refresh...")
        params = {**session_explorer.default_params(), **overrides}
        collections = None
        if collections_str:
            collections = [c.strip() for c in str(collections_str).split(",") if c.strip()]
            reporter.log(f"Targeted refresh for {len(collections)} collection(s).")

        model = embeddings.active_embedding_backend().model_id()
        try:
            corpus_mean, n_vectors, store_fp = embedding_store.get_corpus_mean(
                model, reporter=reporter)
        except (ValueError, embedding_store.CorpusMeanDrift):
            corpus_mean, n_vectors, store_fp = None, 0, ""
        reporter.log(f"Embedding store: {n_vectors:,} vectors (model={model})")

        discovered = session_explorer.discover_collections(collections)
        remaining = [c for c, _ in discovered]
        total = len(remaining)
        # Pinned at setup and carried through the chain: every shard must use
        # the same session-extreme column set or the publish concat would see
        # mismatched schemas (e.g. a video_map rebuild landing mid-chain).
        trend_cols = session_explorer.trend_numeric_columns()
        reporter.log(f"Session min/max columns for {len(trend_cols)} trend variable(s).")
        run_id = str(task_args.get("log_run_id") or uuid.uuid4().hex[:12])
        session_explorer.sweep_stale_run_files(run_id)

        if total == 0:
            # Publish empty artifacts (fresh install / empty targeting) so the
            # tab renders a clean empty state instead of a missing-file error.
            meta = session_explorer.build_artifacts(
                reporter=reporter, params=overrides or None,
                collections=collections)
            reporter.update_progress(100, "Done")
            reporter.log("No collections with play rows; wrote empty artifacts.")
            return None if not meta.get("cancelled") else None

        reporter.log(f"Setup complete: {total} collection(s) to segment; "
                     "chaining to the first batch.")
        return {
            "chain": True,
            "next_task_args": _chain_args(0, remaining, run_id, params,
                                          trend_cols, model, store_fp, total),
            "dispatch_deadline_seconds": _DISPATCH_DEADLINE,
        }
    else:
        params = json.loads(task_args["params_json"])
        model = str(task_args["embedding_model"])
        store_fp = str(task_args.get("corpus_mean_fp", ""))
        run_id = str(task_args["run_id"])
        remaining = [c for c in str(task_args.get("remaining_collections", "")).split("\x1f") if c]
        total = int(task_args.get("total_collections", len(remaining)))
        trend_cols = json.loads(task_args.get("trend_cols_json") or "[]")
        if store_fp:
            try:
                corpus_mean, n_vectors, _ = embedding_store.get_corpus_mean(
                    model, expected_fp=store_fp)
            except embedding_store.CorpusMeanDrift:
                if restarts >= MAX_CHAIN_RESTARTS:
                    raise RuntimeError(
                        f"Embedding store changed mid-chain {restarts + 1} times "
                        f"— giving up; run Sessions refresh again once "
                        f"embeddings_refresh has settled.")
                reporter.log(
                    "Embedding store changed mid-chain (new shards landed). "
                    f"Restarting from scratch (restart {restarts + 1}/{MAX_CHAIN_RESTARTS}).")
                return {
                    "chain": True,
                    "next_task_args": _restart_args(),
                    "dispatch_deadline_seconds": _DISPATCH_DEADLINE,
                }
        else:
            corpus_mean, n_vectors = None, 0

    batch = remaining[:batch_size]
    rest = remaining[batch_size:]
    index = embedding_store.load_index(model) if corpus_mean is not None else None

    with mem_probe("SESSIONS", f"chunk_{chunk:04d}", log=reporter.log,
                   collections=len(batch)):
        srows, erows, wrows, plays, stats = session_explorer.build_batch(
            batch, model, corpus_mean, index, params=params, reporter=reporter,
            trend_cols=trend_cols)
    if srows is None:
        reporter.log("Cancelled by user. Previous artifacts left intact.")
        return None

    session_explorer.write_batch_shards(run_id, chunk, srows, erows, wrows,
                                        trend_cols=trend_cols, plays=plays)

    def _mutate(progress):
        # Keyed by chunk so a Cloud Tasks replay of a link OVERWRITES its own
        # entry instead of double-counting — the totals must stay a pure
        # function of the set of links, or the publish row-count verification
        # would (rightly) refuse a retried run.
        progress = progress if isinstance(progress, dict) else {}
        chunks = progress.setdefault("chunks", {})
        chunks[str(chunk)] = {
            "sessions": len(srows), "episodes": len(erows),
            "windows": len(wrows), "plays": stats["n_plays"],
            # Per-chunk collection count: summed at publish and compared to
            # what discovery found, so a run that only covered part of the
            # corpus can never publish (see publish_artifacts).
            "collections": len(batch),
        }
        return progress

    progress_raw = data_io.update_json(
        storage_location=session_explorer.ARTIFACT_LOCATION,
        filename=_progress_filename(run_id), mutate=_mutate, default=None)
    progress = {
        key: sum(entry.get(key, 0) for entry in progress_raw["chunks"].values())
        for key in ("sessions", "episodes", "windows", "plays", "collections")
    }

    done = total - len(rest)
    reporter.update_progress(
        min(int(done / max(total, 1) * 95), 95),
        f"Segmented {done}/{total} collections "
        f"({progress['sessions']:,} sessions, {progress['episodes']:,} episodes, "
        f"{progress['windows']:,} windows)")
    reporter.log(
        f"Link {chunk}: {len(batch)} collection(s), tier {stats['tier']}, "
        f"{stats['n_vectors']:,} vectors, {stats['n_plays']:,} plays.")

    if rest:
        if reporter.check_cancelled():
            reporter.log("Cancellation requested. Stopping without publishing.")
            return None
        if not _claim_chain_dispatch(run_id, chunk):
            reporter.log(
                f"Link {chunk} was already chained by a concurrent execution "
                "(a platform retry of this link) — stopping this duplicate.")
            return None
        return {
            "chain": True,
            "next_task_args": _chain_args(chunk + 1, rest, run_id, params,
                                          trend_cols, model, store_fp, total),
            "dispatch_deadline_seconds": _DISPATCH_DEADLINE,
        }

    # ---- Final link: publish ----
    index_dim = index.dim if index is not None else None
    if index_dim is None:
        index_dim = embeddings.active_embedding_backend().dim()
    meta = {
        "built_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "embedding_model": model,
        "embedding_dim": int(index_dim),
        "corpus_mean_count": int(n_vectors),
        "store_fingerprint": store_fp,
        "params": params,
        "trend_vars": trend_cols,
        "n_collections": total,
        "n_sessions": int(progress["sessions"]),
        "n_episodes": int(progress["episodes"]),
        "n_windows": int(progress["windows"]),
        "n_plays": int(progress["plays"]),
    }
    session_explorer.publish_artifacts(
        run_id, n_chunks=chunk + 1,
        expected={"sessions": progress["sessions"],
                  "episodes": progress["episodes"],
                  "windows": progress["windows"],
                  "plays": progress["plays"]},
        meta=meta, reporter=reporter,
        covered_collections=progress["collections"],
        total_collections=total)
    data_io.remove(storage_location=session_explorer.ARTIFACT_LOCATION,
                   filename=_progress_filename(run_id))

    reporter.update_progress(100, "Done")
    _t_run = time.perf_counter() - _t_run_start
    reporter.log(
        f"[TIMING] sessions_refresh link={chunk} wall={_t_run:.2f}s "
        f"collections={total} sessions={meta['n_sessions']} "
        f"episodes={meta['n_episodes']} model={meta['embedding_model']}"
    )
    reporter.log("Sessions refresh completed.")
    return None




if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    def _make_task_args(args) -> dict:
        task_args: dict = {}
        if args.collections:
            task_args["collections"] = args.collections
        if args.batch_size:
            task_args["batch_size"] = args.batch_size
        for key in ("cut", "mem", "min_videos", "min_minutes", "max_skip",
                    "window_n", "max_windows"):
            value = getattr(args, key, None)
            if value is not None:
                task_args[key] = value
        return task_args

    def _chain_locally(reporter, task_args):
        """Run the same links the Cloud chain would, in one process."""
        while True:
            chain = run_sessions_refresh(reporter, task_args)
            if not chain:
                return
            task_args = chain["next_task_args"]

    run_worker(
        _chain_locally,
        "sessions_refresh",
        arg_specs=[
            (("--collections",), {"type": str, "default": None,
                                  "help": "Comma-separated collection ids to refresh (default: all)"}),
            (("--batch-size",), {"type": int, "default": None,
                                 "help": f"Collections per link (default {COLLECTIONS_PER_BATCH})"}),
            (("--cut",), {"type": float, "default": None}),
            (("--mem",), {"type": int, "default": None}),
            (("--min-videos",), {"type": int, "default": None}),
            (("--min-minutes",), {"type": float, "default": None}),
            (("--max-skip",), {"type": int, "default": None,
                               "help": "Consecutive off-theme videos a binge survives"}),
            (("--window-n",), {"type": int, "default": None}),
            (("--max-windows",), {"type": int, "default": None}),
        ],
        make_task_args=_make_task_args,
    )
