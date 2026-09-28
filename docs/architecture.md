# Architecture

High-level map of The For You Data Hub for someone reading the code for the
first time: how the pieces fit. Each subsystem's details live in its own
document (linked below); the module-by-module project tree and the developer
workflow are in [DEVELOPING.md](../DEVELOPING.md).

## The system in one paragraph

Researchers upload participants' data-donation exports (zips) or feed
captures — and participants can also upload their own donations self-serve
(My stuff → My Collections), with a browser-side review/prune step before
anything is transmitted and a self-service withdrawal path afterwards. The
**ingestion layer** parses them into a platform-agnostic
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

- **`fyp/core/data_io.py`** — all file I/O goes through named locations
  (`"cache"`, `"recoded"`, `"users"`, ...) that resolve to local paths or GCS
  objects depending on config. Never open raw paths.
- **`web_interface/process_manager.py` + `task_status.py`** — every
  background job is a `run_<name>(reporter, task_args)` function, declared
  once in `web_interface/worker_registry.py` (`WORKERS`: script, Cloud Tasks
  deadline, retry safety, launch surfaces). Locally it
  runs as a subprocess whose stdout is parsed for `::PROGRESS::`/`::DATA::`
  markers (`LocalStatusReporter`); on Cloud Run it runs as a Cloud Task
  reporting to GCS status files with a heartbeat (`GCSStatusReporter`).
  Long jobs self-chain: one batch per task, returning
  `{"chain": True, "next_task_args": ...}`.

The job framework — worker registry, dispatch deadlines, retry model,
single-flight leases, durable run logs, chain-aware status, and the
`enrichment_supervisor` and `ops_report` workers — is described in
[web_interface.md](web_interface.md#background-workers).

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
| `annotation_contract.toml` | the generated annotation prompt, response schema, annotation fields (backend-agnostic — every annotation backend consumes the same generated artifacts) |
| `scrape_contract.toml` | canonical cross-platform scrape fields (base + per-platform) |
| `activity_contract.toml` | the platform-agnostic activity schema |
| `derived_contract.toml` | merge-derived columns |

At config load, `fyp/core/fyp_config.py` synthesizes the in-memory `var_schema`
DataFrame from these contracts plus three **version registries**
(`annotation_versioning`/`scrape_versioning`/`activity_versioning`, id
prefixes `av_`/`sv_`/`acv_`) that stamp per-row provenance and keep
superseded ("legacy") fields readable. Admin-editable presentation flags
(which variables appear on which UI surface) live separately in
`var_presentation.json` and are never part of the schema hash.

**The schema hash matters**: study caches key on it. Metadata-only edits are
hash-neutral by design; structural changes bump it and trigger re-recoding.
The full guide to the contract system — authoring, validation, versioning,
and the runtime annotation-contract flow — is [contracts.md](contracts.md).

## Extensibility pattern: registry base classes

Both ingestion and scraping use the same design — an ABC with an
`__init_subclass__` auto-registry, so adding a platform is one subclass and
zero orchestration edits:

- **Ingestion**: `ForYouBaseCollection` (`fyp/ingest/base.py`). Subclasses declare
  `source_platform`/`raw_path` and implement `load_single_raw()` +
  `process_single()`. Registration also self-registers the platform's
  raw-upload storage location (`activity_data/<platform>/<raw_path>`; the
  three TikTok classes predate the convention and use source-keyed
  `ddp/`, `aio/`, `zeeschuimer/` folders — see
  [configuration.md](configuration.md#storage-locations)).
- **Scraping**: `BaseScraper` (`fyp/scrape/platform_scraper.py`) with
  `get_scraper(platform)` factory. Subclasses implement five hooks
  (`item_url`, `fetch`, `map_to_canonical`, `classify_error`,
  `repair_counts`). Per-platform queues, worker processes, and media
  subdirectories all derive automatically.

Supporting safety nets: the **structure sentinel**
(`fyp/core/structure_sentinel.py`) learns each platform's export structure and
quarantines silently-drifted uploads for admin review instead of ingesting
them (drift inside sections a file contains — sections a donor left out are
noted, never flagged); parse failures leave files pending for retry rather
than discarding;
and the **ingestion ledger** records every file's per-run outcome with row
counts and a drop-reason breakdown, surfaced as a permanent per-file intake
report in the UI. On the scraping side, batch-level guards (a circuit
breaker and two storm guards) stop a broken session from churning the queue:
they abort the batch, stop self-chaining, and raise a persistent per-platform
scraper alert that holds the enrichment supervisor off that platform until it
clears (see [pipeline.md](pipeline.md#failure-verdicts-and-batch-guards)).
On the analysis side, every study refresh writes a
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
- **Sessions subsystem**. `fyp/analysis/session_explorer.py` segments
  sessions and binge episodes; `web_interface/run_sessions_refresh.py`
  is a self-chaining Cloud Task that segments a few collections per link
  against the sidecar and folds per-link shards into the Sessions-tab
  artifacts on the final link. The build is scoped to the collections and
  date windows studies use, and is incremental — including after enrichment
  changes, which re-segment only the collections they touch (details:
  [pipeline.md](pipeline.md#sessions-refresh)). Read side:
  `routes/api_sessions_routes.py`, `templates/tabs/sessions.html`,
  `static/sessions.js`.

## Auto-managed participant studies

Every participant gets two system-managed studies: `__me__{username}`
("Just Me"), a materialised study over their own collections, and
`__me_plus__{username}` ("Everyone & Me"), which is **never materialised** —
its definition carries a COMPOSE marker and its frame is assembled at load
time (`web_interface/services/study_data.py`) from the site-wide default
study plus the user's own data. Both carry SYSTEM markers and are skipped by
all-studies sweeps and boot migrations, and they are provisioned lazily on
first login / donation registration, so dormant donors get nothing. The
architectural consequence: the study count now scales with the participant
count, not just with the researcher-defined studies.

## The web layer

`web_interface/fyp_data_hub.py` is an app factory registering 13
blueprints (`web_interface/routes/` — including the participant-facing
`my_collections` blueprint and, conditionally on the task-runner/local
services, the CSRF-exempt `internal` blueprint). Auth is Flask-Login with a
JSON-file user store, role-based permissions (`permissions.py`,
`@permission_required`), and global CSRF. The public/SEO surface lives in
`web_interface/seo.py`: the canonical `<link>`, `robots.txt`, the sitemap,
JSON-LD, and the `www.`→apex redirect, all derived from the configured
`[site] app_url`. The frontend is a no-build-step
SPA: Jinja templates + vanilla JS per tab, all styling through the CSS
token system in `static/style.css`. See
[web_interface.md](web_interface.md).

## Deployment

One Docker image, two Cloud Run services (`fyp-data-hub` web,
`fyp-task-runner` jobs). The image is layered: `Dockerfile.base` (deps —
rebuild only when `requirements.txt` changes) and `Dockerfile` (app code,
~1 min build). Exact commands:
[DEVELOPING.md](../DEVELOPING.md#cloud-run-deployment).

## Package layout

`fyp/` is organized into five subpackages — `core/` (config, I/O, types,
paths, logging), `ingest/`, `scrape/`, `annotation/`, `analysis/` — mapped
in detail in [fyp-import-graph.md](fyp-import-graph.md). The old flat paths
(`fyp/data_io.py`, `fyp/pca.py`, ...) remain importable as alias shims (same
module objects) for code outside this repository. **Code in this repository
imports only the canonical `fyp.<subpackage>.<module>` paths**, enforced by
ruff's banned-api rule (`TID251` in `pyproject.toml`), because two threads
resolving cold shims concurrently can receive a partially-initialized module
(CPython's per-module-lock deadlock breaker).
`tests/unit/test_pool_import_race.py` sweeps every thread-pool body as a
second guard (see [decision 0010](decisions/0010-canonical-imports-only.md)).

## Where to start reading

1. `fyp/core/fyp_config.py` — config + var_schema synthesis (and the
   import-cycle rule documented in [CONTRIBUTING.md](../CONTRIBUTING.md#invariants-you-must-not-break))
2. `fyp/core/data_io.py` — the storage abstraction everything uses
3. `fyp/ingest/base.py` — the base collection, plus one platform subclass
   (e.g. `fyp/ingest/instagram.py`)
4. `web_interface/fyp_data_hub.py` — the app factory
5. `web_interface/process_manager.py` — how background work runs
