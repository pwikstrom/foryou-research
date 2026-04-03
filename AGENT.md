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
- **Scraping**: PykTok (custom fork `mypyktok.py`), BeautifulSoup4, browser-cookie3
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
│   ├── config.toml              # Active config (paths, GCS, Gemini, labels)
│   └── config.toml.template
├── fyp/                         # Core Python package
│   ├── __init__.py              # Bytecode compilation on import
│   ├── fyp_config.py            # Config loader; uses __proj__.py to find root
│   ├── data_io.py               # Unified I/O (local + GCS, parquet, JSON, ndjson)
│   ├── types.py                 # PyArrow dtype helpers and conversion
│   ├── utils.py                 # Shared utility functions
│   ├── ingest.py                # Data ingestion pipeline
│   ├──  # Donation-level data handling
│   ├── scrape.py                # Metadata enrichment & scraping
│   ├── mypyktok.py              # Custom PykTok fork for TikTok scraping
│   ├── machine_annotation.py    # Gemini-based annotation
│   ├── recode_variables.py      # Variable recoding, feature engineering
│   ├── organize_datasets.py     # Dataset filtering & organisation
│   ├── calc_collection_stats.py   # Donation-level statistics
│   ├── activity_analysis.py     # Activity-based analysis
│   ├── pca.py                   # Distance metrics, PCA helpers
│   ├── stats.py                 # ANOVA, PERMANOVA helpers
│   └── studies.py               # Study definitions
├── web_interface/
│   ├── fyp_data_hub.py          # Flask app entry point (port 5002)
│   ├── data_service.py          # Study cache, PCA computation
│   ├── auth.py                  # Authentication, @admin_required decorator
│   ├── security.py              # Login manager, user manager
│   ├── process_manager.py       # Background job management
│   ├── process_stats.json       # Background job state
│   ├── explorer_backend.py      # Data explorer backend logic
│   ├── slack_service.py         # Slack integration
│   ├── static_content.py        # Static page content
│   ├── mail_utils.py            # Email utilities
│   ├── run_queue_annotator.py   # Gemini annotation worker
│   ├── run_queue_scraper.py     # TikTok scraping worker
│   ├── run_timelines_refresh.py # Timeline updates worker
│   ├── run_meta_refresh_groups.py  # Group metadata refresh
│   ├── run_meta_refresh_viewer.py  # Viewer metadata refresh
│   ├── routes/                  # Flask Blueprints
│   │   ├── auth_routes.py       #   Login, signup, settings
│   │   ├── data_routes.py       #   Data API endpoints (largest)
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
│       ├── style.css            # Main stylesheet
│       ├── js/
│       │   └── data_management.js
│       └── css/                 # (empty — styles in style.css)
├── tests/                       # Ad-hoc test/debug scripts
├── prompts/                     # Gemini prompt templates (*.txt)
├── tmp/                         # Temporary test/debug data
├── Dockerfile
├── requirements.txt
└── requirements312.txt          # Pinned deps for Docker (3.12)
```

---

## Key Files

- **`fyp/data_io.py`**: Always use this module for file access. Abstracts local vs. GCS storage. Use named locations (`"cache"`, `"recoded"`, `"users"`) rather than raw paths.
- **`fyp/fyp_config.py`**: Config loader. Walks up the directory tree looking for `__proj__.py` to locate the project root. Call `fyp_config.initialize()` to set up.
- **`fyp/types.py`**: PyArrow-aware dtype conversion helpers. Use these for dtype handling.
- **`web_interface/`**: Contains the Flask app routes and templates.

---

## Running the Project

### Development

```bash
source .fypenv314/bin/activate
python web_interface/fyp_data_hub.py
# → http://localhost:5002
```

### Docker (Production)

```bash
docker build -t fyp-hub:latest .
docker run -p 5000:$PORT \
  -e FLASK_SECRET_KEY="..." \
  -e GEMINI_API_KEY="..." \
  -e FYP_GCS_BUCKET_NAME="..." \
  -v /path/to/config:/app/config \
  fyp-hub:latest
```

### Background Workers

```bash
python web_interface/run_queue_annotator.py   # Gemini annotation
python web_interface/run_queue_scraper.py     # TikTok scraping
python web_interface/run_timelines_refresh.py # Timeline updates
python web_interface/run_meta_refresh_groups.py  # Group metadata refresh
python web_interface/run_meta_refresh_viewer.py  # Viewer metadata refresh
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

### Background Jobs
Job queues are tracked via JSON-persisted `process_stats`. Workers poll the queue; annotation/scraping progress is visible in the web UI's process tab.

---

## Tests

No formal test framework. Tests are ad-hoc scripts in the `tests/` folder:

```bash
cd tests
python test_env.py
python test_metadata_filtering.py
python test_calc.py
```

Save test/debug data in the `tmp/` folder. Save test scripts in the `tests/` folder.

---

## Main Entry Points

| File | Purpose |
|---|---|
| `web_interface/fyp_data_hub.py` | Web app (Flask, port 5002) |
| `web_interface/run_queue_annotator.py` | Gemini annotation worker |
| `web_interface/run_queue_scraper.py` | Scraping worker |
| `web_interface/run_timelines_refresh.py` | Timeline refresh worker |
| `web_interface/run_meta_refresh_groups.py` | Group metadata refresh |
| `web_interface/run_meta_refresh_viewer.py` | Viewer metadata refresh |
| `fyp/fyp_config.py` | Config initialisation (`fyp_config.initialize()`) |
| `fyp/ingest.py` | Data ingestion classes |
| `fyp/organize_datasets.py` | Dataset organisation & filtering |