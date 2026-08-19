# Developing The For You Data Hub — maintainer guide

## Project Overview

**The For You Data Hub** is a short-video research data toolbox for academics, focused on TikTok with a platform-agnostic core. The object of study is short-form vertical video — the TikTok feed, Instagram Reels and YouTube Shorts; long-form YouTube watches are ingested and keep their metadata but stay below the media duration cap threshold, so they are never annotated. It ingests feed activity from TikTok data captures and from zipped data-donation exports (TikTok, plus Instagram and YouTube/Takeout watch history), enriches them via web scraping and LLM annotation (pluggable backends: Google Gemini by default, hosted Qwen, or local Qwen/MiniCPM — all platforms), performs statistical analysis (PCA, ANOVA, PERMANOVA), and presents findings through an interactive Flask-based web dashboard with role-based access control.

---

## Environment

- **Python**: 3.12 (the `.venv` virtual environment; matches the production runtime).
- Always activate the venv before running scripts: `source .venv/bin/activate`
- `pip install -e .` (editable install of the `fyp` package, from `pyproject.toml`) is the recommended dev setup — never required; the repo also runs from a plain checkout.
- **Deployment**: Docker (Python 3.12-slim), Gunicorn (1 worker, 8 threads).
- **Secrets** (set via environment variables):
  - `GEMINI_API_KEY`
  - `DASHSCOPE_API_KEY` (optional — enables the hosted-Qwen `qwen_api` annotation backend)
  - `FLASK_SECRET_KEY`
  - `FYP_GCS_BUCKET_NAME` (production)
  - `FLASK_DEBUG` (optional)
  - `FYP_CONFIG_PATH` (optional — use this config TOML directly instead of `__proj__.py` root discovery; the reuse hook)
  - `FYP_LOG_LEVEL` (optional — level for `fyp.logging_setup` loggers, default INFO)
  - `FYP_CONTACT_EMAIL`, `FYP_MAIL_SENDER`, `FYP_APP_URL` (optional — instance branding; override the `[site]` config section. Committed defaults are empty; prod sets these on both Cloud Run services)
  - `FYP_VERTEX_PROJECT` (optional — Vertex project when `[machine.gemini].project` is empty; falls back to `GCP_PROJECT_ID`, so prod needs nothing)
  - `K_SERVICE` (auto-set by Cloud Run — triggers GCS storage and Cloud Tasks dispatch)
  - `CLOUD_RUN_SERVICE_URL`, `GCP_PROJECT_ID`, `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_QUEUE`, `CLOUD_TASKS_SA_EMAIL` (Cloud Tasks config)
  - `AIO_DYNAMODB_TABLE`, `AIO_S3_BUCKET` (AIO stack resource names — deployment-specific, no defaults in code)

---

## Coding Style

- Use Python **type hints** in function signatures.
- Docstrings follow the **Google style guide**.
- Module imports at the **top of the file** — not inside functions.
- Use **f-strings** for string formatting.
- Keep functions separated by **at least 5 blank lines**.
- Comments should **explain the code**. Do not write your own reasoning in the code.
- Always use **PyArrow dtypes** for DataFrames.

### Frontend Styling Rules

All visual styling is managed through a **CSS custom property (token) system** in `style.css`. Never hardcode colors, fonts, sizes, or weights in templates, JavaScript, or inline styles.

- **Colors**: Use semantic tokens (e.g., `var(--color-text-primary)`, `var(--btn-danger-bg)`), never hex codes or `rgb()` values. The token hierarchy is: Primitives → Semantic → Component.
- **Fonts**: The primary font is **Inter** (`var(--font-sans)`). Monospace is `var(--font-mono)`. Never set `font-family` inline or in JS.
- **Font sizes**: Use the 7-step type scale tokens: `var(--text-hero)`, `var(--text-h2)`, `var(--text-h3)`, `var(--text-body)`, `var(--text-sm)`, `var(--text-xs)`, `var(--text-xxs)`. Or use the equivalent utility classes: `.text-hero`, `.text-h2`, `.text-h3`, `.text-body`, `.text-sm`, `.text-xs`, `.text-xxs`.
- **Font weights**: Use tokens `var(--weight-normal)` / `var(--weight-medium)` / `var(--weight-semibold)` / `var(--weight-bold)`, or utility classes `.font-normal`, `.font-medium`, `.font-semibold`, `.font-bold`.
- **Line height**: Use `var(--leading-tight)`, `var(--leading-normal)`, `var(--leading-relaxed)`.
- **In templates**: Prefer utility classes (`class="text-sm font-bold"`) over inline `style=""` for font properties.
- **In JavaScript**: Use `element.classList.add('text-sm', 'font-bold')` instead of `element.style.fontSize = '...'`. For Plotly charts, use `family: getCSSVar('--font-sans')`.
- **Tooltips**: Use the `.meta-tooltip` class with `data-tooltip="..."` attribute, not the native `title` attribute.
- **Buttons**: Use existing button classes (`.btn-primary`, `.btn-danger`, `.btn-save`, `.btn-stop`, `.btn-discreet`, `.action-btn`). Never set button colors inline.
- **Dark/light themes**: Both are defined in `style.css` (`:root` for dark, `[data-theme="light"]` for light). All tokens must have values in both themes.

---

## Tech Stack

- **Backend**: Python 3.12, Flask 3.x, Gunicorn (production)
- **Data**: Pandas, NumPy, PyArrow, Parquet format, NDJSON
- **Analysis**: Scikit-learn, SciPy, Statsmodels, Seaborn
- **Storage**: Local filesystem (default `~/fyp_local`) or Google Cloud Storage
- **AI/LLM**: Google Gemini (Vertex AI or Gemini API), hosted Qwen (DashScope), local Qwen/MiniCPM via MLX (optional extras)
- **Scraping**: yt-dlp (primary), BeautifulSoup4, browser-cookie3
- **Frontend**: Vanilla JS, Jinja2 templates, Plotly, no build step
- **Auth**: Flask-Login, Flask-WTF (CSRF), JSON-file user store

---

## Project Structure

```text
foryou-research/
├── __proj__.py                  # Empty sentinel — marks project root
├── DEVELOPING.md                    # This file (maintainer guide: style, layout, patterns, deployment)
├── config/
│   ├── config.toml              # Active config (paths, GCS, Gemini, labels)
│   ├── legacy_annotation_prompt.txt # Retained pre-versioning "v0_legacy" prompt — display-only, shown by the Admin → Versions viewer (not used for go-forward annotation)
│   ├── annotation_contract.toml # Declarative source for the Gemini prompt + response_schema + flattener (sectionless flat prompt since 2026-07; scale inferred from field shape except free-text categorical/text)
│   ├── scrape_contract.toml     # Declarative source for the canonical cross-platform scrape schema (base + per-platform fields)
│   ├── activity_contract.toml   # Declarative source for the platform-agnostic activity schema (ingest required columns + required-core hard-drop set + derived local_*/session fields)
│   └── derived_contract.toml    # Declarative source for merge-derived columns (days_since_created/completion_rate/scraped_fail, niche/niche_name + the embedding-geometry measures typicality_pct/niche_isolation_pct, desc_hashtags/desc_raw, status flags)
├── fyp/                         # Core Python package — five subpackages (see docs/fyp-import-graph.md).
│   │                            #   The old flat paths (fyp/data_io.py, fyp/pca.py, ...) remain importable
│   │                            #   forever as alias shims (same module objects); prefer subpackage paths in new code.
│   ├── __init__.py              # Import-free: docstring + __version__ only (never import submodules here)
│   ├── core/
│   │   ├── fyp_config.py        # Config loader; lazy get_config() + PEP 562 `fyp_cf`; root via __proj__.py sentinel or FYP_CONFIG_PATH
│   │   ├── paths.py             # PROJECT_ROOT + the run_*.py *_SCRIPT constants + PYTHON_EXEC (re-exported from fyp_config)
│   │   ├── data_io.py           # Unified I/O (local + GCS, parquet, JSON, ndjson); runtime register_location() + local_copy()/release_local_copy() (temp-file zip/binary reader)
│   │   ├── types.py             # PyArrow dtype helpers and conversion
│   │   ├── polars_ops.py        # Polars helpers for expensive pandas ops at scale
│   │   ├── memory.py            # Shared RSS/peak probes + mem_probe() context manager ([<TAG>][MEM] log lines)
│   │   ├── utils.py             # Shared utility functions (incl. repair_mojibake() + read_zip_members()/read_zip_member())
│   │   ├── media_paths.py       # Platform-aware media object paths ({prefix}/{platform}/{id}.mp4) + reader-side resolve_media() fallback to the legacy flat path
│   │   ├── logging_setup.py     # get_logger(): stdout logging, bare %(message)s, level from FYP_LOG_LEVEL
│   │   ├── registry_metadata.py # Shared per-version field_metadata snapshot + union helpers for the three registries
│   │   ├── structure_sentinel.py  # DDP structure-drift detection: learned baselines + per-file verdicts; quarantine + approve/reject review flow
│   │   ├── activity_contract.py # Loads/validates config/activity_contract.toml; activity field set + required_columns / required_core_fields (hard-drop)
│   │   ├── activity_versioning.py # Activity-contract version registry (acv_ hash) + per-row activity_contract_version provenance
│   │   └── derived_contract.py  # Loads/validates config/derived_contract.toml; owns var_schema metadata for merge-derived columns
│   ├── ingest/                  # Package (replaced the old fyp/ingest.py module); __init__ re-exports the old API and
│   │   │                        #   imports all platform modules EAGERLY (class definition registers upload locations)
│   │   ├── base.py              # ForYouBaseCollection ABC + ForYouCollection; parse_donor_timezone(); registered_raw_locations();
│   │   │                        #   per-file intake stats (file_stats_this_run: true raw counts + not_parseable/missing_required
│   │   │                        #   drop reasons) persisted via the ingestion ledger (processed_rows/deduped_rows/dropped)
│   │   ├── tiktok.py            # TikTokDDPCollection / TikTokAIOCollection / TikTokZeeschuimerCollection / TikTokDemoCollection (synthetic demo, data_source="demo")
│   │   ├── instagram.py         # InstagramDDPCollection
│   │   └── youtube.py           # YouTubeDDPCollection
│   ├── scrape/                  # Package (replaced the old fyp/scrape.py module); __init__ re-exports the old API
│   │   ├── scrape.py            # Platform-agnostic scrape orchestration (per-platform queue, batching, threads, consolidation, legacy-parquet migration)
│   │   ├── scrape_queues.py     # Per-platform scrape queue files (to_scrape_<platform>.json): naming, legacy-queue migration, load/append/prune
│   │   ├── platform_scraper.py  # BaseScraper ABC + auto-registry + get_scraper() factory; ThrottleController; shared per-K / plays_per_day derivations
│   │   ├── scrape_contract.py   # Loads/validates config/scrape_contract.toml; the canonical scrape field set + PyArrow dtypes
│   │   ├── scrape_versioning.py # Scrape-contract version registry (sv_ hash) + per-row scrape_contract_version provenance
│   │   ├── tiktok_dl.py         # TikTokScraper(BaseScraper) + yt-dlp helpers (download, retry, error classification, 32-bit overflow repair)
│   │   ├── instagram_dl.py      # InstagramScraper(BaseScraper) — yt-dlp, cookie-authenticated; image posts fail permanent:no_video (phase 1)
│   │   ├── youtube_dl.py        # YouTubeScraper(BaseScraper) — yt-dlp, 720p DASH-merge media, bot_check throttle category, EJS/deno n-challenge solver
│   │   ├── scraper_alerts.py    # Persistent per-platform "scraper needs attention" alerts (cache/scraper_alerts.json; raised on permanent storms, auto-cleared on a healthy batch, surfaced on enrichment cards + System Health)
│   │   └── scraper_cookies.py   # Per-platform cookie plumbing (secrets/{platform}_cookies.txt on GCS, /tmp cache, Chrome locally, cookie_health)
│   ├── annotation/
│   │   ├── backends/                 # Pluggable annotation backends: AnnotationBackend ABC + registry (base.py, __init__.py), gemini / qwen_api / qwen_local / minicpm_local, variants.py, settings.py, per-backend support/patch modules
│   │   ├── machine_annotation.py     # Annotation orchestration (queue batches, threading; dispatches to the active backend)
│   │   ├── machine_annotation_batch.py # Batch-mode annotation (Gemini Batch API only)
│   │   ├── annotation_contract.py    # Loads/validates config/annotation_contract.toml; builds FIELD_SPECS from it
│   │   ├── annotation_schema.py      # Generates prompt + response-schema + structured flattener from the contract
│   │   ├── annotation_versioning.py  # Annotation version registry (av_ hash + per-version field_metadata snapshots); drives legacy-field ownership
│   │   ├── recode_variables.py       # Variable recoding, feature engineering
│   │   ├── var_presentation.py       # Admin-editable presentation store (users/var_presentation.json) — owns the four web_*_prio surface flags
│   │   ├── irrelevant_words.py       # Admin-editable hashtag stoplist (users/irrelevant_words.json) + squeeze/wildcard matcher used by recode_tokenise
│   │   ├── ab_eval.py                # Prompt A/B testing harness (arm runs, agreement metrics, reports)
│   │   └── human_eval.py             # Human annotation input (coding tasks, ICR metrics, blind votes, invitations)
│   └── analysis/
│       ├── organize_datasets.py # Dataset filtering & organisation (incl. new_merge)
│       ├── donations.py         # Donation-level data handling (AIO/AWS fetch, collection metadata)
│       ├── calc_collection_stats.py  # Donation-level statistics
│       ├── activity_analysis.py # Activity-based analysis
│       ├── embeddings.py        # Dense semantic embeddings for annotated videos (model-scoped shard store, backend-dispatched)
│       ├── embedding_store.py   # Random-access dense sidecar over the shards: per-model float16 parts + id→row index + fingerprint-stamped corpus mean (memmap local / ranged reads GCS)
│       ├── embedding_backends/  # EmbeddingBackend ABC + registry: gemini (default) / qwen_local (Qwen3-Embedding via sentence-transformers)
│       ├── niche_detection.py   # Data-driven micro-genre ("niche") detection from annotation text
│       ├── video_map.py         # Cluster video embeddings into niches + 2D semantic map (+ video_map_meta.json provenance; term-based niche naming when Gemini is absent). Also emits the two per-video **percentiles** `typicality_pct` / `niche_isolation_pct`, joined into every study frame by `organize_datasets._join_niche_columns` as numeric measures (so they reach the Correlations tab as group means per collection-day). Percentiles, not the raw cosine/PCA distances, because those scales drift with every rebuild. Both are NULL for videos not yet in the map, and the PCA build drops rows with any null feature — so an out-of-date map silently shrinks the correlations frame for **every** variable (logged as a warning at merge time; fix by refreshing embeddings + the video map BEFORE recoding studies)
│       ├── session_profile.py   # Within-session begin→end profiling
│       ├── sequence_analysis.py # Sequence-windowing analysis (dwell→next-window lift)
│       ├── sequence_model.py    # Stage-B predictive modelling for sequence analysis
│       ├── timeline_analysis.py # Timeline metrics (linreg, anomalies, breaks, volatility)
│       ├── pca.py               # Distance metrics, PCA helpers
│       ├── stats.py             # ANOVA, PERMANOVA helpers
│       └── studies.py           # Study definitions
├── web_interface/
│   ├── fyp_data_hub.py          # Flask app entry point (port 5002)
│   ├── data_service.py          # Study cache, PCA computation
│   ├── auth.py                  # Authentication, @admin_required decorator
│   ├── security.py              # Login manager, user manager
│   ├── permissions.py           # Tab + sub-page permission catalog and Flask decorator
│   ├── admin_settings.py        # Persisted admin-controlled site settings (e.g. signup gating)
│   ├── activity_log.py          # Per-user activity log for Data/User Management mutations
│   ├── process_manager.py       # Background job management (subprocess + Cloud Tasks)
│   ├── run_logs.py              # Durable process logs (proc_logs/<status_key>.json in "cache"): last 10 runs
│   │                            #   per process, timestamped once in append(), "Started by <user>" banner,
│   │                            #   CAS writes + per-key flusher thread. Shared by both execution modes and
│   │                            #   every admin; read by GET /api/logs/<name>
│   ├── task_status.py           # GCS/local status reporters, heartbeat, cancellation
│   ├── worker_runner.py         # Shared CLI entrypoint for the run_*.py workers (argparse + reporter + fail wrapper)
│   ├── semantic_trajectory.py   # Collection-trajectory overlay computation for Semantic Space
│   ├── explorer_backend.py      # Data explorer backend logic
│   ├── slack_service.py         # Slack integration
│   ├── mail_utils.py            # Email utilities
│   ├── run_queue_annotator.py   # Gemini annotation (self-chaining Cloud Task)
│   ├── run_queue_scraper.py     # Per-platform scraping worker (queue_scraper_<platform>; --platform / task_args platform)
│   ├── run_timelines_refresh.py # Timeline updates worker
│   ├── run_meta_refresh_groups.py  # Group + Video Analysis metadata refresh (Cloud Task)
│   ├── run_pca_refresh.py       # PCA/correlations refresh (Cloud Task)
│   ├── run_recode_refresh_studies.py  # Study recoding (Cloud Task)
│   ├── run_consolidate_enrichment.py  # Consolidation (Cloud Task)
│   ├── run_study_refresh.py     # Single-study refresh (Cloud Task)
│   ├── run_ingest_refresh.py    # Per-file row-count + provenance snapshot (Cloud Task)
│   ├── run_collection_metadata_refresh.py  # Regenerate collections_metadata.parquet (Cloud Task)
│   ├── run_collection_delete.py # Delete a collection from recoded/metadata parquets (Cloud Task)
│   ├── run_aio_fetch.py         # Fetch recent AIO donations + participant metadata from AWS (Cloud Task)
│   ├── run_embeddings_refresh.py   # Embed not-yet-embedded annotated videos (Cloud Task)
│   ├── run_video_map_refresh.py    # Cluster embedding store into niches + 2D map (Cloud Task)
│   ├── run_sequence_refresh.py  # Refresh sequence-analysis artifacts (Cloud Task)
│   ├── run_sessions_refresh.py  # Build the Sessions tab's session index + binge-episode/window artifacts (self-chaining Cloud Task; O(batch) memory, per-link shards, corpus-mean drift guard). Study-window-scoped: only collections in >=1 study, within the padded union of their studies' date windows. Incremental: stale_only mode refreshes only collections whose windows/in-window play count changed (merge publish replaces just their rows; per-collection provenance in sessions_meta.json); a targeted `collections` run also merges; no-args = force-full. **Enrichment staleness is global, not per-collection**: a changed embedding-store or annotation-corpus fingerprint forces a FULL rebuild, because the per-collection fingerprint comes from the activity file, which carries no enrichment columns (before 2026-08-16 it probed for an `annotated_ok` column that file never had, so new annotations could never mark anything stale and every stale_only run no-op'd). Chained automatically after every study save (pipeline_remaining, skip_if_busy)
│   ├── run_benchmark_parquet_read.py  # Benchmark parquet read paths (Cloud Task)
│   ├── run_queue_annotator_batch.py   # Batch-mode Gemini annotation (Cloud Task)
│   ├── run_ab_eval.py           # Prompt A/B eval run (Cloud Task)
│   ├── run_retokenise_hashtags.py     # Retroactive hashtag-stoplist cleanup (Cloud Task)
│   ├── run_demo_dataset.py      # Generate + install the synthetic demo dataset (Cloud Task; admin button on DM → Ingestion)
│   ├── services/                # Backend logic extracted from routes: study_data, timeline_service,
│   │                            #   analysis_data, user_variables, stats_service, preview_cache, worker_status,
│   │                            #   methods_note (per-study methods/provenance note — {study}_methods.json in
│   │                            #   "cache", written by BOTH study-refresh workers on every refresh incl.
│   │                            #   short-circuit; uses the active-vs-preferred vocabulary)
│   ├── routes/                  # Flask Blueprints
│   │   ├── auth_routes.py       #   Login, signup, settings
│   │   ├── api_explorer_routes.py       #   Studies + Explore API + system-info + per-study methods note
│   │   ├── api_viewer_routes.py         #   Video Analysis + media streaming API
│   │   ├── api_timelines_routes.py      #   Timelines API
│   │   ├── api_correlations_routes.py   #   Correlations API
│   │   ├── api_collections_routes.py    #   Collection stats + annotation API
│   │   ├── api_semantic_space_routes.py #   Semantic Space tab API (video embedding map)
│   │   ├── api_sessions_routes.py       #   Sessions tab API (session index + binge episodes + low-entropy sequences)
│   │   ├── management/          #   Admin/management endpoints — per-domain submodules (studies, collections,
│   │   │                        #     enrichment, contracts, ab_eval, schema, ingestion) on ONE shared blueprint
│   │   ├── management_routes.py #   Compatibility shim re-exporting the management package
│   │   ├── human_eval_routes.py #   Human annotation input (coding, votes, invitations)
│   │   ├── public_routes.py     #   Public (unauthenticated) mini-site pages: /about, /guide, /faq
│   │   └── process_routes.py    #   Background process endpoints
│   ├── templates/
│   │   ├── base.html            # Base layout
│   │   ├── index.html           # Main SPA shell
│   │   ├── login.html / signup.html  # Form-only pages on the public layout
│   │   ├── public/              # Public mini-site: base_public.html + _header/_footer partials,
│   │   │                        #   landing.html (anonymous /), about.html, guide.html, faq.html
│   │   └── tabs/                # Tab content templates
│   │       ├── home.html
│   │       ├── video_analysis.html
│   │       ├── explore.html
│   │       ├── my_stuff.html
│   │       ├── semantic_space.html
│   │       ├── sessions.html
│   │       ├── data_management.html
│   │       ├── correlations.html
│   │       ├── timelines.html
│   │       ├── admin.html       # (+ admin/ and dm/ partial subdirectories)
│   │       └── ...
│   └── static/                  # JS + CSS (no bundler)
│       ├── main.js              # Tab navigation controller
│       ├── video_analysis.js     # Video analysis tab
│       ├── explore.js           # Data explorer tab
│       ├── correlations.js      # Correlations tab
│       ├── timelines.js         # Timelines tab
│       ├── semantic_space.js    # Semantic Space tab
│       ├── sessions.js          # Sessions tab (session explorer + episode inspector)
│       ├── study_state.js       # Shared study-state helper
│       ├── style.css            # Main stylesheet
│       ├── js/
│       │   ├── data_management.js
│       │   ├── variable_prefs.js     # Per-user "Customize variables" panels (gear buttons; deltas in user.settings.variable_prefs)
│       │   ├── admin_var_schema.js   # Var-schema admin viewer (metadata read-only; prio checkboxes save to /api/manage/presentation)
│       │   ├── admin_tab.js / my_stuff_tab.js  # Former inline template scripts (extracted verbatim)
│       │   └── admin_ab_eval.js / admin_contract_editor.js / admin_annotation_versions.js / admin_human_eval.js / human_coding.js
│       └── css/                 # (empty — styles in style.css)
├── tests/                       # pytest suite: unit/ + golden/ (annotation safety net) + bench/ + debug/ + conftest.py
├── tmp/                         # Temporary test/debug data
├── scripts/                     # verify.sh gate, gen_route_inventory.py, generate_demo_dataset.py (CLI over fyp/ingest/demo_dataset.py), migrations, adhoc/ one-offs
├── docs/                        # Human-oriented docs (architecture, configuration, pipeline, web layer, routes)
├── Dockerfile
├── pyproject.toml               # Packaging ([project] fyp-pipeline) + ruff + pytest config
└── requirements.txt          # Pinned deps for Docker (3.12) — production lock
```

---

## Key Files

- **`fyp/core/data_io.py`** (importable as `fyp.data_io`): Always use this module for file access. Abstracts local vs. GCS storage. Use named locations (`"cache"`, `"recoded"`, `"users"`) rather than raw paths.
- **`fyp/core/fyp_config.py`** (importable as `fyp.fyp_config`): Config loader. Locates the project root via the `__proj__.py` sentinel (or `FYP_CONFIG_PATH`). Config is LAZY: `get_config()` / the PEP 562 `fyp_cf` attribute initialize on first access, not at import. An optional gitignored `config/config.local.toml` overlay is deep-merged over the committed config.
- **`fyp/core/types.py`** (importable as `fyp.types`): PyArrow-aware dtype conversion helpers. Use these for dtype handling.
- **`web_interface/`**: Contains the Flask app routes and templates.
- **`web_interface/task_status.py`**: Status reporting framework. `GCSStatusReporter` for Cloud Tasks (writes to GCS with heartbeat), `LocalStatusReporter` for subprocess mode (stdout). Instantiate the one matching the execution environment (Cloud Tasks dispatch uses `GCSStatusReporter`; `__main__` subprocess mode uses `LocalStatusReporter`).
- **`web_interface/process_manager.py`**: Process lifecycle. `CLOUD_TASK_ELIGIBLE` set controls which processes use Cloud Tasks. `start_process()` auto-selects Cloud Tasks vs subprocess based on `K_SERVICE` env var.

---

## Running the Project

### Development

```bash
source .venv/bin/activate
python web_interface/fyp_data_hub.py
# → http://localhost:5002
```

### Cloud Run Deployment (Production)

The app runs on **Google Cloud Run** as two services sharing the same Docker image:

- **`fyp-data-hub`** — Web server (Flask/Gunicorn, 2 CPU, 4 GB)
- **`fyp-task-runner`** — Background task executor (8 CPU, 32 GB, timeout 3600s, concurrency 1)

**GCP Configuration:**
- Project: `<gcp-project>`, Region: `australia-southeast1`
- Cloud Tasks queue: `fyp-background-tasks` (max-attempts=4 with backoff; configure via `scripts/configure_task_queue.sh`). Retry is **app-controlled**: only the idempotent refreshes in `process_routes.QUEUE_RETRY_SAFE` return 503 (→ retried); every other task returns 200 on failure and is terminal. All failures land in the `cache/task_failures.json` ledger (`web_interface/task_failures.py`) — the dead-letter record, surfaced on Admin → System Information.
- Service account: the project's default compute service account
  (`<project-number>-compute@developer.gserviceaccount.com`)
- Base image: `australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-base:latest`
- App image: `australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-app:latest`

**Docker image structure (two layers):**
- **Base image** (`Dockerfile.base`): Python 3.12-slim + gcc + Rust + all pip deps. Only rebuild when `requirements.txt` changes.
- **App image** (`Dockerfile`): Thin layer on top of base — just copies application code. Fast to build (~1 min).

**Deploy steps (both services share the same app image):**

```bash
# 0. Rebuild base image (ONLY when requirements.txt changes — slow, ~5 min)
#    Build the base image locally, then push it to your registry:
docker build -f Dockerfile.base -t <registry>/<gcp-project>/foryou-hub-base:latest .
docker push <registry>/<gcp-project>/foryou-hub-base:latest

# 1. Build the app image (always required before deploying — fast, ~1 min)
gcloud builds submit \
  --tag australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-app:latest \
  --project=<gcp-project> --region=australia-southeast1

# 2. Deploy web server
gcloud run deploy fyp-data-hub \
  --image australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-app:latest \
  --region=australia-southeast1 --project=<gcp-project>

# 3. Deploy task runner
gcloud run deploy fyp-task-runner \
  --image australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-app:latest \
  --region=australia-southeast1 --project=<gcp-project>
```

**When to deploy which service:**
- UI/route/template/JS changes only → deploy just `fyp-data-hub`
- Task worker logic only (`run_*.py`) → deploy just `fyp-task-runner`
- Shared code (`fyp/`, `process_manager.py`, `task_status.py`) → deploy **both**
- Step 1 (build) is always required before any deploy
- Step 0 (base image) is only needed when Python dependencies change

### Background Workers (Local Dev)

```bash
python web_interface/run_queue_annotator.py   # Gemini annotation
python web_interface/run_queue_scraper.py --platform tiktok   # Scraping worker (per platform)
python web_interface/run_timelines_refresh.py # Timeline updates
python web_interface/run_meta_refresh_groups.py  # Group + Video Analysis metadata refresh
```

### Local Scrape-Queue Drain Against Prod GCS (Residential IP)

YouTube's bot wall blocks most media downloads from Cloud Run's datacenter IPs; a
laptop on a residential IP is not affected. Setting `FYP_FORCE_GCS=1` makes a local
process resolve **all** storage (`data`/`cache`/`media`) against the prod GCS bucket —
the same code path Cloud Run uses — while keeping local behavior for everything gated
on `K_SERVICE` (Chrome-profile cookies, stdout status reporting, no Cloud Tasks
dispatch, no `task_status/` or `process_stats.json` writes).

**Prerequisites (once):**
1. Run from the **deployed commit** — the scrape contract and var-schema synthesis are
   code-baked; a drifted branch would stamp mismatched versions/columns.
2. `gcloud auth application-default login` with write access to the prod bucket
   (plain ADC — no service-account key needed).
3. `ffmpeg` (DASH merge) and `node` or `deno` (n-challenge solver) on PATH.
4. Chrome logged into the research YouTube account; approve the macOS Keychain prompt
   on first cookie extraction (run interactively).
5. In the web UI: make sure `queue_scraper_youtube` is **not** running before
   starting the drain. While the drain runs it holds a **drain lease**
   (`local_drain_youtube.json` in `cache`, heartbeat every 30 s, stale after
   10 min — see `web_interface/drain_lease.py`): the web UI refuses to start
   that platform's scraper or a Consolidate while the lease is fresh, and the
   armed auto-consolidate defers. Queue writes themselves are atomic
   (`data_io.update_json` compare-and-swap), so a concurrent append is never
   lost; a concurrent worker would only mean duplicate work.

**Run:**

```bash
export FYP_FORCE_GCS=1
export FYP_GCS_BUCKET_NAME=<prod-bucket>
caffeinate -i python web_interface/run_queue_scraper.py --platform youtube --batch-size 200
```

The boot log must show `FYP_FORCE_GCS set. Forcing all storage to GCS.` (the
`__main__` default batch size is 5 — pass a real value; `caffeinate -i` prevents
sleep mid-drain). Interrupting mid-batch is safe: unpruned items are re-scraped and
already-uploaded media is skipped by `check_existing_media`.

**Verify / finish:** check `gs://<bucket>/media/youtube/` for new mp4s and
`gs://<bucket>/data/scrape/` for new `scrapes_*.parquet`; the queue JSON at
`gs://<bucket>/data/cache/to_scrape_youtube.json` shrinks per batch. Close the shell
(drops `FYP_FORCE_GCS`), then run **Consolidate & Refresh** from the web UI — it
folds in the locally-written parquets automatically.

---

## Configuration

**`config/config.toml`** — primary config (committed). Machine-local values go in the optional gitignored **`config/config.local.toml`** overlay (deep-merged over it at load; template: `config/config.local.toml.example`) — never edit the committed file for personal paths. Key sections:

| Section | Key fields |
|---|---|
| `[machine]` | Annotation backends — one `[machine.<backend>]` block each (gemini/qwen_api/qwen_local/minicpm_local: model, params, optional `pricing`), variants at `[machine.<backend>.variants.<name>]`, generic `max_duration_for_annotation`. Legacy flat `[machine]` Gemini keys + flat `[machine.variants]` are hoisted at load by `fyp_config._normalize_machine_config` |
| `[paths]` | `local_data` (default: `~/fyp_local`, expanded per-user) |
| `[data_io]` | GCS bucket, `use_gcs_*` toggles |
| `[misc]` | Timezone (`Australia/Brisbane`), `local_mode` |
| `[labels]` | Content categories, generic mapper, irrelevant-words seed (`IRRELEVANT_WORDS` seeds the admin-editable hashtag stoplist `irrelevant_words.json`, location `users`, managed by `fyp/annotation/irrelevant_words.py` — Admin → Hashtag Stoplist; squeeze + trailing-`*` prefix matching, applied at recode time, never hash-affecting) |

**Four declarative TOML contracts** sit alongside it and own their variable schemas (overlaid onto `var_schema` at config load, read-only in the admin editor): `config/annotation_contract.toml` (annotation fields + the generated prompt/response schema, backend-agnostic), `config/scrape_contract.toml` (canonical cross-platform scrape fields), `config/activity_contract.toml` (platform-agnostic activity schema), and `config/derived_contract.toml` (merge-derived columns). The full contract-system guide (authoring keys, validation, the runtime upload/promote flow, migration costs) is `docs/contracts.md`. Key facts:

- `var_schema.csv` is **fully retired**: `fyp_config.load_var_schema` synthesizes the in-memory `var_schema` from the contracts + registries and fills the four `web_*_prio` membership columns from the admin-editable presentation store (`var_presentation.json`, location `users`, managed by `fyp/annotation/var_presentation.py`).
- Contracts own each field's `skip_recode` flag (short-circuits the recode plan for columns produced elsewhere; defaults per contract — annotation: false, scrape/activity: the field's `derived` flag, derived contract: true — with explicit per-field overrides).
- The retired `source` column is gone; the admin page shows a computed `origin` (which contract/registry owns the field) instead, and legacy registry snapshots' stored `source` strings remain only as a read-only skip fallback.
- The admin schema editor is read-only for metadata; only the on/off surface checkboxes save (POST `/api/manage/presentation`, etag-guarded, never hash-affecting).
- Each contract also has a **version registry** (`annotation_versioning`/`scrape_versioning`/`activity_versioning`, id prefixes `av_`/`sv_`/`acv_`) that stamps per-row provenance; the annotation registry additionally snapshots per-version `field_metadata` so **superseded ("legacy") fields stay contract-owned/read-only** — unioned into the overlay and badged "legacy" in the admin editor.

See *Scrapers: Base Class + Declarative Contract* below.

---

## Key Patterns & Conventions

### Project Root Discovery
`__proj__.py` is an empty sentinel file. `fyp_config.py` walks up the directory tree looking for it to locate the project root — this makes imports work regardless of working directory.

### Import-Cycle Rule (fyp_config auto-initializes at import)
`fyp/fyp_config.py` runs `initialize()` + `load_var_schema()` at MODULE IMPORT. Never add a module-level `from fyp.fyp_config import fyp_cf` (or `import fyp.data_io`) to modules that the load-time contract overlays call into (`data_io`, the three `*_versioning` modules, `var_presentation`) — a partially-initialized module mid-cycle once made the overlays silently drop legacy metadata and the schema hash drift per-instance. Use function-level `_cf()` / `_data_io()` accessors (see those modules); `tests/unit/test_import_cycle_hash.py` guards this.

### Data I/O Abstraction
`fyp/core/data_io.py` abstracts local vs. GCS storage. Use named locations (`"cache"`, `"recoded"`, `"users"`) rather than raw paths. Toggle `use_gcs_*` flags in config to switch backends. Locations can also be registered **at runtime** via `data_io.register_location(name, abs_path)` (must live under `paths.local_data`; idempotent; auto-derives the `gcs_paths` entry in GCS mode) — this is how collection classes self-register their raw-upload directory without a static `fyp_config` edit. For reading a stored object as a local file (e.g. unzipping a donation), `data_io.local_copy(location, filename)` returns a local path (downloading from GCS to the temp dir when needed) and `data_io.release_local_copy(path)` cleans up the temp copy afterwards (a no-op for a real local path).

### Data Ingestion: Base Class + Per-Platform Collections
`fyp/ingest/` mirrors the scraper's design (base class in `fyp/ingest/base.py`, one module per platform): a `ForYouBaseCollection` ABC with an `__init_subclass__` auto-registry and per-platform subclasses. Each subclass carries `source_platform` / `raw_path` class attributes and implements two hooks — `load_single_raw(filename)` (read one raw donation into a per-file DataFrame) and `process_single(df)` (produce `utc_timestamp` and finalize). The base class owns everything generic: the activity schema (`REQUIRED_COLUMNS`, from `config/activity_contract.toml`), the load loop (manifest, per-file donor-timezone, ledger, dedup), `_finalize_activity_frame()` (drops unparsed timestamps, sets `tz_offset`, sorts chronologically), and `save_enrichment_seed()`.

**Adding a platform is one class, nothing else.** At class definition (import time) `__init_subclass__` registers the class in `ForYouBaseCollection._registry` **and** self-registers its raw-upload location (`activity_data/{source_platform}/{raw_path}`) via `data_io.register_location()` — so a new platform needs no `fyp_config` edit and upload routes see the location before any collection is instantiated. `registered_raw_locations()` derives the whole upload-location list from the registry (used by code that must probe all locations, e.g. collection deletion). The three TikTok classes (`TikTokDDPCollection` / `TikTokAIOCollection` / `TikTokZeeschuimerCollection`) and the two new ones (`InstagramDDPCollection`, `YouTubeDDPCollection`) are the template.

**Instagram / YouTube DDP ingesters** parse **zipped** data-donation exports into the platform-agnostic activity schema. They read zip members with `utils.read_zip_members()` (over a `data_io.local_copy()` of the upload) and repair double-encoded captions with `utils.repair_mojibake()`:
- **`InstagramDDPCollection`** (`source_platform="instagram"`, `raw_path="instagram_raw"`) reads `your_instagram_activity/story_interactions/stories_viewed.json`, `ads_information/ads_and_topics/videos_watched.json` and `ads_and_topics/posts_viewed.json` (viewed reels / feed videos / feed posts → `activity_type="play"`) plus `likes/liked_posts.json` (liked posts → `fave`). The two feed-impression streams are what give a liked item a play row to fold onto. It supports **both** the current `label_values` record schema and the classic `string_list_data` / `string_map_data` schema. `item_id` is the reel/post shortcode parsed from the URL (nullable — classic story views carry no URL). Caption/owner are captured as enrichment-seed columns.
- **`YouTubeDDPCollection`** (`source_platform="youtube"`, `raw_path="youtube_raw"`) parses Google Takeout `history/watch-history.html`. `item_id` is the 11-char video id; organic watches → `play`, served ad impressions ("From Google Ads", detected from the details/caption cell) → `activity_type="ad_play"`, and non-video events (Shorts-creation, community-post views) are dropped. Timestamps are parsed from the account's display locale — day-first *and* US month-first, 12-hour AM/PM (incl. narrow/no-break spaces), with an abbreviated zone or an explicit `GMT±HH:MM` offset. Engagement comes from the optional Takeout CSVs: `comments/comments.csv` → `comment` rows (text in `extra_data`), `playlists/Liked videos.csv` → `fave`, `playlists/Favorites videos.csv` → `save` — all exact-timestamped and video-id-keyed.

**Engagement→play linking (`extra_data`).** `derive_play_duration()` (shared, platform-agnostic) folds engagement activities (fave/comment/share/follow/save) into a play row's `extra_data` as comma-separated `"<atype>[:context]"` tokens — first via chronological same-item adjacency runs (which also attribute dwell), then via a **same-item nearest-play fallback** for engagement that is not adjacent to any play (e.g. an IG like whose only logged view of that item is days earlier — IG's impression streams log a view once per item). Only `extra_data` is affected by the fallback; `play_duration` stays strictly adjacency-based. This folded token is the **only** engagement signal that survives into studies (which filter to play/observe rows) — see `data_service.py` / `explorer_backend.py`.

**Enrichment seed (donated item metadata).** A subclass populates `seed_*` scratch columns (`seed_desc`/`seed_author_id`/`seed_author_name`/`seed_create_time`) in `load_single_raw`; `process()`'s column filter drops them from the activity rows, but `save_enrichment_seed()` persists them separately as a per-platform `{source_platform}_{data_source}_enrichment_seed.parquet` in the **canonical scrape-base schema** (`config/scrape_contract.toml`), keyed on `(source_platform, item_id)` with `scrape_status="donated"` and a `scrape_contract_version` stamp. It merges across ingest runs (existing rows survive; a captioned row wins a key collision over a caption-less duplicate). A later scrape/consolidation can use the seed as a lowest-precedence fallback for items that can't be scraped. It is a no-op for platforms that populate no seed columns (e.g. TikTok). Consolidation merges the seeds as a **lowest-precedence fallback**: `fyp.scrape._merge_enrichment_seeds` anti-joins donated rows against real scrape rows on `(source_platform, item_id)` and appends the rest with `scraped_ok=False`/`video_downloaded=False`, so unscraped (or permanently unfetchable) items surface their donated caption/author in Explore while staying scrape-eligible; a later real scrape supersedes the donated row on the next consolidation, and seed-file row counts participate in consolidation change detection. (Donated-seed rows carry `video_downloaded=False`, so they are never annotation-eligible until really scraped.)

**Donor-timezone override.** The ingestion manifest accepts an optional `tz` per file — an IANA zone name (`Asia/Kolkata`, preferred) or a fixed offset (`+05:30`, `-8`) — collected in the upload modal and validated at upload time by `fyp.ingest.parse_donor_timezone` (an unrecognised value is rejected with HTTP 400, not silently dropped). When present it is the **authoritative** source for local-time conversion, overriding the (sometimes ambiguous, e.g. IST) timezone label in the export — YouTube needs it to interpret local wall-clock times unambiguously. Caveat: the activity contract stores `tz_offset` as **integer hours**, so half-hour zones truncate (e.g. `+05:30`→5); `utc_timestamp` stays exact.

**Structure sentinel — silent format drift is quarantined, not ingested.** `fyp/core/structure_sentinel.py` learns each (platform, data_source)'s upload structure — zip members, typed JSON key paths, HTML markers — plus per-file sanity stats (rows/MB, kept ratio = timestamp parse rate, null-item_id fraction, seed fill rates) into `structure_baselines.json` (location `recoded`).

- During an ingest refresh (`run_ingest_refresh` injects a per-run `StructureSentinel` into every sub-collection) each new file is checked twice: Phase A (raw structure, inside `load_raw` right after `load_single_raw`) and Phase B (processed-stat drift, before `migrate_sub_collections`).
- A deviating file's rows are withheld and its ledger outcome set to `quarantined_structure` (a `LEDGER_SKIP_OUTCOMES` member), with the verdict + findings persisted to `structure_verdicts.json` for the Data Pipeline → Ingest Collections "Structure review" panel (approve = learn structure + un-ledger → next refresh ingests; reject = `manually_excluded`).
- Missing core structure / changed types / hard stat outliers quarantine; purely additive changes only warn (tunable via `ADDITIVE_QUARANTINES`). Baselines under 3 accepted files are learn-only (never quarantine); stat checks need 5.
- Bootstrap from already-uploaded history with `python scripts/bootstrap_structure_baselines.py`. Sentinel failures never block ingestion (log-and-ingest), and `sub.sentinel = None` (e.g. ad-hoc scripts) behaves exactly as before.

**Robustness — parse failures stay pending, they are not discarded.** A structural failure in `load_single_raw` (unreadable zip, missing member, invalid JSON, unsupported timestamp locale) **raises**; the load loop logs it and leaves the file pending — the file is *not* added to `discarded_raw_files` and gets no ledger skip-outcome, so it is retried on the next refresh (e.g. after a parser fix for a new export-format variant). This is distinct from a legitimately-too-small donation, which *is* ledger-recorded as discarded.

**Pre-scraper merge safety (`organize_datasets.new_merge`).** A freshly-ingested platform has activity rows but no scrape/annotation enrichment yet. `new_merge` now **always** emits the enrichment-status and derived columns for both branches: `_ensure_enrichment_status_columns` guarantees `scraped_ok` / `annotated_ok` / `annotated_fail` / `video_downloaded` (False-filled when absent) and `_add_merge_calculated_columns` guarantees `days_since_created` / `plays_per_day` / `scraped_fail` / `completion_rate` (NA/False-defaulted when their inputs are absent) — so Explore / Video Analysis, which gate on these flags, render a clean empty result instead of erroring on a missing column. Relatedly, `update_enrichment_status`'s item-id-length sanity filter is now **per `source_platform`** (modal id-length computed within each platform group): a single global modal length would drop every shorter-id platform's items (TikTok ~19 digits vs Instagram/YouTube ~11 chars). It falls back to the global modal when `source_platform` is absent.

### Scrapers: Base Class + Declarative Contract
The scraper mirrors the collection-ingestion design in `fyp/ingest/`. `fyp/scrape/platform_scraper.py` defines `BaseScraper` (an ABC with an `__init_subclass__` auto-registry and a `get_scraper(platform)` factory) plus the shared, platform-agnostic derivations (per-K engagement rates, `plays_per_day`, column standardization). `fyp/tiktok_dl.py` holds `TikTokScraper(BaseScraper)` (yt-dlp/pyktok); `fyp/instagram_dl.py` and `fyp/youtube_dl.py` hold the Instagram and YouTube scrapers (both yt-dlp, authenticated via `fyp/scraper_cookies.py` — per-platform `secrets/{platform}_cookies.txt` on GCS with a 6h /tmp cache, Chrome-profile cookies in local dev, and a `cookie_health(platform, session_cookie=...)` probe that degrades to file-age status when the session cookie has no expiry row, e.g. YouTube's `__Secure-3PSID`). `fyp/scrape/scrape.py` is platform-agnostic orchestration (threading, queue, consolidation) and calls the active scraper through the base interface.

**Media duration cap (all platforms).** `BaseScraper.media_duration_cap()` reads the optional `[misc] max_duration_for_download_<platform>` config key, falling back to the global `max_duration_for_download` (300s); each `fetch()` calls `should_download_media(duration)` between its metadata and media phases. Skipping for length is not an error — the metadata row is saved with `scrape_status="ok"` and `video_downloaded=False`. Most YouTube watch-history items are long-form and deliberately stay metadata-only; Shorts/clips get media (720p-capped DASH merge). YouTube format extraction needs the n-challenge solver: `yt-dlp-ejs` (requirements.txt) plus a JS runtime (deno in the Docker base image; node works locally) — metadata extraction is solver-independent via `ignore_no_formats_error`. Instagram image-only posts fail `permanent:no_video` in phase 1 (no carousel support; the donated seed compensates), and Instagram's ambiguous "rate-limit reached or login required" is kept transient so throttled items stay queued. YouTube's bot wall is a distinct `bot_check` category in `_THROTTLE_CATEGORIES` (shrinks concurrency). YouTube media streams from datacenter IPs additionally require proof-of-origin tokens: the **bgutil PO-token provider** is integrated in script mode (`bgutil-ytdlp-pot-provider` pip plugin in requirements.txt + the matching provider script built with Node 22 in `Dockerfile.base` at `/opt/bgutil-ytdlp-pot-provider/server`, env `BGUTIL_POT_SERVER_HOME`, wired via `youtube_dl._pot_extractor_args()`; a no-op locally where the script is absent). Note: even with PO tokens, a flagged/rotated cookie session can still hit the bot wall — re-export cookies from a closed incognito session if downloads stall.

A new platform (Instagram Reels, YouTube Shorts, …) is **one subclass** implementing five required hooks — `item_url`, `fetch`, `map_to_canonical`, `classify_error`, `repair_counts` (plus optional `throttle_limits`/`health_check`/`image_count`/`prepare_raw_batch`/`fetch_slideshow_audio` overrides) — registered in `_SCRAPER_MODULES`, plus a `scope="platform"` block in the contract. The orchestration plumbing is then automatic: a per-platform queue (`to_scrape_<platform>.json`), a per-platform worker process (`queue_scraper_<platform>`, its own Cloud Task chain), a scraper UI block on the Scrape page (Data Pipeline → Scrape), and a per-platform media subdirectory all appear with no orchestration edits. The complete add-a-platform checklist, including the supporting steps outside this package, is in `docs/extending.md`.

**Media-phase failure handling & storm guards (generic).** When `fetch()` succeeds on metadata but the media download fails, the returned row carries `df.attrs['media_error_type']` / `media_error_detail` (all three scrapers implement this; contract documented on `BaseScraper.fetch`). The orchestrator saves the metadata row either way; a **transient** media error keeps the item in the scrape queue for a media retry (excluded from the queue prune) and feeds the throttle controller. Batch-level guards stop a broken session from churning the queue:

- **Circuit breaker** (`scrape.CIRCUIT_BREAKER_THRESHOLD` consecutive `rate_limited`/`bot_check` outcomes across fetch+media phases) aborts the batch and stops Cloud-Task self-chaining — YouTube rate-limits the whole session for up to an hour, and its rate-limit response is phrased as "Video unavailable…" (so YouTube's classifier checks rate-limit keywords **before** removal keywords). `BaseScraper.inter_request_delay()` (YouTube: 1.5s) paces workers while holding the throttle slot.
- **Permanent-storm guard** (`scrape._permanent_storm_threshold`, default 15, `[misc] scraper_permanent_storm_threshold`) catches broken sessions that fail every item with the same *permanent* classification (2026-07-16: a flagged IG session returned 404 → `permanent:removed` for live posts, pruning the whole batch): N consecutive identical `permanent:<category>` results abort the batch like the circuit breaker, demote those ids to transient (kept queued, excluded from the failed-scrapes record), and stop chaining (`permanent_storm_tripped`/`permanent_storm_category` attrs). Trade-off: a genuinely-dead homogeneous queue run stays queued and stops the worker — recoverable, unlike false pruning.
- **Transient-storm guard** (`scrape._transient_storm_threshold`, default 25, `[misc] scraper_transient_storm_threshold`) covers the retryable-side blind spot (2026-08-10: TikTok's new bot-challenge wall made yt-dlp fail every item with "No video formats found!" → `transient:unknown`, invisible to both the circuit breaker and the permanent-storm guard, so the worker churned the queue at 0% yield): N consecutive identical `transient:<category>` results abort the batch, stop chaining, and raise the same persistent alert (`KIND_TRANSIENT_STORM`); no demotion is needed — the items are already transient and stay queued.
- A storm raises a **persistent scraper alert** (`fyp/scrape/scraper_alerts.py`, `cache/scraper_alerts.json`, CAS via `data_io.update_json`): the Scrape page shows a red banner + failing health chip on that platform's scraper card and the Admin → System Information health panel shows a banner, until the next healthy batch auto-clears it or an admin dismisses it (POST `/api/manage/enrichment/scraper_alert/dismiss`). The failed-scrapes record stores each item's failure category so storms are diagnosable after the fact.
- The Scrape page's **"Retry missing media"** checkbox re-queues items that are `scraped_ok` but `video_downloaded=False` and within the platform's duration cap (unknown durations pass).

**Per-platform queues & workers.** Each platform has its own scrape queue `to_scrape_<platform>.json` (owned by `fyp/scrape_queues.py`; the legacy single `to_scrape.json` auto-migrates into the default platform's queue on first read) drained by its own `queue_scraper_<platform>` process. The platform rides in `task_args` and is carried through self-chaining; `process_manager.SCRAPER_PROCESS_NAMES` derives the process set from the contract's registered platforms. Every scraped row is stamped with `source_platform` (a `scope="base"` contract field with no var_schema metadata — the **activity** contract owns that var_schema row); it is backfilled to the default platform for pre-column history at consolidation, and the activity↔enrichment merge is composite on `(source_platform, item_id)`.

**Media layout.** New downloads write to `{gcs_media_prefix}/{platform}/{item_id}.mp4`; readers (viewer streaming, Gemini upload) resolve via `fyp/media_paths.resolve_media()` — the row's `storage_link` first, then the platform subpath, then the legacy flat `{item_id}.mp4` path. Existing flat TikTok media is **not** migrated; it keeps working via the fallback. `ThrottleController` lives on `platform_scraper` (generic; TikTok caps concurrency at 6 and reports cookie health via the `health_check` hook).

**Annotation backends.** Machine annotation is pluggable: `fyp/annotation/backends/` holds an `AnnotationBackend` ABC (auto-registry, `get_backend()`/`active_backend_name()`); the **raw-row dict** is the interface boundary — flatten/refine/versioning are backend-agnostic. The authoring checklist for a new backend is `docs/extending.md`. The four implementations:

- `gemini` — default; thin adapter over the historical `machine_annotation` path.
- `qwen_api` — hosted Qwen omni via DashScope's OpenAI-compatible intl endpoint, default `qwen3.5-omni-flash`; native video incl. audio, base64 upload, streaming-only, `json_object` + schema-in-prompt + fence-strip, 429 backoff; key `DASHSCOPE_API_KEY`, config `[machine.qwen_api]`, `cloud_run_capable=True`; throughput is account-rate-limit-bound ~5 videos/min, so `max_workers` stays small.
- `qwen_local` — Qwen3-Omni-30B via mlx-vlm, Apple Silicon only, frames+audio, llguidance-constrained JSON; mlx-vlm 0.6.x bugs patched in `qwen_rope_fix.py`, upstream Blaizzy/mlx-vlm#1619/#1620 (`mlx-vlm` ships as the `local_qwen` pyproject extra, never in requirements.txt).
- `minicpm_local` — MiniCPM-o 4.5 9B, same mlx-vlm frames+audio recipe reused from `qwen_local`'s helpers, ~8 GB peak so it fits 16 GB Macs; the published MLX quants need the checkpoint-naming patch in `minicpm_sanitize_fix.py`; extra `local_minicpm`, checks in `minicpm_support.py`.

Selection and configuration:

- Backend choice lives in the **admin settings store** (Admin → Backends; `fyp/annotation/backends/settings.py` is the read side, `web_interface/admin_settings.py` the write side); the five `[machine.gemini]` params (model/temperature/thinking_budget/media_resolution/max_output_tokens) are deliberately **config-file-only** (edit `config/config.toml` + rebuild/redeploy — the former runtime-override UI was removed 2026-07-21).
- **Backend variants** (`fyp/annotation/backends/variants.py`) let a `[machine.<backend>.variants.<name>]` config block declare a named selection = the parent implementation plus config overrides (typically `model` / `model_id`) — so a legacy and a new model version of the same backend stay selectable side by side (e.g. `gemini` on 3.0 and a `gemini_35` variant on 3.5). The admin setting `annotation_backend` stores either an implementation id or a variant name; variants inherit availability/`cloud_run_capable`/worker-width from their implementation, appear automatically in the Admin dropdown and the per-arm ab_eval picker, and fork their own `av_` versions (identity stays content-based: model+prompt+schema+params; the variant name is descriptor metadata only). The plain `gemini` selection keeps its byte-identical legacy hash path.
- Constraints: variant names are lowercase `[a-z0-9_]` and must not collide with a backend id; batch mode runs only on plain `gemini`; a local backend loads one resident model per worker process (switching local-model variants needs a worker restart).
- Every backend block/variant may carry `pricing = {input, output}` (USD per 1M tokens, metadata never hash-affecting) — `variants.selection_pricing()` feeds the A/B-evaluation cost display (Admin → Contracts).
- **Changing model/params forks a new `av_` annotation version automatically** (model + gen params are hashed into the version identity; the local backend's prompt addendum and frame/audio sampling params fold in too via `extra_params` — additive-only, existing Gemini hashes unchanged).

Safety nets:

- `annotation_configured()` dispatches to the active backend's `availability()` (hardware/deps/model-download checks for qwen via `qwen_support.py`, surfaced in Admin → Backends's requirements panel, `GET /api/manage/annotation/backends`, `scripts/setup.py --check-only`, and the System-Health annotation chip).
- The local backend refuses on Cloud Run (`cloud_run_capable=False`, plus a defense-in-depth guard in `process_manager.start_process`); batch mode stays Gemini-only (worker refuses otherwise).

A/B evaluation: ab_eval (the A/B-evaluation panel on Admin → Contracts) runs explicit test arms via `arms_spec` — the same contract may run as several arms under distinct labels, each with its own `backend` (UI: per-arm backend dropdown; the legacy `candidate_names`/`include_live`/`arm_params` API shape still works, incl. per-arm `model`/`temperature`). Numeric metrics report exact agreement + mean-abs-diff as headline (Pearson r flagged/suppressed under low variance); items lacking a usable annotation from both arms of a pair are excluded. See docs/installation.md#enabling-local-qwen-annotation.

**Embedding backends.** The semantic-space pipeline is pluggable the same way: `fyp/analysis/embedding_backends/` holds an `EmbeddingBackend` ABC (auto-registry, `get_backend()`/`active_backend_name()`). Chosen **independently** of the annotation backend via the admin setting `embedding_backend` (Admin → Backends → Embeddings; both set local ⇒ embeddings + map are fully cloud-free). Implementations:

- `gemini` — default; API details explicit in `[embedding.gemini]` (model_id/dim/location/task_type, defaults `gemini-embedding-001`@1536), so the model is upgradeable by config edit.
- `qwen_api` — hosted Qwen text embeddings via DashScope's OpenAI-compatible `/embeddings` endpoint, default `text-embedding-v4`@1024, config `[embedding.qwen_api]`, key `DASHSCOPE_API_KEY`, 10-inputs-per-request API cap, `cloud_run_capable=True`.
- `qwen_local` — `Qwen/Qwen3-Embedding-0.6B`@1024 via sentence-transformers — MPS/CUDA/CPU, no Apple-Silicon hard gate; config `[embedding.qwen_local]`; pyproject extra `local_embeddings`, never in requirements.txt.

Design points:

- Embedding backends deliberately have **no variant system** (unlike annotation): the model is a plain config value, and the model-scoped shard store already isolates outputs per model.
- The **shard store is model-scoped**: every row of `video_embeddings__*.parquet` stamps `model`/`dim`, and `embedded_item_ids()`/`load_embeddings()` filter to one model — switching backends re-embeds the corpus into new shards (old shards kept; switching back is free).
- `build_niche_map` consumes only the active model's vectors, writes provenance to `recoded/video_map_meta.json` (`embedding_model`/`dim`/`naming_mode`/…), and **niche naming degrades to deterministic term-based labels when Gemini is not configured** (the `_ask` seam is the hook for a future `local_llm` naming mode).
- Gating mirrors annotation: `process_routes.api_start` refuses `embeddings_refresh` when the active backend's `availability()` fails, `process_manager.start_process` + the consolidate pipeline skip Cloud-Run dispatch for a `cloud_run_capable=False` backend, `GET /api/manage/embedding/backends` feeds the admin requirements panel, and System Health has an `embedding` chip. The Semantic Space status endpoint reports `model_mismatch` (map built by a different model than the active backend) as staleness.

See docs/installation.md#enabling-local-embeddings.

**Annotation (all platforms).** Gemini annotation covers every platform. Eligibility at queue entry (`management_routes`): `scraped_ok` AND `video_downloaded` AND under `max_duration_for_annotation` — metadata-only items (e.g. YouTube long-form past the media duration cap) never queue. The queue (`to_annotate.json`) stays a bare list of item ids; each entry's platform is resolved via `machine_annotation.platform_map_for()` (an `enrichment_status.parquet` lookup, fallback: default platform) and drives media resolution (`media_paths.resolve_media`) plus the per-row `source_platform` stamp on annotation output. Annotation rows are keyed composite `(source_platform, item_id)` throughout (archive dedup, active view, the scrapes←annotations merge); legacy platform-less rows are backfilled to the default platform at consolidation.

**Annotation-version vocabulary (use these words, in code and UI).** Two pointers, never interchangeable: **active** = the version the NEXT annotation is stamped with — derived, not stored (`annotation_versioning.active_annotation_version()` / `active_version_descriptor()`, from the live contract + selected backend + gen params); **preferred** = the version studies read when an item was annotated under several — stored in the registry's `preferred` key, changed only by `promote_version()` (`get_preferred_version()`, `select_preferred_view()`, `rebuild_preferred_annotations_from_archive()`, `POST /api/manage/annotation-versions/promote`). The admin UI uses the same two words (Versions page: "Activate" / "Active" and "Prefer" / "Preferred"), and "current"/"live" are retired for this concept. Before 2026-07 the registry key was `active` and meant *preferred* — `load_registry()` migrates it on read (same for the scrape/activity registries). The ab_eval arm value `source: "live"` stays as a frozen wire value in stored run manifests but displays as "active contract".

**Photo/carousel posts.** Still-image posts are stored as slideshow mp4s and treated as videos downstream. Division of labor (documented on `BaseScraper`): the platform's `fetch()` downloads the source images as `{item_id}_{NN:02}.jpeg` and fails with a retryable `carousel` category when a photo post's images can't be extracted/downloaded; the orchestrator (`download_single_video`) detects image posts via the `image_count` hook, assembles `{item_id}.mp4` with `make_slideshow()` at `SLIDESHOW_SECONDS_PER_IMAGE` (2s) per image, muxes the post's audio track (music/TTS voiceover, fetched via the `fetch_slideshow_audio` hook — yt-dlp `bestaudio` for TikTok; failure degrades to a silent slideshow), uploads, and deletes the source jpegs. `prepare_raw_batch` converts the raw image-URL list to a count and overrides `duration = image_count × 2`. Slideshows built before 2026-07 are silent; historical media is not regenerated.

`config/scrape_contract.toml` is the **single declarative source for the canonical, cross-platform scrape schema** — the scraper's analogue of `annotation_contract.toml`. `fyp/scrape/scrape_contract.py` loads/validates it.

- It defines the **base** fields every platform emits (`scrape_status`, `storage_link`, `scrape_ts`, `source_platform`, `desc`, `create_time`, `author_id`, `duration`, `author_name`, `author_handle`, `play_count`, the generic absolute counts `fave_count` / `comment_count` / `share_count` / `save_count`, `comments_per_K_play` / `faves_per_K_play` / `shares_per_K_play` / `saves_per_K_play`, `plays_per_day`) and the genuinely platform-specific fields, each with its PyArrow dtype + var_schema metadata (role/scale/display_name/description/section).
- **Popularity counts and the author handle are platform-agnostic base fields**: each scraper's `_RAW_TO_CANONICAL` translates its platform names at scrape time (`stats_diggCount` / `ig_like_count` / `yt_like_count` → `fave_count`; `author_uniqueId` / `ig_author_handle` / `yt_author_handle` → `author_handle`; ...), and the flat `[perk]` table maps each `*_per_K_play` rate to its generic count.
- The registered platform list is the explicit `[meta].platforms` (a platform may own zero platform-scoped fields — Instagram does).
- At config load, `fyp_config._apply_contract_scrape_metadata` overlays that metadata onto `var_schema` (self-healing legacy→canonical rename via `LEGACY_COLUMN_ALIASES` + injection of any missing rows), and the admin schema editor renders those cells **read-only** — exactly like the annotation contract.
- Engagement per-K ratios and `plays_per_day` are derived at **scrape time**; legacy on-disk scrape parquets are migrated at consolidation by `_coalesce_retired_columns` (retired platform-specific columns → generic base fields per `scrape_contract.RETIRED_TO_GENERIC`; a coalesce, never a rename — several sources share one target) followed by `_canonicalize_legacy_scrape` (legacy base-name renames + rate re-derivation). The retired columns' `web_*_prio` surface flags migrate to their generic successors automatically inside `var_presentation.load_presentation()`.

### Parquet & PyArrow
Data is stored in Parquet. Complex types (dicts, lists) are JSON-stringified before storage. Surrogate characters are escaped. Use `fyp/types.py` helpers for dtype conversion.

### Thread Safety
`StudyCache` in `data_service.py` uses double-checked locking — be careful when modifying cache logic.

### Frontend
Single-page app with tab navigation controlled by `main.js`. All data endpoints return JSON; JS handles filtering and rendering. No bundler — JS files are served as-is from `static/`. **Per-user variable preferences**: each user can include/exclude variables per surface (filter / viz / detail-panel / timeline) via My Stuff → Preferences → Variable customizations (panels fed by the study-independent `GET /api/user/variable-catalog`) — stored as deltas in `user.settings.variable_prefs`, composed as `(global ∪ include) − exclude` (`static/js/variable_prefs.js` client-side; timelines AND the Explore filter-stats endpoint compose server-side — `/api/explore/filter` computes distribution stats only for the user's effective viz set).

### Role-Based Access
Use `@admin_required` decorator from `web_interface/auth.py`. User data lives in JSON files under `{local_data}/users/`.

### Background Jobs & Cloud Tasks
On Cloud Run, eligible background processes run as **Google Cloud Tasks** dispatched to the `fyp-task-runner` service. Locally, they run as subprocesses. The toggle is automatic via `K_SERVICE` env var.

**Architecture:**
- `process_manager.py` — `CLOUD_TASK_ELIGIBLE` set defines which processes use Cloud Tasks. `start_process()` dispatches via `_dispatch_cloud_task()` on Cloud Run, falls back to subprocess locally.
- `task_status.py` — `GCSStatusReporter` writes progress/data to GCS (`task_status/*.json`). Has a background heartbeat thread (30s interval) for stale detection. `LocalStatusReporter` prints `::PROGRESS::`/`::DATA::` to stdout for subprocess mode.
- `process_routes.py` — `internal_bp` blueprint receives Cloud Tasks HTTP requests at `/internal/run-task/<name>`. `TASK_FUNCTIONS` registry maps names to worker functions. `_run_task_with_stats()` handles execution, stats, and chaining.
- Each `run_*.py` worker has a `run_<name>(reporter, task_args)` function for Cloud Tasks and a `__main__` block for local subprocess mode.

**Self-chaining (queue_annotator):** Long-running annotation processes one batch per Cloud Task, then returns `{"chain": True, "next_task_args": {...}}` to dispatch the next batch. Each link inherits the GCS status via `reporter.resume()`.

**Cross-service data:** Both services share `process_stats.json` on GCS. Always call `load_process_stats()` before reading or writing to avoid clobbering the other service's data.

**Stale detection:** If a task's GCS heartbeat is >600s old, the UI and `start_process()` treat it as dead.

---

## Tests

pytest is configured in `pyproject.toml` (`testpaths = tests/unit`) with markers
`requires_data` / `requires_gcs` / `slow` / `stale`; the standard gate for every
change is:

```bash
source .venv/bin/activate
bash scripts/verify.sh
# = ruff (pyflakes bar) + pytest -m "not requires_data and not requires_gcs and not slow and not stale"
#   + the import-cycle/schema-hash guard + the golden safety net + an app import smoke
```

`tests/golden/` is the cost-free annotation regression suite (replays saved raw
Gemini responses — run it after touching any annotation code):

```bash
python tests/golden/run_safety_net.py
```

Key guard tests to know: `tests/unit/test_import_cycle_hash.py` (schema-hash
import-order independence), `test_lazy_config_boot.py` ([BOOT] exactly once,
lazy config), `test_subpackage_shims.py` (old-path aliases stay identical),
`test_url_map_snapshot.py` (HTTP endpoints frozen),
`test_task_status_stdout_contract.py` (::PROGRESS::/::DATA:: wire format).

New tests go in `tests/unit/`. Save test/debug data in the `tmp/` folder;
one-off scripts go in `scripts/adhoc/`, not `tests/`. Both `scripts/adhoc/`
and `tests/debug/` are gitignored — they are working scratch, and in practice
they collect production ids and resource names that must not be published.

---

## Main Entry Points

| File | Purpose |
|---|---|
| `web_interface/fyp_data_hub.py` | Web app (Flask, port 5002) |
| `web_interface/task_status.py` | Status reporters (GCS + local), heartbeat, cancellation |
| `web_interface/process_manager.py` | Process lifecycle, Cloud Tasks dispatch, subprocess fallback |
| `web_interface/run_queue_annotator.py` | Gemini annotation (self-chaining Cloud Task) |
| `web_interface/run_queue_scraper.py` | Scraping worker |
| `web_interface/run_consolidate_enrichment.py` | Consolidation + impact analysis (Cloud Task) |
| `web_interface/run_study_refresh.py` | Single-study stats/PCA/metadata refresh (Cloud Task) |
| `web_interface/run_recode_refresh_studies.py` | Study recoding (Cloud Task) |
| `web_interface/run_pca_refresh.py` | PCA/correlations refresh (Cloud Task) |
| `web_interface/run_meta_refresh_groups.py` | Group + Video Analysis metadata refresh (Cloud Task) |
| `web_interface/run_timelines_refresh.py` | Timeline refresh worker |
| `fyp/core/fyp_config.py` | Config access (`get_config()` / lazy `fyp_cf`) |
| `fyp/ingest/` | Data ingestion classes (base + per-platform modules) |
| `fyp/analysis/organize_datasets.py` | Dataset organisation & filtering |