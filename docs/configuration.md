# Configuration

The For You Data Hub is configured by `config/config.toml` (committed), four declarative TOML
contracts alongside it, and a small set of environment variables. The loader
is `fyp/fyp_config.py`: it walks up the directory tree looking for the empty
sentinel file `__proj__.py` to find the project root, so imports work from
any working directory. **Note:** config loads at `fyp.fyp_config` import
time (it also connects to GCS and synthesizes the variable schema) — see the
import-cycle rule in `CONTRIBUTING.md`.

## config/config.toml sections

| Section | Purpose | Keys you'll actually touch |
|---|---|---|
| `[machine]` | Annotation backends | One `[machine.<backend>]` block per backend (Gemini's is `[machine.gemini]`: `vertexai` — **Vertex AI (default) or the plain Gemini API**; `project`; model, params, `pricing`), variants at `[machine.<backend>.variants.<name>]`. Backend-agnostic keys sit on the top-level `[machine]` table: `max_duration_for_annotation`, plus `est_input_tokens_per_annotation` / `est_output_tokens_per_annotation` (per-item token estimates feeding the pre-queue cost display). Legacy flat `[machine]` keys are hoisted at load. Turning Gemini on after a no-Gemini install: [Enabling Gemini later](installation.md#enabling-gemini-later) |
| `[embedding]` | Embedding backends (semantic space) | One `[embedding.<backend>]` block per backend: `[embedding.gemini]` (default; `model_id`/`dim`/`location`/`task_type` — the model is upgradeable by config edit, no variant system), `[embedding.qwen_api]` (hosted DashScope text embeddings), `[embedding.qwen_local]` (sentence-transformers, `local_embeddings` extra). The active backend is chosen in Admin → Backends, not here |
| `[site]` | instance branding | contact email, mail sender, app URL — overridable via `FYP_CONTACT_EMAIL`/`FYP_MAIL_SENDER`/`FYP_APP_URL` env vars (committed defaults are empty); plus `repo_url` (`FYP_REPO_URL`), the source repository the public pages link to for bug reports, the installation guide and the licence — committed default is the canonical repo, set it empty to drop those links |
| `[paths]` | local storage roots | `local_data` — **set this to a writable directory on your machine**; everything (cache, recoded, users, media) lives under it locally |
| `[misc]` | runtime behavior | timezone, `local_mode`, media duration caps (`max_duration_for_download[_<platform>]`, `max_duration_for_annotation`), `ig_fetch_view_counts` (Instagram count supplementation kill switch), and the optional scraper storm-guard overrides `scraper_permanent_storm_threshold` (default 15) / `scraper_transient_storm_threshold` (default 25) — consecutive identical failures before a batch aborts and raises a scraper alert |
| `[sessions]` | viewing-session identification + Sessions tab | `session_gap_s` (inter-activity gap that closes a session at ingest); binge segmentation (`binge_cut`/`binge_mem`/`binge_min_videos`/`binge_max_skip`/`binge_flick_seconds`/`binge_min_minutes` — baked in at build; changing them needs a sessions_refresh); low-entropy windows (`window_n`/`max_windows`); `context_plays`; query-time knobs `drift_p` and `trend_min_videos`; and the session-list floors `min_session_plays`/`min_session_minutes`/`min_session_coverage_pct` — **seed values only**: Admin → Site Settings → "Sessions tab list floors" overrides them at runtime per key |
| `[correlations]` | Correlations tab | PCA-component offering (`min_variance_pct`, `max_components_per_variable`), `max_scatter_points`, `factor_value_limit`, `correlation_method`, `minimum_group_size`, `interpretation_cutoff`, `permanova_permutations`, `independence_warning_collections`, `max_regression_series` |
| `[web]` | web server | `health_check_max_age_hours` — boot-time system-health check is skipped while the persisted result is younger than this (0 forces a run every boot) |
| `[features]` | feature toggles | rarely changed |
| `[data_io]` | storage backend | GCS bucket name, `use_gcs_*` per-location toggles |
| `[viz]` | dashboard visuals | palette etc. |
| `[labels]` | content categories | category lists, generic mapper, `IRRELEVANT_WORDS` (seed for the admin-editable hashtag stoplist) |

**Don't edit the committed file for machine-local values.** Copy
`config/config.local.toml.example` to `config/config.local.toml` (gitignored)
— it is deep-merged over `config.toml` at load time, so you list only the
keys you override. For a new collaborator that's `[paths] local_data` (and
possibly `[machine.gemini] project` if you have your own Vertex project). CI uses
the same mechanism to redirect storage to a scratch directory.

**Windows paths.** The committed `local_data`/`local_media` defaults are
macOS-style POSIX paths, which don't resolve on Windows. When you run on
Windows without overriding them, `fyp_config` redirects a bare POSIX-absolute
default to `%USERPROFILE%\fyp_local` so the app still starts. To choose your
own location, set a drive path in `config.local.toml` — use forward slashes,
e.g. `local_data = "C:/Users/you/fyp_local"`.

## System dependencies (local dev)

Two external command-line tools are used by the enrichment pipeline (the web
dashboard and the annotation/analysis workers don't need them — only the
scrapers do):

- **ffmpeg** — media assembly during scraping: muxing YouTube's separate DASH
  audio/video streams and building slideshow `.mp4`s from photo/carousel posts
  (moviepy). moviepy ships a bundled copy via the `imageio-ffmpeg` wheel, but a
  system `ffmpeg` on PATH is recommended. On Windows, install it with
  `winget install ffmpeg` (or `choco install ffmpeg`) and confirm it's on PATH.
- **node** or **deno** — YouTube's n-challenge solver (`yt-dlp-ejs`). Optional;
  an absent runtime is simply skipped.

In the Docker image both are provided by the base image, so production needs no
extra setup.

## The four contracts

`annotation_contract.toml`, `scrape_contract.toml`, `activity_contract.toml`,
and `derived_contract.toml` own the variable schemas (field names, dtypes,
display metadata, prompt/response schema for Gemini). They are loaded and
validated by their same-named `fyp/*_contract.py` modules and overlaid onto
the synthesized `var_schema` at config load. The annotation contract can also
be replaced at runtime via the admin UI (stored in `users/`); setting
`FYP_BAKED_CONTRACTS_ONLY=1` forces the committed ("baked") contract —
tests and the golden safety net use this.

## Environment variables

| Variable | Effect |
|---|---|
| `GEMINI_API_KEY` | Gemini API access for annotation/embeddings. Used only when `[machine.gemini].vertexai = false`: with the default `vertexai = true` the app talks to Vertex AI and this key plays no part (the one exception is when no `project` is set, where the app falls back to the key and warns). Not auto-loaded from `.env` — see [Enabling Gemini later](installation.md#enabling-gemini-later) |
| `DASHSCOPE_API_KEY` | API key for the hosted Qwen backends (DashScope's OpenAI-compatible international endpoint). Read by both the `qwen_api` annotation backend and the Qwen API embedding backend — one key serves both. Needed only while one of those is the active backend (Admin → Backends) |
| `FLASK_SECRET_KEY` | Flask session secret (falls back to a dev key locally) |
| `FYP_GCS_BUCKET_NAME` | GCS bucket (production) |
| `K_SERVICE` | Set automatically by Cloud Run — switches storage to GCS and job dispatch to Cloud Tasks |
| `FYP_FORCE_GCS` | Force ALL storage to the prod GCS bucket from a local process (e.g. the local scrape-queue drain runbook in `DEVELOPING.md`); refuses to fall back to local storage if the GCS connection fails |
| `FYP_CONFIG_PATH` | Path to a config TOML to use directly, instead of discovering `config/config.toml` via the `__proj__.py` project-root sentinel — the hook for reusing `fyp` inside another project |
| `FYP_BAKED_CONTRACTS_ONLY` | Ignore any runtime-uploaded annotation contract; use the committed one |
| `FYP_LOG_LEVEL` | Log level for `fyp` modules (`DEBUG`/`INFO`/`WARNING`/`ERROR`; default `INFO`). Logging goes to stdout with a bare message format, so subprocess-worker UI log lines are byte-identical to the pre-logging `print()` output |
| `FLASK_DEBUG` | Optional Flask debug toggle |
| `CLOUD_RUN_SERVICE_URL`, `GCP_PROJECT_ID`, `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_QUEUE`, `CLOUD_TASKS_SA_EMAIL` | Cloud Tasks dispatch configuration (production) |
| `AIO_DYNAMODB_TABLE`, `AIO_S3_BUCKET` | AIO data-donation stack resource names (deployment-specific; only for installations with their own AIO stack) |
| `BGUTIL_POT_SERVER_HOME` | Path to the bgutil PO-token provider script (YouTube media downloads from datacenter IPs) |
| `YTDLP_COOKIE_FILE_<PLATFORM>`, `YTDLP_COOKIE_FILE` | Netscape-format cookie file for scraping, for hosts where neither cookie source applies (Cloud Run reads `gs://<bucket>/secrets/{platform}_cookies.txt`; a local Mac extracts from Chrome). The platform-specific form (`YTDLP_COOKIE_FILE_TIKTOK`, `_INSTAGRAM`, `_YOUTUBE`) takes precedence over the shared one, and **both are ignored unless the path exists on disk** |

## Storage locations

`fyp/data_io.py` maps named locations to directories under `local_data`
locally, or to GCS prefixes in cloud mode (`use_gcs_*` toggles / `K_SERVICE`).
Code must always use location names — `load_parquet("recoded", ...)` — never
absolute paths. Locations can also be registered at runtime
(`data_io.register_location`), which is how platform ingestion classes
self-register their raw-upload directories.

## Admin-editable stores (runtime state, in the `users` location)

- `var_presentation.json` — which variables appear on which UI surface
  (never affects the schema hash)
- `irrelevant_words.json` — hashtag stoplist (seeded from `[labels]`)
- `annotation_contract.toml` — runtime-uploaded annotation contract, if any
- `admin_settings.json` — site settings incl. the annotation/embedding
  backend choice and the Sessions-tab list floors (which override the
  `[sessions]` seed values per key once saved). The `[machine.gemini]`
  model/generation parameters are deliberately NOT here: they are
  config-file-only and need a restart/redeploy to change
- user accounts and per-user settings (JSON files)

## Pinning or A/B-ing annotation model versions (backend variants)

To upgrade a backend's model while keeping the old one selectable — or to A/B
two model generations — declare a **variant** under the backend's block in
`config/config.toml` (or the `config.local.toml` overlay):

```toml
[machine.gemini.variants.gemini_35]
label = "Gemini 3.5 Flash"               # optional display name
model = "gemini-3.5-flash"               # override keys = the parent block's keys
pricing = {input = 0.30, output = 2.50}  # optional, USD per 1M tokens (cost display)
```

After a restart/redeploy the variant appears in Admin → Backends → Machine
annotation and in the Annotation-testing per-arm backend picker. Selecting it
annotates with the overridden model/params and stamps a distinct annotation
version (`av_`) — rows produced under the old model keep their version.
Variant names are lowercase `[a-z0-9_]` and must not reuse a backend id; for
gemini the override keys are the `[machine.gemini]` generation keys (`model`,
`temperature`, `thinking_budget`, `media_resolution`, `max_output_tokens`),
for the other backends the keys of their `[machine.<backend>]` block
(`model_id`, ...). `label` and `pricing` are metadata, never overrides.
Batch-mode annotation runs only on the plain `gemini` selection, and local
backends hold one resident model per worker process.
