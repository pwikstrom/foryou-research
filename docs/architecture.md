# Architecture

High-level map of the FYP platform for someone reading the code for the
first time. The exhaustive reference (every module, every convention) is
`AGENT.md` at the repo root; this document explains how the pieces fit.

## The system in one paragraph

Researchers upload participants' data-donation exports (zips) or feed
captures. The **ingestion layer** parses them into a platform-agnostic
*activity* table (one row per play/like/comment/...). The **scraper** then
fetches metadata + media for each watched item, and the **annotator** sends
downloaded media to Google Gemini for structured content annotation. A
**consolidation + recode** step merges activity, scrape, and annotation data
into per-study datasets, which the **analysis layer** (PCA, ANOVA,
PERMANOVA, timelines, sequence analysis, semantic embeddings) and the
**Flask dashboard** consume.

```
donation zips ─► ingest ─► activity parquet ─┐
                                             ├─► consolidate ─► recode ─► studies ─► analysis / dashboard
item ids ─► scrape queue ─► scraper ─► scrape parquet ──┤
downloaded media ─► annotation queue ─► Gemini ─────────┘
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

- **Ingestion**: `ForYouBaseCollection` (`fyp/ingest.py`). Subclasses declare
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
them; parse failures leave files pending for retry rather than discarding.

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
rebuild only when `requirements312.txt` changes) and `Dockerfile` (app code,
~1 min build). Exact commands: `AGENT.md` §"Cloud Run Deployment".

## Where to start reading

1. `fyp/fyp_config.py` — config + var_schema synthesis (and the import-cycle
   rule documented in `CONTRIBUTING.md`)
2. `fyp/data_io.py` — the storage abstraction everything uses
3. `fyp/ingest.py` — the base collection + one platform subclass
4. `web_interface/fyp_data_hub.py` — the app factory
5. `web_interface/process_manager.py` — how background work runs
