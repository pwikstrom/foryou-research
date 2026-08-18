# Architecture

High-level map of The For You Data Hub for someone reading the code for the
first time. The exhaustive reference (every module, every convention) is
`DEVELOPING.md` at the repo root; this document explains how the pieces fit.

## The system in one paragraph

Researchers upload participants' data-donation exports (zips) or feed
captures. The **ingestion layer** parses them into a platform-agnostic
*activity* table (one row per play/like/comment/...). The **scraper** then
fetches metadata + media for each watched item, and the **annotator** sends
downloaded media to a pluggable LLM backend (Google Gemini by default;
hosted Qwen or fully-local models) for structured content annotation. A
**consolidation + recode** step merges activity, scrape, and annotation data
into per-study datasets, which the **analysis layer** (PCA, ANOVA,
PERMANOVA, timelines, sequence analysis, semantic embeddings, session/binge
profiling) and the **Flask dashboard** consume.

```
donation zips ─► ingest ─► activity parquet ─┐
                                             ├─► consolidate ─► recode ─► studies ─► analysis / dashboard
item ids ─► scrape queue ─► scraper ─► scrape parquet ──┤
downloaded media ─► annotation queue ─► LLM backend ────┘
```

## Execution environments

The same codebase runs in three modes; almost all code is mode-agnostic:

| Mode | Storage | Background jobs | Trigger |
|---|---|---|---|
| Local dev | filesystem (`[paths] local_data`) | subprocesses | default |
| Cloud Run (`fyp-data-hub`) | GCS | dispatches Cloud Tasks | `K_SERVICE` env set |
| Cloud Run (`fyp-task-runner`) | GCS | executes Cloud Tasks | Cloud Tasks HTTP push |

Two abstractions make this work:

- **`fyp/data_io.py`** — all file I/O goes through named locations
  (`"cache"`, `"recoded"`, `"users"`, ...) that resolve to local paths or GCS
  objects depending on config. Never open raw paths.
- **`web_interface/process_manager.py` + `task_status.py`** — every
  background job is a `run_<name>(reporter, task_args)` function. Locally it
  runs as a subprocess whose stdout is parsed for `::PROGRESS::`/`::DATA::`
  markers (`LocalStatusReporter`); on Cloud Run it runs as a Cloud Task
  reporting to GCS status files with a heartbeat (`GCSStatusReporter`).
  Long jobs self-chain: one batch per task, returning
  `{"chain": True, "next_task_args": ...}`.

Hard-won robustness around that job framework, all mode-agnostic:

- **Durable run logs** (`web_interface/run_logs.py`) — the last 10 runs per
  process persist as `proc_logs/<key>.json` in `cache` (compare-and-swap
  writes, per-key flusher thread), with per-line timestamps and a
  "Started by <user>" banner. Both execution modes and both Cloud Run
  services write to the same document, so a log survives restarts and is
  visible from either service; log bookkeeping never raises into the task.
- **Explicit dispatch deadlines** — Cloud Tasks' default HTTP deadline is
  shorter than a heavy batch link, and a timed-out attempt *keeps running*
  while the retry starts a concurrent duplicate chain. Every self-chaining
  refresh therefore carries an explicit 1800 s deadline, including the
  *initial* dispatch from `process_manager` (a worker's own
  `_DISPATCH_DEADLINE` only governs the links it dispatches itself;
  `tests/unit/test_dispatch_deadlines.py` pins the table).
- **Single-flight leases** — `embeddings_refresh` claims a CAS-guarded lease
  file so a Cloud Tasks redelivery can never run two appenders against the
  embedding shard store at once (a duplicate chain once wrote twin shards);
  the store readers additionally dedupe on item id, last occurrence wins.
- **Chain-aware status** — `last_run_duration` spans the whole self-chain
  (the run start rides through `task_args`), not just the final link, and
  the UI's status lights use one unified green/blue/amber/red vocabulary
  fed by the GCS status files (queued/failed states included).

**Packaging / reuse.** `fyp` is an installable package (`pip install -e .` is
the recommended dev setup; see `pyproject.toml`), but installation is never
required — the repo also runs from a plain checkout, and the Docker image
copies the code without installing it. Reusing `fyp` in another project
requires a config file: either a project root containing `__proj__.py` and
`config/config.toml` (located via the `__proj__.py` sentinel), or the
`FYP_CONFIG_PATH` environment variable pointing at a config TOML directly.
Configuration loads lazily — importing `fyp` submodules does not touch it
until `get_config()` / `fyp_cf` is first accessed (the one exception is
`fyp.ingest`, whose platform classes register storage locations at import).

## The contract system (variable schema)

Four declarative TOML files in `config/` own the entire variable schema:

| Contract | Owns |
|---|---|
| `annotation_contract.toml` | Gemini prompt, response schema, annotation fields |
| `scrape_contract.toml` | canonical cross-platform scrape fields (base + per-platform) |
| `activity_contract.toml` | the platform-agnostic activity schema |
| `derived_contract.toml` | merge-derived columns |

At config load, `fyp/fyp_config.py` synthesizes the in-memory `var_schema`
DataFrame from these contracts plus three **version registries**
(`annotation_versioning`/`scrape_versioning`/`activity_versioning`, id
prefixes `av_`/`sv_`/`acv_`) that stamp per-row provenance and keep
superseded ("legacy") fields readable. Admin-editable presentation flags
(which variables appear on which UI surface) live separately in
`var_presentation.json` and are never part of the schema hash.

**The schema hash matters**: study caches key on it. Metadata-only edits are
hash-neutral by design; structural changes bump it and trigger re-recoding.

## Extensibility pattern: registry base classes

Both ingestion and scraping use the same design — an ABC with an
`__init_subclass__` auto-registry, so adding a platform is one subclass and
zero orchestration edits:

- **Ingestion**: `ForYouBaseCollection` (`fyp/ingest/base.py`). Subclasses declare
  `source_platform`/`raw_path` and implement `load_single_raw()` +
  `process_single()`. Registration also self-registers the platform's
  raw-upload storage location.
- **Scraping**: `BaseScraper` (`fyp/platform_scraper.py`) with
  `get_scraper(platform)` factory. Subclasses implement five hooks
  (`item_url`, `fetch`, `map_to_canonical`, `classify_error`,
  `repair_counts`). Per-platform queues, worker processes, and media
  subdirectories all derive automatically.

Supporting safety nets: the **structure sentinel**
(`fyp/structure_sentinel.py`) learns each platform's export structure and
quarantines silently-drifted uploads for admin review instead of ingesting
them; parse failures leave files pending for retry rather than discarding;
and the **ingestion ledger** records every file's per-run outcome with row
counts and a drop-reason breakdown, surfaced as a permanent per-file intake
report in the UI. On the scraping side, batch-level guards stop a broken
session from churning the queue: a **circuit breaker** (consecutive
rate-limit/bot-check outcomes) plus twin **storm guards** — N consecutive
identical *permanent* classifications (a flagged session mis-reporting live
items as removed) or identical *transient* ones (a bot wall failing every
item retryably) abort the batch, stop self-chaining, and raise a persistent
per-platform scraper alert; the failed-scrapes record stores each item's
failure category so storms are diagnosable after the fact. On the analysis
side, every study refresh writes a
**methods/provenance note** (`{study}_methods.json`) summarising filters,
counts, and the contract/model versions behind the data — see
[pipeline.md](pipeline.md).

## Analysis at corpus scale

Two additions keep the heavy analysis paths O(batch) rather than O(corpus):

- **Dense embedding sidecar** (`fyp/analysis/embedding_store.py`). The
  embedding parquet shards are the source of truth but decode in full on any
  subset read. The sidecar is a pure derived cache per model: immutable
  float16 part files (one per compacted shard), a sorted id→row index, and a
  manifest carrying a shard-set fingerprint plus the running vector sum (so
  the exact corpus mean needs no second pass). Vectors are read back via
  `np.memmap` locally and coalesced ranged reads on GCS. The
  fingerprint-stamped corpus mean is the guard that keeps a batched consumer
  from ever centring on a stale mean. This is what made the sessions build
  batch-sized and fixed the PCA refresh's memory blow-up.
- **Sessions subsystem**. `fyp/analysis/session_profile.py` and
  `sequence_analysis.py` do the profiling; `web_interface/run_sessions_refresh.py`
  is a self-chaining Cloud Task that segments a few collections per link
  against the sidecar, writes per-link shards, and folds them into the
  Sessions-tab artifacts (session index, binge episodes, low-entropy
  windows, and a `sessions_plays.parquet` detail fast path) on the final
  link. The build is **study-window-scoped** (only collections in ≥1 study,
  within the padded union of their studies' date windows) and
  **incremental**: `stale_only` rebuilds just the collections whose windows
  or in-window play counts changed, merging their rows into the artifacts.
  Enrichment staleness is deliberately *global* — a changed embedding-store
  or annotation-corpus fingerprint forces a full rebuild, because the
  per-collection fingerprint comes from the activity file, which carries no
  enrichment columns. The chain pins one corpus-mean fingerprint at link 0
  and restarts (bounded) if the shard store moves mid-run. A sessions
  refresh is chained automatically after every study save. Read side:
  `routes/api_sessions_routes.py`, `templates/tabs/sessions.html`,
  `static/sessions.js`.

## The web layer

`web_interface/fyp_data_hub.py` is an app factory registering ~10
blueprints (`web_interface/routes/`). Auth is Flask-Login with a JSON-file
user store, role-based permissions (`permissions.py`,
`@permission_required`), and global CSRF. The frontend is a no-build-step
SPA: Jinja templates + vanilla JS per tab, all styling through the CSS
token system in `static/style.css`. See
[web_interface.md](web_interface.md).

## Deployment

One Docker image, two Cloud Run services (`fyp-data-hub` web,
`fyp-task-runner` jobs). The image is layered: `Dockerfile.base` (deps —
rebuild only when `requirements.txt` changes) and `Dockerfile` (app code,
~1 min build). Exact commands: `DEVELOPING.md` §"Cloud Run Deployment".

## Package layout

`fyp/` is organized into five subpackages — `core/` (config, I/O, types,
paths, logging), `ingest/`, `scrape/`, `annotation/`, `analysis/` — mapped
in detail in [fyp-import-graph.md](fyp-import-graph.md). The old flat paths
(`fyp/data_io.py`, `fyp/pca.py`, ...) remain permanently importable as alias
shims (same module objects), so both spellings work; new code should prefer
the subpackage paths. One hard rule: **code that can run on a worker thread
pool must import the canonical `fyp.<subpackage>.<module>` path, never a
flat shim, in any lazy (function-level) import** — two threads resolving
cold shims concurrently can receive a partially-initialized module (CPython's
per-module-lock deadlock breaker), which silently dropped collections from
prod timelines batches. Module-level shim imports are fine (they resolve
once, single-threaded); `tests/unit/test_pool_import_race.py` sweeps every
pool body for violations.

## Where to start reading

1. `fyp/core/fyp_config.py` — config + var_schema synthesis (and the
   import-cycle rule documented in `CONTRIBUTING.md`)
2. `fyp/core/data_io.py` — the storage abstraction everything uses
3. `fyp/ingest/base.py` — the base collection, plus one platform subclass
   (e.g. `fyp/ingest/instagram.py`)
4. `web_interface/fyp_data_hub.py` — the app factory
5. `web_interface/process_manager.py` — how background work runs
