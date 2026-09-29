# Developing The For You Data Hub

The developer guide: setting up a development checkout, coding style, tests
and the verification gate, repository conventions, the project tree, and
deployment. What the Hub is and how its parts fit together is in the
[README](README.md) and [docs/architecture.md](docs/architecture.md); each
subsystem has its own document:

| Topic | Document |
|---|---|
| Installing and running an instance | [docs/installation.md](docs/installation.md) |
| Config sections, environment variables, storage locations | [docs/configuration.md](docs/configuration.md) |
| The contract system and variable schema | [docs/contracts.md](docs/contracts.md) |
| Ingestion, scraping, annotation, consolidation, analysis | [docs/pipeline.md](docs/pipeline.md) |
| Flask app, auth, background workers, frontend | [docs/web_interface.md](docs/web_interface.md) |
| Adding a platform or a backend | [docs/extending.md](docs/extending.md) |
| `fyp/` module placement and import rules | [docs/fyp-import-graph.md](docs/fyp-import-graph.md) |
| Why things are the way they are | [docs/decisions/README.md](docs/decisions/README.md) |

---

## Development setup

- **Python 3.12**, in a `.venv` virtual environment (matches the production
  runtime). Always activate it before running anything:
  `source .venv/bin/activate`.
- Install the dev requirements and the package: `pip install -r
  requirements-dev.txt` (runtime pins + pytest/ruff/pre-commit), then
  `pip install -e .` (editable install of the `fyp` package from
  `pyproject.toml`). The editable install is recommended, never required —
  the repo also runs from a plain checkout.
- Install the pre-commit hook once: `pre-commit install`.
- Configure with `python scripts/setup.py` or by copying
  `config/config.local.toml.example` to `config/config.local.toml`. Never
  edit the committed `config/config.toml` for machine-local values.
  Prerequisites, the wizard and optional services are covered in
  [docs/installation.md](docs/installation.md); every config key and
  environment variable (secrets such as `GEMINI_API_KEY`,
  `FLASK_SECRET_KEY`, ...) is in [docs/configuration.md](docs/configuration.md).
  A `.env` file at the project root is loaded at startup.

Run the web app:

```bash
source .venv/bin/activate
python web_interface/fyp_data_hub.py
# → http://localhost:5002   (FLASK_DEBUG=1 for the auto-reloading server)
```

Background workers run as subprocesses, started from the web UI's Data
Pipeline tab or by hand (how the two execution modes work:
[docs/web_interface.md](docs/web_interface.md#background-workers)):

```bash
python -m web_interface.workers.run_queue_annotator                   # annotation
python -m web_interface.workers.run_queue_scraper --platform tiktok   # scraping (one worker per platform)
python -m web_interface.workers.run_timelines_refresh                 # timelines refresh
python -m web_interface.workers.run_meta_refresh_groups               # group + Video Analysis metadata refresh
```

---

## Tech Stack

- **Backend**: Python 3.12, Flask 3.x, Gunicorn in production (Docker,
  `python:3.12-slim`; 1 worker, 8 threads)
- **Data**: Pandas, NumPy, PyArrow, Parquet format, NDJSON
- **Analysis**: Scikit-learn, SciPy, Statsmodels
- **Storage**: Local filesystem (default `~/fyp_local`) or Google Cloud Storage
- **AI/LLM**: Google Gemini (Vertex AI or Gemini API), hosted Qwen (DashScope),
  local Qwen/MiniCPM via MLX (optional extras)
- **Scraping**: yt-dlp (primary), BeautifulSoup4, browser-cookie3
- **Frontend**: Vanilla JS, Jinja2 templates, Plotly, no build step
- **Auth**: Flask-Login, Flask-WTF (CSRF), JSON-file user store

---

## Coding Style

- Use Python **type hints** in function signatures.
- Docstrings follow the **Google style guide**.
- Module imports at the **top of the file**, except where the import-cycle
  rule (CONTRIBUTING.md, invariant 1) or a heavy optional dependency calls for
  a function-level import.
- Import `fyp` modules by their canonical subpackage path
  (`fyp.scrape.platform_scraper`), never the flat shims (ruff `TID251`).
- Use **f-strings** for string formatting.
- Layout is whatever `ruff format` produces (pyproject settings; enforced by
  pre-commit, `scripts/verify.sh` and CI). Don't hand-format.
- Comments should **explain the code**. Do not write your own reasoning in the
  code.
- The project is pandas-first; always use **PyArrow dtypes** for DataFrames.

### Frontend Styling Rules

All visual styling is managed through a **CSS custom property (token) system**
in `style.css`. Never hardcode colors, fonts, sizes, or weights in templates,
JavaScript, or inline styles.

- **Colors**: Use semantic tokens (e.g., `var(--color-text-primary)`,
  `var(--btn-danger-bg)`), never hex codes or `rgb()` values. The token
  hierarchy is: Primitives → Semantic → Component.
- **Fonts**: The primary font is **Inter** (`var(--font-sans)`). Monospace is
  `var(--font-mono)`. Never set `font-family` inline or in JS.
- **Font sizes**: Use the 7-step type scale tokens: `var(--text-hero)`,
  `var(--text-h2)`, `var(--text-h3)`, `var(--text-body)`, `var(--text-sm)`,
  `var(--text-xs)`, `var(--text-xxs)`. Or use the equivalent utility classes:
  `.text-hero`, `.text-h2`, `.text-h3`, `.text-body`, `.text-sm`, `.text-xs`,
  `.text-xxs`.
- **Font weights**: Use tokens `var(--weight-normal)` / `var(--weight-medium)`
  / `var(--weight-semibold)` / `var(--weight-bold)`, or utility classes
  `.font-normal`, `.font-medium`, `.font-semibold`, `.font-bold`.
- **Line height**: Use `var(--leading-tight)`, `var(--leading-normal)`,
  `var(--leading-relaxed)`.
- **In templates**: Prefer utility classes (`class="text-sm font-bold"`) over
  inline `style=""` for font properties.
- **In JavaScript**: Use `element.classList.add('text-sm', 'font-bold')`
  instead of `element.style.fontSize = '...'`. For Plotly charts, use
  `family: getCSSVar('--font-sans')`.
- **Tooltips**: Use the `.meta-tooltip` class with `data-tooltip="..."`
  attribute, not the native `title` attribute.
- **Buttons**: Use existing button classes (`.btn-primary`, `.btn-danger`,
  `.btn-save`, `.btn-stop`, `.btn-discreet`, `.action-btn`). Never set button
  colors inline.
- **Dark/light themes**: Both are defined in `style.css` (`:root` for dark,
  `[data-theme="light"]` for light). All tokens must have values in both
  themes.

---

## Tests

pytest is configured in `pyproject.toml` (`testpaths = tests/unit`). The
standard gate for every change is:

```bash
source .venv/bin/activate
bash scripts/verify.sh
# = ruff check + ruff format --check (pyproject rule set)
#   + pytest -m "not requires_data and not requires_gcs and not slow and not stale"
#     (includes the import-cycle/schema-hash guard, the routes.md freshness
#     check and the version-consistency check)
#   + the golden safety net + an app import smoke
```

It is cost-free — no Gemini calls, no GCS writes, no production data — and
passes on a fresh checkout. (On Windows, where `bash` may be unavailable, run
the pytest line directly.)

**Markers** (`pyproject.toml`), all excluded from the gate:

- `requires_data` — needs local/production data files (parquets, media) not
  present in a fresh checkout
- `requires_gcs` — needs live Google Cloud Storage / GCP credentials
- `slow` — takes noticeably long to run
- `stale` — the designated bucket for tests known-broken against current
  contracts/data shapes (not a regression signal)

**The golden safety net.** `tests/golden/` is the cost-free annotation
regression suite: it replays saved raw Gemini responses through the full
parse/flatten/repair pipeline, offline. Run it after touching any annotation
code (see `tests/golden/README.md`). Plain `pytest` does not collect it; only
`run_safety_net.py` (and so `verify.sh` and CI) runs it:

```bash
python tests/golden/run_safety_net.py
```

**Guard tests to know:** `tests/unit/test_import_cycle_hash.py` (schema-hash
import-order independence), `test_lazy_config_boot.py` ([BOOT] exactly once,
lazy config), `test_subpackage_shims.py` (old-path aliases stay identical),
`test_pool_import_race.py` (no cold shim imports in thread-pool bodies),
`test_url_map_snapshot.py` (HTTP endpoints frozen), `test_routes_doc.py`
(`docs/routes.md` matches the URL map — regenerate with
`python scripts/gen_route_inventory.py`),
`test_task_status_stdout_contract.py` (`::PROGRESS::`/`::DATA::` wire format),
`test_worker_registry.py` (worker tables derived from `WORKERS`).

**Where things go.** New tests go in `tests/unit/`. Three self-runner
integration scripts (`test_annotation_contract_api.py`,
`test_annotation_contract_editor.py`, `test_var_schema_api.py`) are listed in
`tests/unit/conftest.py::collect_ignore`: they have their own `main()`
harness and snapshot/restore the live var-schema and presentation stores, so
they run directly (`python tests/unit/<file>.py`), never in the shared gate;
converting one to pytest style and deleting its entry is a welcome
contribution. Save test/debug data in `tmp/`; throwaway debug scripts go in
`tests/debug/` and one-off maintenance scripts in `scripts/adhoc/`. Both are
gitignored — they are working scratch, and in practice they collect
production ids, bucket names and donation filenames that must not be
published.

---

## Key Patterns & Conventions

The load-bearing invariants (config import cycle, worker stdout contract,
var-schema hash, contract ownership, cross-service stats, no flat shims) are
listed with their guards in
[CONTRIBUTING.md](CONTRIBUTING.md#invariants-you-must-not-break).

### Project Root Discovery

`__proj__.py` is an empty sentinel file. `fyp/core/paths.py` walks up from the
working directory to find it (unless `FYP_CONFIG_PATH` names a config TOML
directly), so imports work from any working directory
([docs/configuration.md](docs/configuration.md)).

### Import-Cycle Rule

`fyp/core/fyp_config.py` boots lazily, on first access to `fyp_cf` or
`get_config()`, and the boot re-enters the modules it calls (`data_io`, the
three `*_versioning` modules, `var_presentation`). Those modules never import
`fyp_cf` or `fyp.core.data_io` at module level; they use
`fyp.core.runtime.cf` (imported as `_cf`) and local `_data_io()` helpers.
`import fyp.ingest` boots config by design. The full rule is CONTRIBUTING.md
invariant 1; guards: `tests/unit/test_import_cycle_hash.py` and
`tests/unit/test_lazy_config_boot.py`. Where a new module may live follows
from it: [docs/fyp-import-graph.md](docs/fyp-import-graph.md).

### Data I/O Abstraction

All file access goes through `fyp/core/data_io.py` named locations
(`"cache"`, `"recoded"`, `"users"`, ...), never raw paths, so code works
unchanged against local disk and GCS. Runtime-registered locations and
local copies of stored objects:
[docs/configuration.md](docs/configuration.md#storage-locations).

### Parquet & PyArrow

Data is stored in Parquet. Complex types (dicts, lists) are JSON-stringified
before storage. Surrogate characters are escaped. Use `fyp/core/types.py`
helpers for dtype conversion.

### Thread Safety

`StudyCache` in `web_interface/services/study_data.py` uses double-checked
locking — be careful when modifying cache logic.

### Annotation-Version Vocabulary

Use two words, in code and UI, never interchangeably: **active** = the
version the next annotation is stamped with (derived, not stored);
**preferred** = the version studies read (stored, changed only by a promote).
"Current" and "live" are retired for this concept. Details:
[docs/contracts.md](docs/contracts.md#the-runtime-annotation-contract).

### Web Layer, Workers and Frontend

Route guards and roles, the background-job framework (Cloud Tasks vs local
subprocesses, `worker_registry.WORKERS`, self-chaining, retries, run logs),
and the frontend (tab SPA, per-user variable preferences, content-hashed
asset URLs) are documented in [docs/web_interface.md](docs/web_interface.md).
A new worker is one `run_<name>.py` module plus one `WORKERS` entry; a new
Dataset Assembly card stays inert until `main.js` calls `setStatus()` for it.

### Pipeline Rules

How each stage behaves — ingestion classes and the structure sentinel,
scrapers and their batch guards, annotation and embedding backends,
consolidation, the enrichment loop, the sessions build — is documented in
[docs/pipeline.md](docs/pipeline.md). Adding a platform or a backend:
[docs/extending.md](docs/extending.md).

---

## Project Structure

```text
foryou-research/
├── __proj__.py                  # Empty sentinel — marks the project root
├── DEVELOPING.md                # This file (developer guide)
├── config/
│   ├── config.toml              # Committed config (docs/configuration.md); overlay: config.local.toml
│   ├── legacy_annotation_prompt.txt # Pre-versioning "v0_legacy" prompt, display-only (Admin → Versions)
│   ├── annotation_contract.toml # Annotation fields → generated prompt, response schema, flattener
│   ├── scrape_contract.toml     # Canonical cross-platform scrape schema (base + per-platform fields)
│   ├── activity_contract.toml   # Platform-agnostic activity schema (required/hard-drop fields, derived local_*/session fields)
│   └── derived_contract.toml    # Metadata for merge-derived columns (status flags, niche measures, ...)
├── fyp/                         # Core package, five subpackages (docs/fyp-import-graph.md)
│   ├── __init__.py              # Import-free: docstring + __version__ only (never import submodules here)
│   ├── core/
│   │   ├── fyp_config.py        # Config loader; lazy get_config() + PEP 562 `fyp_cf`
│   │   ├── paths.py             # Project-root discovery (__proj__.py / FYP_CONFIG_PATH); PROJECT_ROOT, PYTHON_EXEC
│   │   ├── runtime.py           # Import-light accessors: cf(), label(), is_cloud_run(), graceful_stop_requested()
│   │   ├── gemini_client.py     # The one way to build a Google GenAI client (Vertex AI vs API key)
│   │   ├── data_io.py           # Unified I/O over named locations (local + GCS; parquet, JSON, ndjson)
│   │   ├── types.py             # PyArrow dtype helpers and conversion
│   │   ├── polars_ops.py        # Polars helpers for expensive pandas ops at scale
│   │   ├── memory.py            # RSS/peak probes + mem_probe() ([<TAG>][MEM] log lines)
│   │   ├── utils.py             # Shared utilities: connectivity probe, fuzzy matching, zip/mojibake helpers
│   │   ├── activity_vocabulary.py # Activity types, engagement labels and tokens
│   │   ├── progress_monitor.py  # Live progress bar for a batch of concurrent futures
│   │   ├── var_schema_vocab.py  # var_schema roles, scales and legacy role names
│   │   ├── artifacts.py         # Shared artifact names and readers (enrichment_status)
│   │   ├── media_paths.py       # Platform-aware media paths + resolve_media() legacy fallback
│   │   ├── logging_setup.py     # get_logger(): stdout logging, bare %(message)s, level from FYP_LOG_LEVEL
│   │   ├── registry_metadata.py # Per-version field_metadata snapshots + union helpers for the registries
│   │   ├── activity_contract.py # Loads/validates activity_contract.toml
│   │   ├── activity_versioning.py # Activity-contract version registry (acv_)
│   │   └── derived_contract.py  # Loads/validates derived_contract.toml
│   ├── ingest/                  # Ingestion; __init__ imports all platform modules eagerly (config boots)
│   │   ├── base.py              # ForYouBaseCollection ABC, load loop, per-file intake stats
│   │   ├── transforms.py        # Row transforms: time zones, local-time features, session ids, play durations
│   │   ├── ingestion_ledger.py  # The per-file ingestion ledger (skip outcomes + the collection's ledger methods)
│   │   ├── structure_sentinel.py  # DDP structure-drift detection, quarantine and review flow
│   │   ├── donations.py         # AIO/AWS donation fetch + participant metadata
│   │   ├── raw_names.py         # Generated identities for raw uploads (stored names, collection ids, display ids)
│   │   ├── migrations/          # One-off rewrites of stored activity data (testable half of scripts/migrate_*.py)
│   │   ├── tiktok.py            # TikTokDDPCollection / TikTokAIOCollection / TikTokZeeschuimerCollection
│   │   ├── instagram.py         # InstagramDDPCollection
│   │   └── youtube.py           # YouTubeDDPCollection
│   ├── scrape/                  # Scraping; __init__ re-exports the old fyp.scrape API
│   │   ├── scrape.py            # Platform-agnostic orchestration: queues, batching, guards
│   │   ├── consolidate.py       # Fold scrape batches into the recoded scrapes frame
│   │   ├── failures.py          # The failed-scrapes ledger
│   │   ├── slideshow.py         # Photo-post slideshows (images + audio → MP4)
│   │   ├── scrape_queues.py     # Per-platform queue files (to_scrape_<platform>.json) + retry-budget sidecars
│   │   ├── platform_scraper.py  # BaseScraper ABC + registry + get_scraper(); ThrottleController; shared derivations
│   │   ├── scrape_contract.py   # Loads/validates scrape_contract.toml; canonical field set + dtypes
│   │   ├── scrape_versioning.py # Scrape-contract version registry (sv_)
│   │   ├── tiktok_dl.py         # TikTokScraper (yt-dlp)
│   │   ├── instagram_dl.py      # InstagramScraper (yt-dlp; anonymous first, cookies for gated posts)
│   │   ├── youtube_dl.py        # YouTubeScraper (yt-dlp; DASH merge, bot_check, n-challenge solver)
│   │   ├── connectivity.py      # Online probe + ConnectivityGate (outages are waited out)
│   │   ├── scraper_alerts.py    # Persistent per-platform scraper alerts (cache/scraper_alerts.json)
│   │   └── scraper_cookies.py   # Per-platform cookie plumbing + cookie_health
│   ├── annotation/
│   │   ├── backends/                 # AnnotationBackend ABC + registry; gemini / qwen_api / qwen_local / minicpm_local, variants, settings
│   │   ├── machine_annotation.py     # Annotation entry points (queue batches, backend dispatch)
│   │   ├── gemini_calls.py           # Gemini client, generation config, retries, threaded batch calls
│   │   ├── response_parsing.py       # Raw responses → flat annotation rows (JSON repair, flattening)
│   │   ├── annotation_refinement.py  # Refine raw batches; consolidate into the versioned dataset
│   │   ├── machine_annotation_batch.py # Batch-mode annotation (Gemini Batch API only)
│   │   ├── annotation_contract.py    # Loads/validates annotation_contract.toml; builds FIELD_SPECS
│   │   ├── annotation_schema.py      # Generates prompt, response schema and flattener from the contract
│   │   ├── annotation_versioning.py  # Annotation version registry (av_) + field_metadata snapshots
│   │   ├── recode_variables.py       # Variable recoding, feature engineering, schema hash
│   │   ├── var_presentation.py       # Admin-editable presentation store (the four web_*_prio flags)
│   │   ├── irrelevant_words.py       # Admin-editable hashtag stoplist + the matcher recode_tokenise uses
│   │   ├── ab_eval.py                # Prompt/model A/B testing harness (arms, agreement metrics, reports)
│   │   └── human_eval.py             # Human annotation input (coding tasks, ICR metrics, votes, invitations)
│   └── analysis/
│       ├── organize_datasets.py # Dataset build entry points (create_study_recoded_dataset ...)
│       ├── datasets/            # Dataset build steps: loading, sampling, merge, refresh sidecars, enrichment status
│       ├── calc_collection_stats.py  # Donation-level statistics
│       ├── activity_analysis.py # Activity-based analysis
│       ├── experimental/        # Research analyses the app does not use: niche_detection, session_profile, sequence_model
│       ├── embeddings.py        # Dense embeddings for annotated videos (model-scoped shard store)
│       ├── embedding_store.py   # Random-access dense sidecar over the shards (float16 parts, id index, corpus mean)
│       ├── embedding_backends/  # EmbeddingBackend ABC + registry: gemini / qwen_api / qwen_local
│       ├── video_map.py         # Niche clustering + 2D semantic map + per-video typicality/isolation percentiles
│       ├── sessions/            # Sessions tab build: inputs, segmentation, refresh plan, publishing
│       ├── session_explorer.py  # Re-export surface of sessions/
│       ├── entropy_metrics.py   # Entropy/dispersion measures on dense embeddings
│       ├── sequence_analysis.py # Sequence-windowing analysis (dwell→next-window lift)
│       ├── timeline_analysis.py # Timeline metrics (linreg, anomalies, breaks, volatility)
│       ├── pca.py               # Distance metrics, PCA helpers
│       ├── stats.py             # ANOVA, PERMANOVA helpers
│       └── studies.py           # Study definitions
├── web_interface/
│   ├── fyp_data_hub.py          # Flask app factory + entry point (port 5002)
│   ├── static_assets.py         # asset_url(): content-hashed URLs for static JS/CSS
│   ├── seo.py                   # Canonical host/link, robots.txt, sitemap.xml, JSON-LD
│   ├── auth/                    # Accounts and access control
│   │   ├── accounts.py          #   User + role store (UserManager, RoleManager, the user_manager singleton)
│   │   ├── security.py          #   Flask-Login wiring (login_manager, user loader)
│   │   ├── permissions.py       #   Permission catalog + route guards (@permission_required, @admin_required)
│   │   └── email_verification.py  # Signup email verification: signed, time-limited links + policy
│   ├── tasks/                   # Background-task runtime (docs/web_interface.md "Background workers")
│   │   ├── worker_registry.py   #   WORKERS: the one table of background workers
│   │   ├── process_manager.py   #   Launch a worker: local subprocess or Cloud Tasks dispatch
│   │   ├── runtime.py           #   Cloud Tasks runtime: run a delivery, record stats, advance chains + pipeline
│   │   ├── task_status.py       #   GCS/local status reporters, heartbeat, cancellation
│   │   ├── run_logs.py          #   Durable per-process run logs (last 10 runs; GET /api/logs/<name>)
│   │   ├── task_failures.py     #   Durable failure ledger (task_failures.json in "cache")
│   │   ├── worker_runner.py     #   Shared __main__ CLI entrypoint used by most workers
│   │   └── drain_lease.py       #   Cross-instance heartbeat lease for local scrape-queue drains
│   ├── workers/                 # One module per background worker; run locally as python -m web_interface.workers.run_<name>
│   │   ├── run_queue_annotator.py   # Annotation worker (self-chaining Cloud Task)
│   │   ├── run_queue_scraper.py     # Per-platform scraping worker (queue_scraper_<platform>)
│   │   ├── run_timelines_refresh.py # Timelines refresh worker
│   │   ├── run_meta_refresh_groups.py  # Group + Video Analysis metadata refresh
│   │   ├── run_pca_refresh.py       # PCA/correlations refresh
│   │   ├── run_recode_refresh_studies.py  # Study recoding
│   │   ├── run_consolidate_enrichment.py  # Consolidation + impact analysis
│   │   ├── run_study_refresh.py     # Single-study stats/PCA/metadata refresh
│   │   ├── run_ingest_refresh.py    # Ingest refresh: per-file row counts + provenance snapshot
│   │   ├── run_collection_metadata_refresh.py  # Regenerate collections_metadata.parquet
│   │   ├── run_collection_delete.py # Delete a collection from recoded/metadata parquets
│   │   ├── run_aio_fetch.py         # Fetch recent AIO donations + participant metadata from AWS
│   │   ├── run_embeddings_refresh.py   # Embed not-yet-embedded annotated videos (single-flight)
│   │   ├── run_video_map_refresh.py    # Cluster the embedding store into niches + 2D map
│   │   ├── run_sequence_refresh.py  # Refresh sequence-analysis artifacts
│   │   ├── run_sessions_refresh.py  # Sessions tab build (self-chaining, incremental; docs/pipeline.md)
│   │   ├── run_benchmark_parquet_read.py  # Benchmark parquet read paths
│   │   ├── run_queue_annotator_batch.py   # Batch-mode Gemini annotation
│   │   ├── run_ab_eval.py           # Prompt A/B eval run
│   │   ├── run_retokenise_hashtags.py     # Retroactive hashtag-stoplist cleanup
│   │   ├── run_enrichment_supervisor.py  # One tick of the automatic per-collection enrichment loop
│   │   └── run_ops_report.py        # Daily ops report: assemble + email (not queue-retry-safe)
│   ├── integrations/            # Outbound email (mail_utils) and Slack (slack_service)
│   ├── services/                # Business logic shared by routes and workers — data, stats, settings, accounts links (docs/web_interface.md)
│   ├── routes/                  # Flask Blueprints (docs/web_interface.md; endpoints: docs/routes.md)
│   │   ├── auth_routes/         #   Login + signup, admin users / roles / site settings, a user's own settings
│   │   ├── api_explorer_routes/         #   Studies + Explore API + methods note; admin system info / health / ops report
│   │   ├── api_viewer_routes.py         #   Video Analysis + media streaming API
│   │   ├── api_timelines_routes.py      #   Timelines API
│   │   ├── api_correlations_routes.py   #   Correlations API
│   │   ├── api_semantic_space_routes.py #   Semantic Space tab API
│   │   ├── api_sessions_routes.py       #   Sessions tab API
│   │   ├── my_collections_routes.py     #   Participant self-service API (/api/my/collections/*)
│   │   ├── _access.py           #   Shared route-level access helpers
│   │   ├── management/          #   Admin/management endpoints: per-domain submodules on one blueprint
│   │   ├── human_eval_routes.py #   Human annotation input (coding, votes, invitations)
│   │   ├── public_routes.py     #   Public (unauthenticated) mini-site, robots.txt, sitemap.xml
│   │   └── process_routes.py    #   Background process endpoints + the internal Cloud Tasks receiver
│   ├── templates/
│   │   ├── base.html            # Base layout
│   │   ├── index.html           # Main SPA shell
│   │   ├── login.html / signup.html  # Form-only pages on the public layout
│   │   ├── public/              # Public mini-site: base_public.html, partials, one template per page
│   │   ├── partials/            # Shared partials (consent statement, per-platform how-to)
│   │   ├── _macros.html         # Shared Jinja macros (card_info)
│   │   └── tabs/                # Tab content templates (+ admin/ and dm/ partial subdirectories)
│   └── static/                  # JS + CSS, no bundler
│       ├── main.js              # Tab navigation, CSRF fetch wrapper, theme, app dialogs
│       ├── video_analysis.js    # Video Analysis tab
│       ├── explore.js           # Data explorer tab
│       ├── correlations.js      # Correlations tab
│       ├── timelines.js         # Timelines tab
│       ├── semantic_space.js    # Semantic Space tab
│       ├── sessions.js          # Sessions tab (session explorer + episode inspector)
│       ├── study_state.js       # Shared study-state helper
│       ├── filter_group_ui.js / filter_value_search.js  # Shared Explore + Video Analysis filter widgets
│       ├── style.css            # Main stylesheet (the token system)
│       └── js/
│           ├── core/dom_utils.js     # DOM helpers shared by every app script (loaded first)
│           ├── data_management/      # Data Pipeline tab, one file per feature (load order: templates/index.html)
│           ├── worker_control.js     # Worker cards, the status poll, the log modal
│           ├── help.js               # Per-tab help text + the help modal
│           ├── variable_prefs.js     # Per-user "Customize variables" panels
│           ├── admin_var_schema.js   # Variable Visibility viewer (only the prio checkboxes save)
│           ├── admin_tab.js / my_stuff_tab.js  # Former inline template scripts
│           └── admin_ab_eval.js / admin_contract_editor.js / admin_annotation_versions.js / admin_human_eval.js / human_coding.js
├── tests/                       # unit/ + golden/ + conftest.py, _storage_guard.py, _web.py (Flask test-client helpers)
├── tmp/                         # Temporary test/debug data
├── scripts/                     # verify.sh gate, setup wizard, doc generators, make_video_grid_hero.py, migrations; adhoc/ (gitignored)
├── docs/                        # Documentation (index: README.md); decisions/ is the decision log
├── Dockerfile / Dockerfile.base # App image / dependency base image
├── cloudbuild-app.yaml / cloudbuild-base.yaml  # Cloud Build configs (image paths via --substitutions)
├── pyproject.toml               # Packaging ([project] fyp-pipeline) + ruff + pytest config
└── requirements.txt             # Pinned deps for Docker (3.12) — production lock
```

---

## Deployment

### Cloud Run Deployment

The app runs on **Google Cloud Run** as two services sharing the same Docker
image:

- **`fyp-data-hub`** — web server (Flask/Gunicorn, 2 CPU, 4 GB)
- **`fyp-task-runner`** — background task executor (8 CPU, 32 GB, timeout
  3600 s, concurrency 1)

**GCP configuration:**

- Project: `<gcp-project>`, Region: `australia-southeast1`
- Cloud Tasks queue: `fyp-background-tasks` (max-attempts=4 with backoff;
  configure via `scripts/configure_task_queue.sh`). Retry is app-controlled
  and failures land in the task-failures ledger — see
  [docs/web_interface.md](docs/web_interface.md#background-workers).
- Service account: the project's default compute service account
  (`<project-number>-compute@developer.gserviceaccount.com`)
- Base image: `australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-base:latest`
- App image: `australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-app:latest`

**Docker image structure (two layers):**

- **Base image** (`Dockerfile.base`): Python 3.12-slim + gcc + Rust + all pip
  deps. Only rebuild when `requirements.txt` changes.
- **App image** (`Dockerfile`): thin layer on top of the base — just copies
  application code. Fast to build (~1 min).

**Build context.** `.gcloudignore` is the only filter that reaches Cloud
Build — it excludes `.dockerignore` itself, so the docker step runs with no
ignore file and `.dockerignore` matters only to a local `docker build`. Keep
the two in step. A git worktree's `.git` is a *file* (a `gitdir:` pointer)
that the `.git/` pattern does not match, so both files list bare `.git` and
`.pytest_cache/` as well (see
[decision 0019](docs/decisions/0019-cloud-build-context.md)).

**Deploy steps** (both services share the same app image). Every build runs
in Cloud Build — no local Docker install is required.

```bash
# 0. Rebuild base image (ONLY when requirements.txt changes — slow, ~11 min
#    in Cloud Build with no layer cache from a prior local build)
gcloud builds submit --config=cloudbuild-base.yaml \
  --substitutions=_BASE_IMAGE=australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-base:latest \
  --project=<gcp-project> --region=australia-southeast1

# 1. Build the app image (always required before deploying — fast, ~2 min)
gcloud builds submit --config=cloudbuild-app.yaml \
  --substitutions=_BASE_IMAGE=australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-base:latest,_APP_IMAGE=australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-app:latest \
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

`cloudbuild-base.yaml` / `cloudbuild-app.yaml` (repo root) are generic Cloud
Build configs — a `--tag` one-liner can't pass `-f Dockerfile.base` or
`--build-arg`, so each needs a config file to select the Dockerfile and wire
the base image in. Neither config hardcodes a registry path; both image paths
are supplied per invocation via `--substitutions`, so no project-specific
identifier lives in the public tree (see
[decision 0011](docs/decisions/0011-gcp-identifiers-supplied-at-build-time.md)).
A local `docker build` (see the comments atop `Dockerfile` /
`Dockerfile.base`) still works as a fallback if you have Docker installed,
but nothing here needs it.

**When to deploy which service:**

- UI/route/template/JS changes only → deploy just `fyp-data-hub`
- Task worker logic only (`workers/run_*.py`) → deploy just `fyp-task-runner`
- Shared code (`fyp/`, `tasks/`, `services/`) → deploy
  **both**
- Step 1 (build) is always required before any deploy
- Step 0 (base image) is only needed when Python dependencies change

Production deploys straight from `main`, so every merged change must be
independently deployable.

### Local Scrape-Queue Drain Against Prod GCS (Residential IP)

Instagram and YouTube wall off Cloud Run's datacenter IPs, so their scrape
queues are drained from a laptop on a residential IP (why:
[docs/pipeline.md](docs/pipeline.md#where-each-scraper-runs)). Setting
`FYP_FORCE_GCS=1` makes a local process resolve **all** storage
(`data`/`cache`/`media`) against the prod GCS bucket — the same code path
Cloud Run uses — while keeping local behavior for everything gated on
`K_SERVICE` (Chrome-profile cookies, stdout status reporting, no Cloud Tasks
dispatch, no `task_status/` or `process_stats.json` writes).

**Prerequisites (once):**

1. Run from the **deployed commit** — the scrape contract and var-schema
   synthesis are code-baked; a drifted branch would stamp mismatched
   versions/columns.
2. `gcloud auth application-default login` with write access to the prod
   bucket (plain ADC — no service-account key needed).
3. `ffmpeg` (DASH merge) and `node` or `deno` (n-challenge solver) on PATH.
4. Chrome logged into the research account for the platform; approve the
   macOS Keychain prompt on first cookie extraction (run interactively).
5. In the web UI: make sure `queue_scraper_<platform>` is **not** running
   before starting the drain. While the drain runs it holds a **drain lease**
   (`local_drain_<platform>.json` in `cache`, heartbeat every 30 s, stale
   after 10 min — see `web_interface/tasks/drain_lease.py`): the web UI refuses to
   start that platform's scraper or a Consolidate while the lease is fresh,
   and the armed auto-consolidate defers. Queue writes themselves are atomic
   (`data_io.update_json` compare-and-swap), so a concurrent append is never
   lost; a concurrent worker would only mean duplicate work.

**Run** (YouTube shown):

```bash
export FYP_FORCE_GCS=1
export FYP_GCS_BUCKET_NAME=<prod-bucket>
caffeinate -i python -m web_interface.workers.run_queue_scraper --platform youtube --batch-size 200
```

The boot log must show `FYP_FORCE_GCS set. Forcing all storage to GCS.` (the
`__main__` default batch size is 5 — pass a real value; `caffeinate -i`
prevents sleep mid-drain). Interrupting mid-batch is safe: unpruned items are
re-scraped and already-uploaded media is skipped by `check_existing_media`.

**Verify / finish:** check `gs://<bucket>/media/youtube/` for new mp4s and
`gs://<bucket>/data/scrape/` for new `scrapes_*.parquet`; the queue JSON at
`gs://<bucket>/data/cache/to_scrape_youtube.json` shrinks per batch. Close the
shell (drops `FYP_FORCE_GCS`), then run **Consolidate & Refresh** from the web
UI — it folds in the locally-written parquets automatically.
