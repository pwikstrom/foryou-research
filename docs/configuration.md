# Configuration

FYP is configured by `config/config.toml` (committed), four declarative TOML
contracts alongside it, and a small set of environment variables. The loader
is `fyp/fyp_config.py`: it walks up the directory tree looking for the empty
sentinel file `__proj__.py` to find the project root, so imports work from
any working directory. **Note:** config loads at `fyp.fyp_config` import
time (it also connects to GCS and synthesizes the variable schema) — see the
import-cycle rule in `CONTRIBUTING.md`.

## config/config.toml sections

| Section | Purpose | Keys you'll actually touch |
|---|---|---|
| `[machine]` | Gemini annotation | `vertexai` — **which service to use: Vertex AI (default) or the plain Gemini API**; `project` (the GCP project, required by Vertex); model name, temperature. Turning Gemini on after a no-Gemini install: [Enabling Gemini later](installation.md#enabling-gemini-later) |
| `[paths]` | local storage roots | `local_data` — **set this to a writable directory on your machine**; everything (cache, recoded, users, media) lives under it locally |
| `[misc]` | runtime behavior | timezone, `local_mode`, media duration caps (`max_duration_for_download[_<platform>]`, `max_duration_for_annotation`) |
| `[features]` | feature toggles | rarely changed |
| `[data_io]` | storage backend | GCS bucket name, `use_gcs_*` per-location toggles |
| `[viz]` | dashboard visuals | palette etc. |
| `[labels]` | content categories | category lists, generic mapper, `IRRELEVANT_WORDS` (seed for the admin-editable hashtag stoplist) |

**Don't edit the committed file for machine-local values.** Copy
`config/config.local.toml.example` to `config/config.local.toml` (gitignored)
— it is deep-merged over `config.toml` at load time, so you list only the
keys you override. For a new collaborator that's `[paths] local_data` (and
possibly `[machine] project` if you have your own Vertex project). CI uses
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
| `GEMINI_API_KEY` | Gemini API access for annotation/embeddings. Used only when `[machine].vertexai = false`: with the default `vertexai = true` the app talks to Vertex AI and this key plays no part (the one exception is when no `project` is set, where the app falls back to the key and warns). Not auto-loaded from `.env` — see [Enabling Gemini later](installation.md#enabling-gemini-later) |
| `FLASK_SECRET_KEY` | Flask session secret (falls back to a dev key locally) |
| `FYP_GCS_BUCKET_NAME` | GCS bucket (production) |
| `K_SERVICE` | Set automatically by Cloud Run — switches storage to GCS and job dispatch to Cloud Tasks |
| `FYP_FORCE_GCS` | Force ALL storage to the prod GCS bucket from a local process (e.g. the local scrape-queue drain runbook in `CLAUDE.md`); refuses to fall back to local storage if the GCS connection fails |
| `FYP_CONFIG_PATH` | Path to a config TOML to use directly, instead of discovering `config/config.toml` via the `__proj__.py` project-root sentinel — the hook for reusing `fyp` inside another project |
| `FYP_BAKED_CONTRACTS_ONLY` | Ignore any runtime-uploaded annotation contract; use the committed one |
| `FYP_LOG_LEVEL` | Log level for `fyp` modules (`DEBUG`/`INFO`/`WARNING`/`ERROR`; default `INFO`). Logging goes to stdout with a bare message format, so subprocess-worker UI log lines are byte-identical to the pre-logging `print()` output |
| `FLASK_DEBUG` | Optional Flask debug toggle |
| `CLOUD_RUN_SERVICE_URL`, `GCP_PROJECT_ID`, `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_QUEUE`, `CLOUD_TASKS_SA_EMAIL` | Cloud Tasks dispatch configuration (production) |
| `BGUTIL_POT_SERVER_HOME` | Path to the bgutil PO-token provider script (YouTube media downloads from datacenter IPs) |

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
  backend choice. The `[machine]` model/generation parameters are deliberately
  NOT here: they are config-file-only and need a restart/redeploy to change
- user accounts and per-user settings (JSON files)

## Pinning or A/B-ing annotation model versions (backend variants)

To upgrade a backend's model while keeping the old one selectable — or to A/B
two model generations — declare a **variant** in `config/config.toml` (or the
`config.local.toml` overlay):

```toml
[machine.variants.gemini_35]
backend = "gemini"           # implementation: gemini | qwen_api | qwen_local | minicpm_local
label = "Gemini 3.5 Flash"   # optional display name
model = "gemini-3.5-flash"   # override keys = the implementation's config keys
```

After a restart/redeploy the variant appears in Admin → General → Machine
annotation and in the Annotation-testing per-arm backend picker. Selecting it
annotates with the overridden model/params and stamps a distinct annotation
version (`av_`) — rows produced under the old model keep their version.
Variant names are lowercase `[a-z0-9_]` and must not reuse a backend id; for
gemini the override keys are the `[machine]` keys (`model`, `temperature`,
`thinking_budget`, `media_resolution`, `max_output_tokens`), for the other
backends the keys of their `[machine.<backend>]` block (`model_id`, ...).
Batch-mode annotation runs only on the plain `gemini` selection, and local
backends hold one resident model per worker process.
