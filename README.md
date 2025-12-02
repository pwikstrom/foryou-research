# For You Project – data + analysis stack

- **Purpose**: ingest TikTok data donations and baseline captures, enrich with scraping/LLM coding, run analysis, and serve a simple dashboard of viewing logs.
- **Tech**: Python notebooks + helper package (`fyp`), FastAPI + React dashboard, GCP bucket for media, optional Gemini integration.

## Repo layout
- `config/`: `core.toml` chooses config file + study defs; `config.toml` holds paths (data dirs, temp/backup), Gemini model/prompt, GCP bucket; `studies.toml` defines study presets (date windows, sampling, inclusion of Zeeschuimer data).
- `fyp/`: core helpers.
  - `fyp_main.py`: finds project root via `__proj__.py`, loads config, ensures directories, optionally inits Gemini client + GCP bucket handle; utilities for temp/backup paths, pattern checks, URL parsing.
  - `mypyktok.py`: PykTok-based TikTok fetch/normalisation (cookies via `browser_cookie3`, JSON scraping, pandas row builder with author/music/stats fields).
  - `donations.py`: AWS DynamoDB scan + S3 download helpers for recent data donations.
  - `pca.py`: distance/similarity matrices for categorical counts (JS, Hellinger, TV, Bray-Curtis, chi²) with smoothing/weighting/tempering; entropy/dominance helpers.
  - `stats.py`: ANOVA with effect sizes; PERMANOVA variants over PCA scores/features.
  - `recode_variables.py` and `machine_annotation.py`: recoding and LLM coding utilities.
  - Other helpers: `download_videos.py`, `get_baseline_log.py`.
- `analysis_notebooks/`: main analysis notebook (`main_analysis_notebook_004.ipynb`) plus variable scheme (`var_scheme.csv`).
- `baseline_ingest/`, `ddp_ingest/`, `enrich_tiktok_data/`, `organise_and_export_datasets/`: ingestion, enrichment, export notebooks and artifacts (e.g., `special_ids.pkl`).
- `ddp_dashboard/`: small analytics app.
  - `backend/` (FastAPI, Polars): loads `data/views.parquet` and serves `/views`, `/daily_counts`, `/top_videos`.
  - `frontend/` (Vite + React): filter form + table consuming backend API (`src/api.js`).
  - `scripts/convert_csv_to_parquet.py`: helper for dashboard data prep.
- `prompts/`: Gemini prompt files referenced by config.
- `__proj__.py`: empty marker file so code can locate project root.

## Setup (local)
- Python: create/activate a virtualenv; install `pip install -r requirements.txt` (or the slim `requirements_1.txt` set).
- Node (dashboard): from `ddp_dashboard/frontend`, run `npm install`.
- AWS CLI + credentials required for `fyp/donations.py` download helpers.
- GCP auth required for media bucket access if `local_mode` is false.

## Configuration
- Edit `config/config.toml` for paths, Gemini model/prompt, GCP bucket, and local_mode toggle; pick active config via `config/core.toml`.
- Study presets (date ranges, sampling, inclusion flags) live in `config/studies.toml`; `init_config` injects selected study into runtime config.
- Set `GEMINI_API_KEY` in your environment; the config file leaves the key blank to avoid committing secrets.

## Typical workflows
- **Ingest (donations/baseline)**: notebooks in `ddp_ingest/` and `baseline_ingest/` pull raw logs/metadata; `fyp/donations.py` can scan/download recent donations via AWS CLI.
- **Enrich**: notebooks in `enrich_tiktok_data/` scrape TikTok video metadata, run Gemini annotation, etc. `mypyktok.py` powers scraping/normalisation.
- **Organise/export**: notebooks in `organise_and_export_datasets/` clean and package datasets; uses study presets from config.
- **Analysis**: `analysis_notebooks/main_analysis_notebook_004.ipynb` leverages `fyp/pca.py`, `fyp/stats.py`, and `var_scheme.csv` for PCA/ANOVA/PERMANOVA and feature interpretation.
- **Dashboard**: prepare `ddp_dashboard/backend/data/views.parquet` (or use `scripts/convert_csv_to_parquet.py`), start backend (`uvicorn app.main:app` from `ddp_dashboard/backend`), then frontend (`npm run dev` from `ddp_dashboard/frontend`).

## Quick start snippets
- Load config + ensure directories:
  ```bash
  python - <<'PY'
  from fyp.fyp_main import init_project
  cf = init_project(verbose=True)
  print(cf["paths"]["main"])
  PY
  ```
- Fetch recent donation metadata (requires AWS CLI creds):
  ```bash
  python - <<'PY'
  from fyp.donations import download_recent_metadata
  download_recent_metadata(hours_back=6, output_dir="baseline_ingest/raw_metadata")
  PY
  ```
- Run dashboard backend:
  ```bash
  cd ddp_dashboard/backend
  uvicorn app.main:app --reload
  ```
- Run dashboard frontend:
  ```bash
  cd ddp_dashboard/frontend
  npm run dev
  ```

## Notes and cautions
- `fyp_main.py` will print project root and attach it to `sys.path`; it also tries to init Gemini and GCP unless `local_mode=true`.
- Data paths in `config.toml` point to local Google Drive/temp locations—adjust to your environment before running ingestion/enrichment notebooks.
- Parquet/CSV artifacts under `ddp_dashboard/backend/data/` are sample data; replace with your exports as needed.
