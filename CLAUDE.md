# CLAUDE.md — FYP Research Platform

## Project Overview

**FYP (For You Project)** is a TikTok research data platform for academics. It ingests TikTok data donations and baseline video captures, enriches them via web scraping and LLM annotation (Google Gemini), performs statistical analysis (PCA, ANOVA, PERMANOVA), and presents findings through an interactive Flask-based web dashboard with role-based access control.

---

## Tech Stack

- **Backend**: Python 3.12, Flask 3.1.2, Gunicorn (production)
- **Data**: Pandas, NumPy, PyArrow, Parquet format, NDJSON
- **Analysis**: Scikit-learn, SciPy, Statsmodels, Seaborn
- **Storage**: Local filesystem (`/Users/<user>/fyp_local`) or Google Cloud Storage
- **AI/LLM**: Google Gemini (Vertex AI), OpenAI (secondary)
- **Scraping**: PykTok, BeautifulSoup4, browser-cookie3
- **Frontend**: Vanilla JS + jQuery patterns, Jinja2 templates, no build step
- **Auth**: Flask-Login, Flask-WTF (CSRF), JSON-file user store
- **Deployment**: Docker (Python 3.12-slim), Gunicorn

---

## Project Structure

```
fyp_main_v02/
├── __proj__.py              # Empty sentinel file — marks project root
├── config/
│   ├── config.toml          # Active config (paths, GCS, Gemini, labels)
│   └── config.toml.template
├── fyp/                     # Core Python package
│   ├── fyp_config.py        # Config loader; uses __proj__.py to find root
│   ├── data_io.py           # Unified I/O (local + GCS, parquet, JSON, ndjson)
│   ├── ingest.py            # Data ingestion pipeline
│   ├── scrape.py            # Metadata enrichment & scraping
│   ├── machine_annotation.py# Gemini-based annotation
│   ├── recode_variables.py  # Variable recoding, feature engineering
│   ├── organize_datasets.py # Dataset filtering & organisation
│   ├── pca.py               # Distance metrics, PCA helpers
│   └── stats.py             # ANOVA, PERMANOVA helpers
├── web_interface/
│   ├── fyp_data_hub.py      # Flask app entry point (port 5002)
│   ├── data_service.py      # Study cache, PCA computation
│   ├── routes/              # Flask blueprints (auth, data, management, process)
│   ├── templates/           # Jinja2 templates (base + tabs/)
│   └── static/              # JS + CSS (no bundler)
├── tests/                   # Mostly ad-hoc debug/verify scripts
├── prompts/                 # Gemini prompt templates (*.txt)
├── Dockerfile
├── requirements.txt         # Pinned deps
└── requirements312.txt
```

---

## Running the Project

### Development

```bash
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements312.txt

# Set secrets (or put them in environment)
export GEMINI_API_KEY="..."
export FLASK_SECRET_KEY="..."

# Start web app
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
# Gunicorn: 1 worker, 8 threads
```

### Background Workers

```bash
python web_interface/run_queue_annotator.py   # Gemini annotation
python web_interface/run_queue_scraper.py     # TikTok scraping
python web_interface/run_timelines_refresh.py # Timeline updates
```

---

## Tests

No formal test framework (pytest not required). Tests are ad-hoc scripts:

```bash
cd tests
python test_env.py
python test_metadata_filtering.py
python test_calc.py
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

**Environment variables** (override config):

- `GEMINI_API_KEY`
- `FYP_GCS_BUCKET_NAME`
- `FLASK_SECRET_KEY`
- `FLASK_DEBUG`

---

## Key Patterns & Conventions

### Project Root Discovery
`__proj__.py` is an empty sentinel file. `fyp_config.py` walks up the directory tree looking for it to locate the project root — this makes imports work regardless of working directory.

### Data I/O Abstraction
`fyp/data_io.py` abstracts local vs. GCS storage. Use named locations (`"cache"`, `"studies"`, `"users"`) rather than raw paths. Toggle `use_gcs_*` flags in config to switch backends.

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

## Main Entry Points

| File | Purpose |
|---|---|
| `web_interface/fyp_data_hub.py` | Web app (Flask) |
| `web_interface/run_queue_annotator.py` | Gemini annotation worker |
| `web_interface/run_queue_scraper.py` | Scraping worker |
| `web_interface/run_timelines_refresh.py` | Timeline refresh worker |
| `fyp/fyp_config.py` | Config initialisation (`fyp_config.initialize()`) |
| `fyp/ingest.py` | Data ingestion classes |
| `fyp/organize_datasets.py` | Dataset organisation & filtering |
