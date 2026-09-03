"""Sessions refresh: batch-and-chain build of the Sessions-tab artifacts.

On Cloud Run each link segments COLLECTIONS_PER_BATCH collections against the
dense embedding sidecar, writes its rows as per-link shards, and self-chains;
the final link folds the shards into the four single artifact files
(sessions_index / session_episodes / session_windows / sessions_plays in
"cache") — so the read side (api_sessions_routes) never changes. Peak memory
per link is O(batch), never O(corpus).

The build is **study-window-scoped**: only collections selected by at least
one study are segmented, and only within the union of those studies' saved
date windows (padded — see session_explorer.compute_coverage_spec). Three
run modes, decided at the setup link:

* **full** (no ``collections``, no ``stale_only``): rebuild every covered
  collection; the publish overwrites the artifacts wholesale.
* **merge** (``collections`` and/or ``stale_only``): rebuild a subset and
  fold its rows into the existing artifacts, replacing only that subset's
  rows (plus dropping collections that left every study). ``stale_only``
  derives the subset by comparing each collection's coverage windows and
  in-window play count against the per-collection provenance block in
  sessions_meta.json; global invalidators (model/params/schema change,
  missing meta, and — since 2026-08-16 — a changed embedding store or
  annotation corpus) escalate to full.
* **noop** (``stale_only`` with nothing stale): return immediately without
  touching anything.

``skip_if_busy`` makes the setup link exit gracefully when another run's
progress file looks live — used by chained dispatch (e.g. after a study
refresh) so it never sweeps an in-flight run's shards.

The chain is pinned to one corpus-mean fingerprint at link 0: if the shard
store moves mid-run (an embeddings_refresh appended a shard), later links see
CorpusMeanDrift and restart the chain from scratch — bounded at
MAX_CHAIN_RESTARTS — rather than publish an artifact whose halves were
centred on different means. Since 2026-08-16 a store that moved *between*
runs escalates to a full rebuild too, so the residual ``corpus_mean_drift``
meta flag only marks the degraded case where the store could not be read at
all.

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

# Collections per chain link when the caller pins ``batch_size``. The binding
# constraint is the vector working set (see
# session_explorer.MAX_VECTORS_PER_LINK), which build_batch enforces
# independently; this only sets the plays/feature batch width.
COLLECTIONS_PER_BATCH = 8

# Default batching is by play budget, not collection count: a link takes
# collections (biggest first) until it holds PLAYS_PER_BATCH plays or
# MAX_COLLECTIONS_PER_BATCH collections. With a fixed count of 8 the tail
# links were eight tiny collections each and ~40 s of pure per-link overhead
# (loads + dispatch) for a second of segmentation (prod, 2026-09-02); the
# budget folds that tail into one or two links while the biggest collections
# still get links of their own. Segmentation is parallel within a link, so a
# bigger link costs wall time only in proportion to its plays.
PLAYS_PER_BATCH = 250_000
MAX_COLLECTIONS_PER_BATCH = 32
_DISPATCH_DEADLINE = 1800
MAX_CHAIN_RESTARTS = 2

# Segmentation override keys accepted from the UI / CLI and carried verbatim
# through every link (so a chain restart re-resolves with the same inputs).
_OVERRIDE_KEYS = (("cut", float), ("mem", int), ("min_videos", int),
                  ("min_minutes", float), ("max_skip", int),
                  ("window_n", int), ("max_windows", int))






def plan_batch(remaining: list[str], counts: dict | None,
               batch_size: int | None) -> tuple[list[str], list[str]]:
    """Split ``remaining`` into this link's batch and the rest.

    An explicit ``batch_size`` is a plain count. Otherwise the batch grows
    until it reaches :data:`PLAYS_PER_BATCH` plays (per the run manifest's
    in-window ``counts``) or :data:`MAX_COLLECTIONS_PER_BATCH` collections;
    a collection bigger than the budget gets a link to itself. A legacy chain
    with no manifest counts falls back to :data:`COLLECTIONS_PER_BATCH`.
    """
    if batch_size:
        n = int(batch_size)
        return remaining[:n], remaining[n:]
    if not counts:
        return (remaining[:COLLECTIONS_PER_BATCH],
                remaining[COLLECTIONS_PER_BATCH:])
    n, plays = 0, 0
    while n < len(remaining) and n < MAX_COLLECTIONS_PER_BATCH:
        n += 1
        plays += int(counts.get(remaining[n - 1], 0))
        if plays >= PLAYS_PER_BATCH:
            break
    return remaining[:n], remaining[n:]




def _progress_filename(run_id: str) -> str:
    from fyp.analysis import session_explorer

    return f"{session_explorer.PROGRESS_PREFIX}{run_id}.json"






def _flag(value) -> bool:
    """Parse a task_args boolean that may arrive as bool, str, or int."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")






def _manifest_n_plays(value) -> int:
    """Read a manifest ``counts`` entry as a play count.

    Before 2026-08-16 the entry was ``[n_plays, n_annotated]``; the annotated
    term was dropped (it was structurally always 0 — see
    :func:`session_explorer.discover_covered_collections`). A chain that was
    already in flight across the deploy still carries the list shape in its
    progress file, and its final link runs on the new code.
    """
    if isinstance(value, (list, tuple)):
        return int(value[0]) if value else 0
    return int(value or 0)






def _store_shards_for(store_fp: str) -> list | None:
    """The shard set to record in the meta, or None when it no longer matches
    ``store_fp`` (the store moved mid-chain; the next run then re-baselines).

    Recorded so the next run can tell an append from a rewrite — see
    ``session_explorer.enrichment_change_scope``.
    """
    from fyp.analysis import embedding_store

    try:
        entries = embedding_store.shard_entries()
        if store_fp and embedding_store.fingerprint_of(entries) != store_fp:
            return None
        return [[str(n), int(s), float(m)] for n, s, m in entries]
    except Exception as exc:
        print(f"[sessions] shard set not recorded ({type(exc).__name__}: {exc}); "
              "the next run will re-baseline.")
        return None


def _merge_meta(meta_old, manifest: dict, params: dict, model: str,
                store_fp: str, n_vectors: int, dim: int,
                trend_cols: list[str], annotations_fp: str,
                annotations_max_ts: str | None = None) -> dict:
    """Meta payload for a merge publish.

    The per-collection block is the previous build's block minus dropped and
    refreshed collections, plus fresh entries for the refreshed ones (windows
    and counts from the run manifest). The ``n_*`` totals are filled by
    :func:`session_explorer.merge_publish_artifacts` from the merged files.
    ``corpus_mean_drift`` records that untouched collections were centred on
    a different (statistically equivalent) corpus mean than this run's.
    ``baseline_corpus_count`` (the vector count at the last FULL build) is
    carried forward: it is what the drift budget is measured against.
    """
    import pandas as pd

    old = meta_old if isinstance(meta_old, dict) else {}
    old_block = (old.get("collections")
                 if isinstance(old.get("collections"), dict) else {})
    refresh = list(manifest.get("refresh") or [])
    gone = set(manifest.get("drop") or []) | set(refresh)
    block = {cid: rec for cid, rec in old_block.items() if cid not in gone}
    built_at = pd.Timestamp.now(tz="UTC").isoformat()
    cov = manifest.get("coverage") or {}
    cnt = manifest.get("counts") or {}
    for cid in refresh:
        block[cid] = {"windows": cov.get(cid, []),
                      "n_plays": _manifest_n_plays(cnt.get(cid)),
                      "built_at": built_at}
    meta = {
        "built_at": built_at,
        "embedding_model": model,
        "embedding_dim": int(dim),
        "corpus_mean_count": int(n_vectors),
        "store_fingerprint": store_fp,
        "annotations_fingerprint": annotations_fp,
        "params": params,
        "trend_vars": trend_cols,
        "collections": block,
        "baseline_corpus_count": int(old.get("baseline_corpus_count")
                                     or old.get("corpus_mean_count") or n_vectors),
        "annotations_max_ts": annotations_max_ts or old.get("annotations_max_ts"),
    }
    shards = _store_shards_for(store_fp)
    if shards is not None:
        meta["store_shards"] = shards
    if old.get("store_fingerprint") and store_fp and \
            old.get("store_fingerprint") != store_fp:
        meta["corpus_mean_drift"] = True
    return meta






def _foreign_run_active(max_age_seconds: float = 5400) -> bool:
    """Whether any run's progress file was touched within ``max_age_seconds``.

    The setup link has no run id yet, so ANY fresh progress file means an
    in-flight chain (links update the file at least once per link; the
    longest observed link is ~44 min, hence the 90-minute freshness window).
    A finished chain deletes its progress file; an abandoned one goes stale
    and stops blocking.
    """
    import fyp.data_io as data_io
    from fyp.analysis import session_explorer

    now = time.time()
    for fn in data_io.listdir(storage_location=session_explorer.ARTIFACT_LOCATION):
        if not fn.startswith(session_explorer.PROGRESS_PREFIX):
            continue
        st = data_io.stat(storage_location=session_explorer.ARTIFACT_LOCATION,
                          filename=fn)
        if st and now - st["mtime"] <= max_age_seconds:
            return True
    return False






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
            allow-list — now folded into the existing artifacts instead of
            replacing them), ``stale_only`` (auto mode: refresh only stale
            collections), ``skip_if_busy`` (setup exits gracefully when
            another run looks live), ``batch_size``, plus the segmentation
            overrides ``cut``/``mem``/``min_videos``/``min_minutes``/
            ``max_skip``/``window_n``/``max_windows``. Chain-internal:
            ``chunk_index``, ``remaining_collections``, ``run_id``, ``mode``,
            ``params_json``, ``embedding_model``, ``corpus_mean_fp``,
            ``total_collections``, ``chain_restarts``.

    Returns:
        A chain dict when more batches remain, else None.
    """
    import pandas as pd

    import fyp.data_io as data_io
    from fyp.analysis import embedding_store, embeddings, session_explorer
    from fyp.memory import mem_probe

    task_args = task_args or {}
    chunk = int(task_args.get("chunk_index", 0))
    # None = the play-budget rule (see plan_batch); an explicit count is
    # carried verbatim through every link.
    batch_size = int(task_args["batch_size"]) if task_args.get("batch_size") else None
    restarts = int(task_args.get("chain_restarts", 0))
    stale_only = _flag(task_args.get("stale_only", ""))
    # Segmentation worker processes. Deliberately NOT a segmentation
    # override: overrides land in ``params`` and therefore in the published
    # meta, where a change would (rightly) force a full rebuild — the worker
    # count changes wall time only, never the rows.
    workers = task_args.get("workers")
    _t_run_start = time.perf_counter()

    overrides: dict = {}
    for key, cast in _OVERRIDE_KEYS:
        if task_args.get(key) is not None:
            overrides[key] = cast(task_args[key])
    collections_str = task_args.get("collections")

    def _restart_args() -> dict:
        # NOTE: skip_if_busy is deliberately NOT carried — a restart re-enters
        # setup while its own previous progress file still exists, so carrying
        # the flag would make the chain skip itself.
        base = {k: task_args.get(k) for k, _ in _OVERRIDE_KEYS
                if task_args.get(k) is not None}
        if collections_str:
            base["collections"] = collections_str
        if stale_only:
            base["stale_only"] = True
        if workers is not None:
            base["workers"] = workers
        if batch_size:
            base["batch_size"] = batch_size
        base["chunk_index"] = 0
        base["chain_restarts"] = restarts + 1
        return base

    def _chain_args(next_chunk: int, remaining: list[str], run_id: str,
                    params: dict, trend_cols: list[str], model: str,
                    store_fp: str, total: int, mode: str,
                    annotations_fp: str,
                    annotations_max_ts: str | None = None) -> dict:
        args = {
            "chunk_index": next_chunk,
            # \x1f (unit separator): collection ids are user-derived strings,
            # so a comma join would be ambiguous.
            "remaining_collections": "\x1f".join(remaining),
            "run_id": run_id,
            "mode": mode,
            "params_json": json.dumps(params),
            "trend_cols_json": json.dumps(trend_cols),
            "embedding_model": model,
            "corpus_mean_fp": store_fp,
            # Pinned at setup like corpus_mean_fp: the final link stamps it
            # into the published meta, and it must describe the corpus the
            # chain actually read, not whatever landed while it ran.
            "annotations_fp": annotations_fp,
            # The annotation corpus's latest inference_ts at setup — the
            # watermark the next run scopes "changed annotations" against.
            "annotations_max_ts": annotations_max_ts,
            "total_collections": total,
            "chain_restarts": restarts,
        }
        for key, _ in _OVERRIDE_KEYS:
            if task_args.get(key) is not None:
                args[key] = task_args[key]
        if collections_str:
            args["collections"] = collections_str
        if stale_only:
            args["stale_only"] = True
        if workers is not None:
            args["workers"] = workers
        if batch_size:
            args["batch_size"] = batch_size
        return args

    # The initial dispatch (no run_id yet) is SETUP-ONLY: discovery, corpus-
    # mean pinning, stale-file sweep — then it chains immediately. It must
    # never process a batch: the initial task runs under the Cloud Tasks
    # dispatch deadline (1800s max for HTTP targets), and a batch link can
    # exceed that (44 min observed 2026-08-12), which makes Cloud Tasks
    # retry the "failed" dispatch and fork a duplicate chain while the
    # original keeps running. Setup completes in seconds, so the deadline is
    # trivially met and retries of the initial task can no longer fork.
    if "run_id" not in task_args:
        reporter.log("Starting Sessions refresh...")
        # The setup link streams the whole play file to discover coverage, which
        # takes minutes on a large corpus. Report progress from the first moment
        # so the card shows a live phase instead of a bare "Initializing...".
        reporter.update_progress(0, "Planning refresh...")
        if _flag(task_args.get("skip_if_busy", "")) and restarts == 0:
            if _foreign_run_active():
                reporter.log("Another sessions refresh appears to be running "
                             "— skipping this (chained) run.")
                reporter.update_progress(100, "Skipped — refresh already running")
                return None
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
        annotations_fp = session_explorer.annotation_corpus_fingerprint()

        # Coverage-scoped discovery: only collections selected by >=1 study,
        # only their in-window plays. Always global — an explicit collections
        # list narrows the plan below, never the discovery, so staleness and
        # drops are computed against the whole corpus.
        reporter.update_progress(0, "Discovering covered collections...")
        coverage = session_explorer.compute_coverage_spec()
        discovered = session_explorer.discover_covered_collections(coverage)
        # Pinned at setup and carried through the chain: every shard must use
        # the same session-extreme column set or the publish concat would see
        # mismatched schemas (e.g. a video_map rebuild landing mid-chain).
        trend_cols = session_explorer.trend_numeric_columns()
        reporter.log(f"Session min/max columns for {len(trend_cols)} trend variable(s).")

        artifact_files = (session_explorer.SESSIONS_FILE,
                          session_explorer.EPISODES_FILE,
                          session_explorer.WINDOWS_FILE,
                          session_explorer.PLAYS_FILE)
        artifacts_exist = all(
            data_io.exists(storage_location=session_explorer.ARTIFACT_LOCATION,
                           filename=f) for f in artifact_files)
        meta_old = None
        if data_io.exists(storage_location=session_explorer.ARTIFACT_LOCATION,
                          filename=session_explorer.META_FILE):
            meta_old = data_io.load_json(
                storage_location=session_explorer.ARTIFACT_LOCATION,
                filename=session_explorer.META_FILE)
        plays_schema_ok = True
        if artifacts_exist:
            old_cols = data_io.get_parquet_columns(
                storage_location=session_explorer.ARTIFACT_LOCATION,
                filename=session_explorer.PLAYS_FILE)
            plays_schema_ok = (sorted(old_cols or []) ==
                               sorted(session_explorer.plays_table(None).schema.names))

        scope = {str(c) for c in collections} if collections else None

        # Where did the enrichment change land? A routine append (newly
        # annotated videos, embedded and folded in) touches only the
        # collections that contain those videos; the planner rebuilds
        # everything unless this proves the change local.
        covered = {cid for cid, _ in discovered}
        annotations_max_ts = (meta_old or {}).get("annotations_max_ts")
        try:
            escope = session_explorer.enrichment_change_scope(
                meta_old, store_fp, int(n_vectors), annotations_fp, model, covered)
            if escope.get("reason") and escope.get("reason") != "enrichment unchanged":
                verb = "local" if escope.get("local") else "not local"
                reporter.log(f"Enrichment change is {verb}: {escope['reason']}.")
            # The watermark the published meta records: freshly scanned when
            # the corpus moved, else carried from the previous build, else
            # scanned now (a first build, or one from before the watermark).
            annotations_max_ts = escope.get("annotations_max_ts") or annotations_max_ts
            if annotations_max_ts is None and annotations_fp:
                annotations_max_ts = session_explorer.annotation_corpus_max_ts()
        except Exception as exc:
            # Scoping is an optimisation: if it cannot be computed the plan
            # falls back to the full rebuild it always did.
            reporter.log(f"Enrichment-change scoping skipped ({type(exc).__name__}: {exc}) "
                         "— any enrichment change rebuilds every collection.")
            escope = None

        plan = session_explorer.compute_refresh_plan(
            discovered, coverage, meta_old, params, model, trend_cols,
            artifacts_exist, plays_schema_ok=plays_schema_ok, scope=scope,
            store_fp=store_fp, annotations_fp=annotations_fp,
            enrichment_scope=escope)
        if not stale_only and plan["mode"] != "full":
            # Forced refresh: rebuild the requested set regardless of
            # staleness. Unscoped -> full overwrite; scoped -> merge.
            refresh = [cid for cid, _ in discovered
                       if scope is None or cid in scope]
            if scope is None:
                plan = {"mode": "full", "reason": "forced full rebuild",
                        "refresh": refresh, "drop": []}
            else:
                plan = {"mode": "merge", "reason": "targeted refresh",
                        "refresh": refresh, "drop": plan["drop"]}
        elif plan["mode"] == "full" and (stale_only or scope is not None):
            reporter.log(f"Cannot refresh incrementally ({plan['reason']}) — "
                         "running a full rebuild of every covered collection.")
        reporter.log(f"Plan: mode={plan['mode']} ({plan['reason']}); "
                     f"{len(plan['refresh'])} to segment, "
                     f"{len(plan['drop'])} to drop; "
                     f"{len(discovered)} covered collection(s) total.")

        if plan["mode"] == "noop":
            reporter.update_progress(100, "Up to date")
            reporter.log("Sessions artifacts are up to date — nothing to do.")
            return None

        counts = dict(discovered)
        remaining = plan["refresh"]
        total = len(remaining)
        run_id = str(task_args.get("log_run_id") or uuid.uuid4().hex[:12])

        if plan["mode"] == "merge" and total == 0:
            # Pure-drop merge (collections left every study, nothing stale):
            # no batches to segment, so fold the drops in right here — the
            # stream is bounded and the setup deadline is trivially met.
            session_explorer.sweep_stale_run_files(run_id)
            meta = _merge_meta(
                meta_old, {"refresh": [], "drop": plan["drop"]}, params,
                model, store_fp, n_vectors,
                embeddings.active_embedding_backend().dim(), trend_cols,
                annotations_fp, annotations_max_ts)
            session_explorer.merge_publish_artifacts(
                run_id, n_chunks=0, refresh_cids=[], drop_cids=plan["drop"],
                expected={}, meta=meta, trend_cols=trend_cols,
                reporter=reporter, covered_collections=0)
            reporter.update_progress(100, "Done")
            reporter.log(f"Dropped {len(plan['drop'])} collection(s) that left "
                         "every study; nothing to segment.")
            return None

        if plan["mode"] == "full" and total == 0:
            # Publish empty artifacts (fresh install / no studies) so the
            # tab renders a clean empty state instead of a missing-file error.
            session_explorer.sweep_stale_run_files(run_id)
            meta = session_explorer.build_artifacts(
                reporter=reporter, params=overrides or None,
                coverage=coverage)
            reporter.update_progress(100, "Done")
            reporter.log("No covered collections with in-window play rows; "
                         "wrote empty artifacts.")
            return None if not meta.get("cancelled") else None

        session_explorer.sweep_stale_run_files(run_id)
        # Seed the run manifest into the progress file: the batch links read
        # the coverage windows from it, and the final link builds the meta's
        # per-collection block from it — Cloud Tasks payloads stay small.
        manifest = {
            "mode": plan["mode"],
            "refresh": plan["refresh"],
            "drop": plan["drop"],
            "coverage": {cid: coverage.get(cid, []) for cid in plan["refresh"]},
            "counts": {cid: int(counts.get(cid, 0))
                       for cid in plan["refresh"]},
        }

        def _seed(progress):
            progress = progress if isinstance(progress, dict) else {}
            progress["manifest"] = manifest
            return progress

        data_io.update_json(
            storage_location=session_explorer.ARTIFACT_LOCATION,
            filename=_progress_filename(run_id), mutate=_seed, default=None)

        reporter.log(f"Setup complete: {total} collection(s) to segment; "
                     "chaining to the first batch.")
        return {
            "chain": True,
            "next_task_args": _chain_args(0, remaining, run_id, params,
                                          trend_cols, model, store_fp, total,
                                          plan["mode"], annotations_fp,
                                          annotations_max_ts),
            "dispatch_deadline_seconds": _DISPATCH_DEADLINE,
        }
    else:
        params = json.loads(task_args["params_json"])
        model = str(task_args["embedding_model"])
        store_fp = str(task_args.get("corpus_mean_fp", ""))
        annotations_fp = str(task_args.get("annotations_fp", ""))
        annotations_max_ts = task_args.get("annotations_max_ts") or None
        run_id = str(task_args["run_id"])
        # Legacy in-flight chains (started pre-upgrade) carry no mode and have
        # no manifest: they publish full, unscoped, without the per-collection
        # block — the next auto run migrates them.
        mode = str(task_args.get("mode") or "full")
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

    # The run manifest (seeded at setup) carries each collection's coverage
    # windows and in-window play counts (the batching budget); a legacy chain
    # has none and builds unscoped in fixed-count batches, as before.
    manifest: dict = {}
    if data_io.exists(storage_location=session_explorer.ARTIFACT_LOCATION,
                      filename=_progress_filename(run_id)):
        prog = data_io.load_json(
            storage_location=session_explorer.ARTIFACT_LOCATION,
            filename=_progress_filename(run_id))
        if isinstance(prog, dict) and isinstance(prog.get("manifest"), dict):
            manifest = prog["manifest"]
    coverage_map = manifest.get("coverage") if manifest else None

    batch, rest = plan_batch(remaining, manifest.get("counts") if manifest else None,
                             batch_size)

    # Report before segmenting, not only after: a link runs for many minutes and
    # each link's reporter starts from a blank progress dict (link 0) or the
    # previous link's last write, so without this the bar sits still for the
    # whole batch.
    started = total - len(remaining)
    reporter.update_progress(
        min(int(started / max(total, 1) * 95), 95),
        f"Segmenting collections {started + 1}-{started + len(batch)} of {total}...")

    index = embedding_store.load_index(model) if corpus_mean is not None else None

    with mem_probe("SESSIONS", f"chunk_{chunk:04d}", log=reporter.log,
                   collections=len(batch)):
        srows, erows, wrows, plays, stats = session_explorer.build_batch(
            batch, model, corpus_mean, index, params=params, reporter=reporter,
            trend_cols=trend_cols, coverage=coverage_map, workers=workers)
    if srows is None:
        reporter.log("Cancelled by user. Previous artifacts left intact.")
        return None
    reporter.log(session_explorer.format_batch_timing(chunk, len(batch), stats))

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
                                          trend_cols, model, store_fp, total,
                                          mode, annotations_fp,
                                          annotations_max_ts),
            "dispatch_deadline_seconds": _DISPATCH_DEADLINE,
        }

    # ---- Final link: publish ----
    index_dim = index.dim if index is not None else None
    if index_dim is None:
        index_dim = embeddings.active_embedding_backend().dim()
    expected = {"sessions": progress["sessions"],
                "episodes": progress["episodes"],
                "windows": progress["windows"],
                "plays": progress["plays"]}
    if mode == "merge":
        if not manifest:
            # Without the manifest a merge cannot know what to replace, and
            # falling through to a full publish would overwrite the artifacts
            # with only this run's subset — the exact bug merge mode fixes.
            raise RuntimeError(
                f"merge run {run_id} lost its manifest (progress file "
                f"rewritten?) — refusing to publish. Shards kept.")
        meta_old = None
        if data_io.exists(storage_location=session_explorer.ARTIFACT_LOCATION,
                          filename=session_explorer.META_FILE):
            meta_old = data_io.load_json(
                storage_location=session_explorer.ARTIFACT_LOCATION,
                filename=session_explorer.META_FILE)
        meta = _merge_meta(meta_old, manifest, params, model, store_fp,
                           int(n_vectors), int(index_dim), trend_cols,
                           annotations_fp, annotations_max_ts)
        session_explorer.merge_publish_artifacts(
            run_id, n_chunks=chunk + 1,
            refresh_cids=list(manifest.get("refresh") or []),
            drop_cids=list(manifest.get("drop") or []),
            expected=expected, meta=meta, trend_cols=trend_cols,
            reporter=reporter,
            covered_collections=progress["collections"])
    else:
        meta = {
            "built_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "embedding_model": model,
            "embedding_dim": int(index_dim),
            "corpus_mean_count": int(n_vectors),
            "store_fingerprint": store_fp,
            "annotations_fingerprint": annotations_fp,
            # A full build IS the baseline: every collection is centred on
            # this corpus mean, and the drift budget counts appends from here.
            "baseline_corpus_count": int(n_vectors),
            "annotations_max_ts": annotations_max_ts,
            "store_shards": _store_shards_for(store_fp),
            "params": params,
            "trend_vars": trend_cols,
            "n_collections": total,
            "n_sessions": int(progress["sessions"]),
            "n_episodes": int(progress["episodes"]),
            "n_windows": int(progress["windows"]),
            "n_plays": int(progress["plays"]),
        }
        if manifest:
            manifest_counts = manifest.get("counts") or {}
            meta["collections"] = {
                cid: {"windows": (manifest.get("coverage") or {}).get(cid, []),
                      "n_plays": _manifest_n_plays(manifest_counts.get(cid)),
                      "built_at": meta["built_at"]}
                for cid in manifest.get("refresh") or []}
        session_explorer.publish_artifacts(
            run_id, n_chunks=chunk + 1,
            expected=expected, meta=meta, reporter=reporter,
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
        if getattr(args, "stale_only", False):
            task_args["stale_only"] = True
        if getattr(args, "skip_if_busy", False):
            task_args["skip_if_busy"] = True
        if args.batch_size:
            task_args["batch_size"] = args.batch_size
        if getattr(args, "workers", None) is not None:
            task_args["workers"] = args.workers
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
                                  "help": "Comma-separated collection ids to refresh "
                                          "(merged into the existing artifacts; default: all)"}),
            (("--stale-only",), {"action": "store_true",
                                 "help": "Refresh only collections whose study windows "
                                         "or in-window data changed"}),
            (("--skip-if-busy",), {"action": "store_true",
                                   "help": "Exit gracefully when another sessions "
                                           "refresh appears to be running"}),
            (("--batch-size",), {"type": int, "default": None,
                                 "help": "Collections per link (default: fill each "
                                         f"link to {PLAYS_PER_BATCH:,} plays or "
                                         f"{MAX_COLLECTIONS_PER_BATCH} collections)"}),
            (("--workers",), {"type": int, "default": None,
                              "help": "Segmentation worker processes per link "
                                      "(default: config [sessions] workers; "
                                      "1 = serial; results identical)"}),
            (("--cut",), {"type": float, "default": None,
                          "help": "Focus threshold on mean cosine distance to the "
                                  "recent centroid (default: config [sessions])"}),
            (("--mem",), {"type": int, "default": None,
                          "help": "Recent episode members the centroid is taken over "
                                  "(default: config [sessions])"}),
            (("--min-videos",), {"type": int, "default": None,
                                 "help": "Minimum distinct videos to keep an episode "
                                         "(default: config [sessions])"}),
            (("--min-minutes",), {"type": float, "default": None,
                                  "help": "Minimum episode span in minutes "
                                          "(default: config [sessions])"}),
            (("--max-skip",), {"type": int, "default": None,
                               "help": "Consecutive off-theme videos a binge survives"}),
            (("--window-n",), {"type": int, "default": None,
                               "help": "Videos per low-entropy window "
                                       "(default: config [sessions])"}),
            (("--max-windows",), {"type": int, "default": None,
                                  "help": "Low-entropy windows kept per session "
                                          "(default: config [sessions])"}),
        ],
        make_task_args=_make_task_args,
        description="Build the Sessions-tab artifacts (session index, binge "
                    "episodes, low-entropy windows)",
    )
