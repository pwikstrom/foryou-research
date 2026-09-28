# Configuration

The For You Data Hub is configured by `config/config.toml` (committed), four declarative TOML
contracts alongside it, and a small set of environment variables. The loader
is `fyp/core/fyp_config.py`; the project root is found by
`fyp/core/paths.py`, which walks up the directory tree looking for the empty
sentinel file `__proj__.py` (unless `FYP_CONFIG_PATH` names a config TOML
directly), so imports work from any working directory. **Note:**
configuration loads lazily — importing `fyp` submodules does not touch it
(`fyp.ingest` is the one deliberate exception); the first `get_config()` /
`fyp_cf` access triggers the load, which also connects to GCS and synthesizes
the variable schema. That load calls back into several low-level modules,
which is why they read the config through the function-level accessor
`fyp.core.runtime.cf` rather than importing `fyp_cf` at module level — see
the import-cycle rule in `CONTRIBUTING.md`.

## config/config.toml sections

| Section | Purpose | Keys you'll actually touch |
|---|---|---|
| `[machine]` | Annotation backends | One `[machine.<backend>]` block per backend (Gemini's is `[machine.gemini]`: `vertexai` — **Vertex AI (default) or the plain Gemini API**; `project`; model, params, `pricing`), variants at `[machine.<backend>.variants.<name>]`. Backend-agnostic keys sit on the top-level `[machine]` table: `max_duration_for_annotation`, plus `est_input_tokens_per_annotation` / `est_output_tokens_per_annotation` (per-item token estimates feeding the pre-queue cost display). Legacy flat `[machine]` keys are hoisted at load. Turning Gemini on after a no-Gemini install: [Enabling Gemini later](installation.md#enabling-gemini-later) |
| `[embedding]` | Embedding backends (semantic space) | One `[embedding.<backend>]` block per backend: `[embedding.gemini]` (default; `model_id`/`dim`/`location`/`task_type` — the model is upgradeable by config edit, no variant system), `[embedding.qwen_api]` (hosted DashScope text embeddings), `[embedding.qwen_local]` (sentence-transformers, `local_embeddings` extra). The active backend is chosen in Admin → Backends, not here |
| `[site]` | instance branding | contact email, mail sender, app URL — overridable via `FYP_CONTACT_EMAIL`/`FYP_MAIL_SENDER`/`FYP_APP_URL` env vars (committed defaults are empty). `app_url` has a second role beyond email links: it is the **canonical origin for SEO** — the canonical `<link>`, the sitemap, the JSON-LD and the `www.`→apex redirect all derive from it (`web_interface/seo.py`). Pointing it at the raw `*.run.app` host de-indexes the real domain; leaving it empty falls back to the request host and disables the redirect. Also `participant_placeholder_domain` (default `"foryouresearch.net"`, env `FYP_PARTICIPANT_PLACEHOLDER_DOMAIN`) — the domain of the fake `p-N@<domain>` addresses given to placeholder participant accounts; `ops_report_email` — recipient of the daily ops report (read in `web_interface/services/ops_report.py`, falls back to `mail_sender`; this key exists only in code, not in the committed `config.toml`); plus `repo_url` (`FYP_REPO_URL`), the source repository the public pages link to for bug reports, the installation guide and the licence — committed default is the canonical repo, set it empty to drop those links |
| `[paths]` | local storage roots | `local_data` — **set this to a writable directory on your machine**; everything (cache, recoded, users, media) lives under it locally |
| `[misc]` | runtime behavior | `TIME_ZONE` (display/log timestamps), `local_mode`, the media download duration cap (`max_duration_for_download[_<platform>]`; the annotation cap `max_duration_for_annotation` lives under `[machine]`), `min_media_object_size` (bytes; a stored media file smaller than this counts as missing), `ig_fetch_view_counts` (Instagram count supplementation kill switch), and optional overrides that are absent from the committed file unless noted: `connectivity_probe_host` (default `connectivitycheck.gstatic.com` — the host probed before connecting to GCS at config load and, with the platform's own host, by the scrapers' network-outage gate); the scraper storm-guard thresholds `scraper_permanent_storm_threshold` (default 15) / `scraper_transient_storm_threshold` (default 25) — consecutive identical failures before a batch aborts and raises a scraper alert; `scraper_offline_max_wait_seconds` (default 1800 locally, 120 on Cloud Run) — how long a batch waits out a network outage before stopping without an alert; `scraper_local_batch_deadline_seconds` (default 14400 — the wall-clock ceiling of one batch in a local drain; Cloud Run is fixed at 1800); `scraper_memory_stop_fraction` (default 0.60 — share of the container memory limit at which a batch stops launching downloads and defers the rest to the queue); YouTube pacing `scraper_youtube_max_concurrency` (default 2) / `scraper_youtube_inter_request_delay` (default 5.0 s) / `scraper_youtube_max_batch_size` (default 250, 0 = no cap); `scraper_instagram_auth_interval` (default 20 s between logged-in Instagram requests); `max_media_download_bytes` (default 1 GiB — the TikTok scraper's per-download ceiling); `slideshow_max_dimension` (default 1000 px — longest edge of a photo-post slideshow) |
| `[sessions]` | viewing-session identification + Sessions tab | `session_gap_s` (inter-activity gap that closes a session at ingest); binge segmentation (`binge_cut`/`binge_mem`/`binge_min_videos`/`binge_max_skip`/`binge_flick_seconds`/`binge_min_minutes` — baked in at build; changing them needs a sessions_refresh); low-entropy windows (`window_n`/`max_windows`); the refresh worker's `workers` (forked process pool for per-session segmentation, `"auto"` = one per core less one; never changes the rows), `vector_cache` (cache the dense embedding parts whole on the task-runner instance) and `rebaseline_fraction` (share of vectors appended since the last full build beyond which a scoped enrichment refresh becomes a full one, default 0.05); `context_plays`; query-time knobs `drift_p` and `trend_min_videos`; and the session-list floors `min_session_plays`/`min_session_minutes`/`min_session_coverage_pct` — **seed values only**: Admin → Site Settings → "Sessions tab list floors" overrides them at runtime per key |
| `[studies]` | study size guardrail | `max_activities` (default 500000) — hard cap on the number of activities a single study may contain, enforced both in the study modal and server-side on save (`web_interface/routes/management/studies.py`); `web_interface/services/stats_service.py` falls back to its built-in `LARGE_STUDY_THRESHOLD` when the section is absent (configs predating it) |
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
`~/fyp_local` and `~/fyp_local/media`; a leading `~` expands to the current
user's home directory on every platform. A bare POSIX-absolute path (e.g. one
carried over from an older config) doesn't resolve on Windows, so when the app
meets one there `fyp_config` redirects it to `%USERPROFILE%\fyp_local` and
still starts. To choose your
own location, set a drive path in `config.local.toml` — use forward slashes,
e.g. `local_data = "C:/Users/you/fyp_local"`.

## System dependencies (local dev)

Two external command-line tools are used by the enrichment pipeline (the web
dashboard, the Gemini/hosted annotation backends and the analysis workers don't
need them):

- **ffmpeg** — a system `ffmpeg` on PATH is needed for YouTube HD media (yt-dlp
  merges YouTube's separate DASH audio/video streams with it), for its
  `ffprobe`, which backfills the duration of Instagram videos (without it the
  duration stays empty), and for the local Qwen/MiniCPM annotation backends
  (frame sampling and audio extraction). Building slideshow `.mp4`s from
  photo/carousel posts does not need it: moviepy uses the binary bundled in
  the `imageio-ffmpeg` wheel. On Windows, install it with
  `winget install ffmpeg` (or `choco install ffmpeg`) and confirm it's on PATH.
- **node** or **deno** — YouTube's n-challenge solver (`yt-dlp-ejs`). Optional;
  an absent runtime is simply skipped.

In the Docker image both are provided by the base image, so production needs no
extra setup.

## The four contracts

`annotation_contract.toml`, `scrape_contract.toml`, `activity_contract.toml`,
and `derived_contract.toml` own the variable schemas (field names, dtypes,
display metadata, and — for the annotation contract — the generated prompt
and structured response schema used by every annotation backend). They are
loaded and **validated** by their same-named loader modules
(`fyp/annotation/annotation_contract.py`, `fyp/scrape/scrape_contract.py`,
`fyp/core/activity_contract.py`, `fyp/core/derived_contract.py`); a contract that fails validation raises on explicit loads (tests,
tools, the scrapers), while the config-load overlay paths degrade to a
warned fallback so the app still boots. The validated contracts are
overlaid onto the synthesized `var_schema` at config load. The annotation
contract can also be replaced at runtime via the admin UI (stored in
`users/`); setting `FYP_BAKED_CONTRACTS_ONLY=1` forces the committed
("baked") contract — tests and the golden safety net use this. The full
guide — authoring keys, validation, the runtime upload/promote flow, and
what a contract change costs operationally — is [contracts.md](contracts.md).

## Environment variables

| Variable | Effect |
|---|---|
| `GEMINI_API_KEY` | Gemini API access for annotation/embeddings. Used only when `[machine.gemini].vertexai = false`: with the default `vertexai = true` the app talks to Vertex AI and this key plays no part (the one exception is when no `project` is set, where the app falls back to the key and warns). Can live in `.env` at the project root, which is loaded automatically at startup — see [Enabling Gemini later](installation.md#enabling-gemini-later) |
| `DASHSCOPE_API_KEY` | API key for the hosted Qwen backends (DashScope's OpenAI-compatible international endpoint). Read by both the `qwen_api` annotation backend and the Qwen API embedding backend — one key serves both. Needed only while one of those is the active backend (Admin → Backends) |
| `FLASK_SECRET_KEY` | Flask session secret (falls back to a dev key locally) |
| `FYP_GCS_BUCKET_NAME` | GCS bucket (production) |
| `K_SERVICE` | Set automatically by Cloud Run — switches storage to GCS and job dispatch to Cloud Tasks |
| `FYP_FORCE_GCS` | Force ALL storage to the prod GCS bucket from a local process (e.g. the local scrape-queue drain runbook in `DEVELOPING.md`); refuses to fall back to local storage if the GCS connection fails |
| `FYP_CONFIG_PATH` | Path to a config TOML to use directly, instead of discovering `config/config.toml` via the `__proj__.py` project-root sentinel — the hook for reusing `fyp` inside another project |
| `FYP_BAKED_CONTRACTS_ONLY` | Ignore any runtime-uploaded annotation contract; use the committed one |
| `FYP_LOG_LEVEL` | Log level for `fyp` modules (`DEBUG`/`INFO`/`WARNING`/`ERROR`; default `INFO`). Logging goes to stdout with a bare message format, so subprocess-worker UI log lines are byte-identical to the pre-logging `print()` output |
| `FLASK_DEBUG` | Optional Flask debug toggle |
| `FYP_VERTEX_PROJECT` | Vertex AI project override for Gemini. When `[machine.gemini].project` is empty the app falls back to this, then to `GCP_PROJECT_ID` |
| `FYP_CONTACT_EMAIL`, `FYP_MAIL_SENDER`, `FYP_APP_URL`, `FYP_REPO_URL` | Env overrides of the `[site]` branding keys (contact email, outbound-mail sender, public instance URL, source-repository URL) — the deployed services set these; locally use `config.local.toml`. `FYP_APP_URL` also sets the canonical SEO origin (see the `[site]` row above) — never point it at the raw `*.run.app` host |
| `FYP_PARTICIPANT_PLACEHOLDER_DOMAIN` | Env override of `[site] participant_placeholder_domain` — the domain used for placeholder `p-N@<domain>` participant account addresses |
| `MAIL_PASSWORD` | SMTP password for outbound mail. Mail no-ops unless BOTH the sender and this are set |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID` | Optional Slack integration for feedback/notifications; the feature is hidden while unset |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` | AWS credentials for the AIO donation fetch (standard boto3 chain; `~/.aws/credentials` works too) |
| `FYP_DENSE_CACHE_DIR` | Local directory for the dense embedding sidecar's cached part files when the store is on GCS (`fyp/analysis/embedding_store.py`; default `<system temp dir>/fyp_dense_cache`) |
| `HF_HOME` / `HF_HUB_CACHE` | Hugging Face cache directory, honoured by the local Qwen/MiniCPM backends when locating downloaded model weights |
| `CLOUD_RUN_SERVICE_URL`, `GCP_PROJECT_ID`, `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_QUEUE`, `CLOUD_TASKS_SA_EMAIL` | Cloud Tasks dispatch configuration (production) |
| `AIO_DYNAMODB_TABLE`, `AIO_S3_BUCKET` | AIO data-donation stack resource names (deployment-specific; only for installations with their own AIO stack) |
| `BGUTIL_POT_SERVER_HOME` | Path to the bgutil PO-token provider script (YouTube media downloads from datacenter IPs) |
| `YTDLP_COOKIE_FILE_<PLATFORM>`, `YTDLP_COOKIE_FILE` | Netscape-format cookie file for scraping, for hosts where neither cookie source applies (Cloud Run reads `gs://<bucket>/secrets/{platform}_cookies.txt`; a local Mac extracts from Chrome). The platform-specific form (`YTDLP_COOKIE_FILE_TIKTOK`, `_INSTAGRAM`, `_YOUTUBE`) takes precedence over the shared one, and **both are ignored unless the path exists on disk** |

## Storage locations

`fyp/core/data_io.py` maps named locations to directories under `local_data`
locally, or to GCS prefixes in cloud mode (`use_gcs_*` toggles / `K_SERVICE`).
Code must always use location names — `load_parquet("recoded", ...)` — never
absolute paths. Locations can also be registered at runtime
(`data_io.register_location`), which is how platform ingestion classes
self-register their raw-upload directories.

Only `local_data` (and `local_media`) come from config; every location
below it is a fixed string in `fyp/core/fyp_config.py` or an ingestion
class's `raw_path`. The raw-upload layout under `activity_data/` is
therefore not configurable and is inconsistently named for historical
reasons: the TikTok folders are keyed by source (`ddp/ddp_raw`,
`aio/aio_raw`, `zeeschuimer/zeeschuimer_raw`) while Instagram and YouTube
are keyed by platform (`instagram/instagram_raw`, `youtube/youtube_raw`).
See DEVELOPING.md ("Raw-folder naming is inconsistent") before renaming.

**Cloud mode housekeeping.** `machine_annotations_batch_input/` and
`machine_annotations_batch_output/` grow without bound (see
[pipeline.md](pipeline.md) §Annotation). Put a bucket lifecycle rule on
them; the app does not clean them up.

## Admin-editable stores (runtime state, in the `users` location)

- `var_presentation.json` — which variables appear on which UI surface
  (never affects the schema hash)
- `irrelevant_words.json` — hashtag stoplist (seeded from `[labels]`)
- `annotation_contract.toml` — runtime-uploaded annotation contract, if any
- `admin_settings.json` — site settings incl. the annotation/embedding
  backend choice and the Sessions-tab list floors (which override the
  `[sessions]` seed values per key once saved). The other defaults:
  `new_user_admin_approval_required` (`True` — new signups land inactive
  until an admin approves them), `signup_email_verification_required`
  (`True` — a signup must open an emailed link before it can log in;
  effective only when outgoing mail is configured), `default_new_user_role`
  (`"viewer"`),
  `default_study` (`""`), `demo_collection` (`""` — the collection the
  guided tour uses; it must belong to the default study), and the non-admin
  queue caps `queue_cap_annotation_items` (5000) /
  `queue_cap_scrape_items` (10000). Note that setting `default_study`
  makes that study readable by **every** logged-in user regardless of its
  `USER_ACCESS` list. The `[machine.gemini]`
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
annotation and in the per-arm backend picker of the A/B evaluation panel
(Admin → Contracts). Selecting it
annotates with the overridden model/params and stamps a distinct annotation
version (`av_`) — rows produced under the old model keep their version.
Variant names are lowercase `[a-z0-9_]` and must not reuse a backend id; for
gemini the override keys are the `[machine.gemini]` generation keys (`model`,
`temperature`, `thinking_budget`, `media_resolution`, `max_output_tokens`),
for the other backends the keys of their `[machine.<backend>]` block
(`model_id`, ...). `label` and `pricing` are metadata, never overrides.
Batch-mode annotation runs only on the plain `gemini` selection, and local
backends hold one resident model per worker process.

A worked example of the full nested `[machine.<backend>]` layout, and the
developer checklist for authoring a brand-new annotation or embedding
backend, are in [extending.md](extending.md).
