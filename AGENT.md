# AGENT.md — FYP Research Platform

## Project Overview

**FYP (For You Project)** is a TikTok research data platform for academics. It ingests TikTok data ideo captures, enriches them via web scraping and LLM annotation (Google Gemini), performs statistical analysis (PCA, ANOVA, PERMANOVA), and presents findings through an interactive Flask-based web dashboard with role-based access control.

---

## Environment

- **Python**: 3.14 (the `.fypenv314` virtual environment).
- Always activate the venv before running scripts: `source .fypenv314/bin/activate`
- **Deployment**: Docker (Python 3.12-slim), Gunicorn (1 worker, 8 threads).
- **Secrets** (set via environment variables):
  - `GEMINI_API_KEY`
  - `FLASK_SECRET_KEY`
  - `FYP_GCS_BUCKET_NAME` (production)
  - `FLASK_DEBUG` (optional)
  - `K_SERVICE` (auto-set by Cloud Run — triggers GCS storage and Cloud Tasks dispatch)
  - `CLOUD_RUN_SERVICE_URL`, `GCP_PROJECT_ID`, `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_QUEUE`, `CLOUD_TASKS_SA_EMAIL` (Cloud Tasks config)

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

- **Backend**: Python 3.14, Flask 3.x, Gunicorn (production)
- **Data**: Pandas, NumPy, PyArrow, Parquet format, NDJSON
- **Analysis**: Scikit-learn, SciPy, Statsmodels, Seaborn
- **Storage**: Local filesystem (`/Users/<user>/fyp_local`) or Google Cloud Storage
- **AI/LLM**: Google Gemini (Vertex AI), OpenAI (secondary)
- **Scraping**: yt-dlp (primary), BeautifulSoup4, browser-cookie3
- **Frontend**: Vanilla JS + jQuery, Jinja2 templates, no build step
- **Auth**: Flask-Login, Flask-WTF (CSRF), JSON-file user store

---

## Project Structure

```text
fyp_main_v02/
├── __proj__.py                  # Empty sentinel — marks project root
├── AGENT.md                     # This file (project instructions)
├── GEMINI.md                    # Alias for AGENT.md
├── config/
│   └── config.toml              # Active config (paths, GCS, Gemini, labels)
├── fyp/                         # Core Python package
│   ├── __init__.py              # Bytecode compilation on import
│   ├── fyp_config.py            # Config loader; uses __proj__.py to find root
│   ├── data_io.py               # Unified I/O (local + GCS, parquet, JSON, ndjson)
│   ├── types.py                 # PyArrow dtype helpers and conversion
│   ├── polars_ops.py            # Polars helpers for expensive pandas ops at scale
│   ├── utils.py                 # Shared utility functions
│   ├── ingest.py                # Data ingestion pipeline
│   ├── donations.py             # Donation-level data handling (AIO/AWS fetch, collection metadata)
│   ├── scrape.py                # Scrape orchestration (queue, batching, threads)
│   ├── tiktok_dl.py             # yt-dlp wrapper (download, retry, error classification)
│   ├── mypyktok.py              # Legacy PykTok fork (deprecated, kept for reference)
│   ├── machine_annotation.py    # Gemini-based annotation
│   ├── annotation_schema.py     # Declarative field spec + Gemini response-schema builder + structured flattener
│   ├── recode_variables.py      # Variable recoding, feature engineering
│   ├── organize_datasets.py     # Dataset filtering & organisation
│   ├── calc_collection_stats.py   # Donation-level statistics
│   ├── activity_analysis.py     # Activity-based analysis
│   ├── embeddings.py            # Dense semantic embeddings for annotated videos (gemini-embedding)
│   ├── niche_detection.py       # Data-driven micro-genre ("niche") detection from annotation text
│   ├── video_map.py             # Cluster video embeddings into niches + 2D semantic map
│   ├── session_profile.py       # Within-session begin→end profiling
│   ├── sequence_analysis.py     # Sequence-windowing analysis (dwell→next-window lift)
│   ├── sequence_model.py        # Stage-B predictive modelling for sequence analysis
│   ├── timeline_analysis.py     # Timeline metrics (linreg, anomalies, breaks, volatility)
│   ├── pca.py                   # Distance metrics, PCA helpers
│   ├── stats.py                 # ANOVA, PERMANOVA helpers
│   └── studies.py               # Study definitions
├── web_interface/
│   ├── fyp_data_hub.py          # Flask app entry point (port 5002)
│   ├── data_service.py          # Study cache, PCA computation
│   ├── auth.py                  # Authentication, @admin_required decorator
│   ├── security.py              # Login manager, user manager
│   ├── permissions.py           # Tab + sub-page permission catalog and Flask decorator
│   ├── admin_settings.py        # Persisted admin-controlled site settings (e.g. signup gating)
│   ├── activity_log.py          # Per-user activity log for Data/User Management mutations
│   ├── process_manager.py       # Background job management (subprocess + Cloud Tasks)
│   ├── task_status.py           # GCS/local status reporters, heartbeat, cancellation
│   ├── explorer_backend.py      # Data explorer backend logic
│   ├── slack_service.py         # Slack integration
│   ├── static_content.py        # Static page content
│   ├── mail_utils.py            # Email utilities
│   ├── run_queue_annotator.py   # Gemini annotation (self-chaining Cloud Task)
│   ├── run_queue_scraper.py     # TikTok scraping worker
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
│   ├── run_benchmark_parquet_read.py  # Benchmark parquet read paths (Cloud Task)
│   ├── routes/                  # Flask Blueprints
│   │   ├── auth_routes.py       #   Login, signup, settings
│   │   ├── api_explorer_routes.py       #   Studies + Explore API + system-info
│   │   ├── api_viewer_routes.py         #   Video Analysis + media streaming API
│   │   ├── api_timelines_routes.py      #   Timelines API
│   │   ├── api_correlations_routes.py   #   Correlations API
│   │   ├── api_collections_routes.py    #   Collection stats + annotation API
│   │   ├── api_semantic_space_routes.py #   Semantic Space tab API (video embedding map)
│   │   ├── management_routes.py #   Admin/management endpoints
│   │   └── process_routes.py    #   Background process endpoints
│   ├── templates/
│   │   ├── base.html            # Base layout
│   │   ├── index.html           # Main SPA shell
│   │   ├── login.html / signup.html
│   │   └── tabs/                # Tab content templates
│   │       ├── home.html
│   │       ├── video_analysis.html
│   │       ├── explore.html
│   │       ├── my_studies.html
│   │       ├── semantic_space.html
│   │       ├── data_management.html
│   │       ├── correlations.html
│   │       ├── timelines.html
│   │       ├── collections.html
│   │       ├── admin.html
│   │       └── settings.html
│   └── static/                  # JS + CSS (no bundler)
│       ├── main.js              # Tab navigation controller
│       ├── video_analysis.js     # Video analysis tab
│       ├── explore.js           # Data explorer tab
│       ├── correlations.js      # Correlations tab
│       ├── timelines.js         # Timelines tab
│       ├── collections.js       # Collections tab
│       ├── semantic_space.js    # Semantic Space tab
│       ├── study_state.js       # Shared study-state helper
│       ├── style.css            # Main stylesheet
│       ├── js/
│       │   ├── data_management.js
│       │   └── admin_var_schema.js   # Var-schema admin editor
│       └── css/                 # (empty — styles in style.css)
├── tests/                       # Ad-hoc test/debug scripts
├── prompts/                     # Gemini prompt templates (*.txt)
├── tmp/                         # Temporary test/debug data
├── Dockerfile
└── requirements312.txt          # Pinned deps for Docker (3.12) — single source of truth
```

---

## Key Files

- **`fyp/data_io.py`**: Always use this module for file access. Abstracts local vs. GCS storage. Use named locations (`"cache"`, `"recoded"`, `"users"`) rather than raw paths.
- **`fyp/fyp_config.py`**: Config loader. Walks up the directory tree looking for `__proj__.py` to locate the project root. Call `fyp_config.initialize()` to set up.
- **`fyp/types.py`**: PyArrow-aware dtype conversion helpers. Use these for dtype handling.
- **`web_interface/`**: Contains the Flask app routes and templates.
- **`web_interface/task_status.py`**: Status reporting framework. `GCSStatusReporter` for Cloud Tasks (writes to GCS with heartbeat), `LocalStatusReporter` for subprocess mode (stdout). Use `get_reporter(name)` to get the right one.
- **`web_interface/process_manager.py`**: Process lifecycle. `CLOUD_TASK_ELIGIBLE` set controls which processes use Cloud Tasks. `start_process()` auto-selects Cloud Tasks vs subprocess based on `K_SERVICE` env var.

---

## Running the Project

### Development

```bash
source .fypenv314/bin/activate
python web_interface/fyp_data_hub.py
# → http://localhost:5002
```

### Cloud Run Deployment (Production)

The app runs on **Google Cloud Run** as two services sharing the same Docker image:

- **`fyp-data-hub`** — Web server (Flask/Gunicorn, 2 CPU, 4 GB)
- **`fyp-task-runner`** — Background task executor (8 CPU, 32 GB, timeout 3600s, concurrency 1)

**GCP Configuration:**
- Project: `<gcp-project>`, Region: `australia-southeast1`
- Cloud Tasks queue: `fyp-background-tasks` (max-attempts=1)
- Service account: `<project-number>-compute@developer.gserviceaccount.com`
- Base image: `australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-base:latest`
- App image: `australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-app:latest`

**Docker image structure (two layers):**
- **Base image** (`Dockerfile.base`): Python 3.12-slim + gcc + Rust + all pip deps. Only rebuild when `requirements312.txt` changes.
- **App image** (`Dockerfile`): Thin layer on top of base — just copies application code. Fast to build (~1 min).

**Deploy steps (both services share the same app image):**

```bash
# 0. Rebuild base image (ONLY when requirements312.txt changes — slow, ~5 min)
#    gcloud builds submit doesn't support -f, so use --config with a build spec:
gcloud builds submit \
  --config=cloudbuild-base.yaml \
  --project=<gcp-project> --region=australia-southeast1

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
python web_interface/run_queue_scraper.py     # TikTok scraping
python web_interface/run_timelines_refresh.py # Timeline updates
python web_interface/run_meta_refresh_groups.py  # Group + Video Analysis metadata refresh
```

---

## Configuration

**`config/config.toml`** — primary config. Key sections:

| Section | Key fields |
|---|---|
| `[machine]` | Gemini model, prompt file, temperature, Vertex AI project |
| `[paths]` | `local_data` (default: `/Users/<user>/fyp_local`) |
| `[data_io]` | GCS bucket, `use_gcs_*` toggles |
| `[misc]` | Timezone (`Australia/Brisbane`), `local_mode` |
| `[labels]` | Content categories, irrelevant words, generic mapper |

---

## Key Patterns & Conventions

### Project Root Discovery
`__proj__.py` is an empty sentinel file. `fyp_config.py` walks up the directory tree looking for it to locate the project root — this makes imports work regardless of working directory.

### Data I/O Abstraction
`fyp/data_io.py` abstracts local vs. GCS storage. Use named locations (`"cache"`, `"recoded"`, `"users"`) rather than raw paths. Toggle `use_gcs_*` flags in config to switch backends.

### Parquet & PyArrow
Data is stored in Parquet. Complex types (dicts, lists) are JSON-stringified before storage. Surrogate characters are escaped. Use `fyp/types.py` helpers for dtype conversion.

### Thread Safety
`StudyCache` in `data_service.py` uses double-checked locking — be careful when modifying cache logic.

### Frontend
Single-page app with tab navigation controlled by `main.js`. All data endpoints return JSON; JS handles filtering and rendering. No bundler — JS files are served as-is from `static/`.

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

No formal test framework. The closest thing to a regression suite is the cost-free
annotation safety net; everything else is ad-hoc scripts in the `tests/` folder:

```bash
# Cost-free regression + consistency suite over saved raw annotation responses
python tests/golden/run_safety_net.py

# Ad-hoc scripts (examples)
python tests/test_sequence_analysis.py
python tests/test_model_availability.py
```

Save test/debug data in the `tmp/` folder. Save test scripts in the `tests/` folder.

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
| `fyp/fyp_config.py` | Config initialisation (`fyp_config.initialize()`) |
| `fyp/ingest.py` | Data ingestion classes |
| `fyp/organize_datasets.py` | Dataset organisation & filtering |