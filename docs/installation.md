# Installing FYP from scratch

This guide walks through a complete local installation on a new machine,
starting with no data and no credentials. The short version lives in the
[README Quickstart](../README.md#quickstart-local-development); this page
covers the details, the optional pieces, and what to expect on first run.

## What works with zero credentials

A plain local install — no Gemini key, no Google Cloud account, no Slack, no
mail — gives you the full platform minus LLM features:

- the web dashboard with login and role-based access,
- data-donation upload (TikTok/Instagram/YouTube DDP zips) and ingestion,
- scraping (metadata + media) on your own machine,
- statistics, timelines, and the data explorer.

Only Gemini-dependent features are off until configured: machine annotation,
embeddings, the semantic map, and the Correlations tab (which needs
annotation-derived variables). Slack and email integrations silently no-op
when unconfigured. Everything stores to the local filesystem by default
(`~/fyp_local`).

## Prerequisites

| Requirement | Needed for | Notes |
|---|---|---|
| **Python 3.12** | everything | Matches production; see [python-versions.md](python-versions.md). `brew install python@3.12` / `apt install python3.12` |
| `ffmpeg` | YouTube HD media only | yt-dlp needs it to merge DASH video+audio. TikTok/Instagram downloads and photo-slideshow assembly work without it (bundled `imageio-ffmpeg`). `brew install ffmpeg` / `apt install ffmpeg` |
| `node` *or* `deno` | YouTube media from datacenter IPs | Runs yt-dlp's JS challenge solver. Usually unnecessary on a home (residential) connection. |
| Google Chrome, logged in | authenticated scraping | Cookies are read from the local Chrome profile — **macOS only** (approve the Keychain prompt on first use). Instagram scraping effectively requires this; TikTok/YouTube degrade to public-content access. On Linux, provide a Netscape cookies file via `YTDLP_COOKIE_FILE` instead. |

## Install

```bash
git clone https://github.com/pwikstrom/foryou-research.git
cd foryou-research
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -e .
```

There is a single dependency set — optional services (Gemini, GCS, Slack) are
enabled by configuration, not by extra installs.

## Configure: the setup wizard

```bash
python scripts/setup.py
```

The wizard asks for:

1. **Data directory** (default `~/fyp_local`) — all project data except media.
2. **Media directory** (default `<data>/media`) — video files; needs disk space.
3. **Platforms you plan to scrape** — informational; tailors the printed
   guidance. All platforms are always available in the app.
4. **Gemini annotation** — off / plain API key (`GEMINI_API_KEY`) / Vertex AI
   (your own GCP project, authenticated via `gcloud auth application-default
   login`).
5. **Google Cloud Storage** — off by default; if on, the bucket name.
6. **Contact email** (optional) — shown on the public guide/FAQ pages and the
   home-tab feedback note; those passages are hidden when skipped.

It writes `config/config.local.toml`, a gitignored overlay merged over the
committed `config/config.toml` — the committed file is never edited. Re-run
the wizard any time; it uses your current values as defaults and backs up
the old file. `python scripts/setup.py --check-only` just runs the
environment checks. Flags (`--data-dir`, `--no-gemini`, `--yes`, ...) allow a
fully non-interactive run — see `--help`.

**Manual alternative:** `cp config/config.local.toml.example
config/config.local.toml` and edit it. Key reference:
[configuration.md](configuration.md). If you write a `.env` file, note it is
**not** auto-loaded — `set -a; source .env; set +a` or export the values
yourself.

## Enabling Gemini later

Everything in this section is for the common case: you installed without
Gemini, used the app happily for a while, and now want machine annotation
(and the embeddings / semantic map / Correlations that build on it). Nothing
here requires reinstalling or touching your data.

Pick one of two ways to reach Gemini. **The API key is the easier one** and
needs no Google Cloud account at all.

### Option A — plain Gemini API key

1. Create a key at [Google AI Studio](https://aistudio.google.com/apikey).
2. Tell the app to use the plain API rather than Vertex, by adding this to
   `config/config.local.toml` (create the file if it doesn't exist):

   ```toml
   [machine.gemini]
   vertexai = false
   ```

3. Provide the key. Either export it in the shell you start the app from:

   ```bash
   export GEMINI_API_KEY=your-key-here
   ```

   or put `GEMINI_API_KEY=your-key-here` in a `.env` file at the project root
   and load it — `.env` is **not** read automatically:

   ```bash
   set -a; source .env; set +a
   ```

4. Restart the app. Data Management → Enrichment will now let you queue
   annotation.

> **Don't skip step 2.** `vertexai = true` is the default, and it is what
> decides which service the app talks to — the key alone does not switch it.
> As a safety net the app recognises the specific case of "Vertex enabled, no
> project set, but a key available" and falls back to the key rather than
> failing, so setting only the key does work. It logs a warning each run until
> you set `vertexai = false` to make the choice explicit.

### Option B — Vertex AI

Choose this if you already have a Google Cloud project, or want billing and
quota handled through GCP.

1. A GCP project with billing enabled and the **Vertex AI API** turned on.
2. Authenticate with Application Default Credentials:

   ```bash
   gcloud auth application-default login
   ```

3. Point the app at the project in `config/config.local.toml`:

   ```toml
   [machine.gemini]
   vertexai = true
   project = "your-gcp-project-id"
   ```

4. Restart the app.

### Or just re-run the wizard

`python scripts/setup.py` walks through the same choice, writes the config for
you, and backs up your existing overlay first. Your answers from last time are
the defaults, so you can change only the Gemini question and press Enter
through the rest. It never touches your data directory.

### Which features each option enables

| | Vertex AI | API key |
|---|---|---|
| Machine annotation (media on local disk) | yes | yes |
| Machine annotation (media on GCS) | yes | **no** |
| Embeddings, semantic map, niche naming | yes | yes |

The one real difference is media storage. Media kept on GCS is handed to
Gemini as a `gs://` URI, which only Vertex AI can read; the plain Gemini API
is only ever given media inlined from local disk. This affects nobody running
the default local setup (`use_gcs_for_media = false`) — but if you have
switched media storage to GCS, annotation needs Vertex. The app checks this
combination up front and explains it rather than failing per video.

## Enabling local Qwen annotation

The app can annotate with a **local open-weights model** instead of Gemini:
Qwen3-Omni-30B (Apache 2.0) running entirely on your own machine via Apple's
MLX. Nothing leaves your computer and there are no API costs. In a 20-video
evaluation against Gemini Flash it matched well on categorical/visual fields
and produced near-verbatim speech transcripts (it hears the audio track);
Gemini remains somewhat stronger on exhaustive lists and prose detail.

**Is your machine suitable?** The short version:

| Requirement | Minimum | Notes |
|---|---|---|
| Computer | Apple Silicon Mac (M1–M4) | Intel Macs, Windows, Linux and Cloud Run are **not supported** in this version — keep using Gemini there |
| Memory | 32 GB unified memory | 48 GB+ recommended; the model peaks at ~23 GB, so on 32 GB close other big apps |
| Disk | ~20 GB free | one-time model download (~18 GB), stored in your Hugging Face cache |
| Tools | `ffmpeg` | used to sample video frames and extract the audio track |

You never have to check these by hand — the app does it for you and tells you
exactly what to fix (see step 1).

### Step-by-step

1. **Check your machine.** Either of these shows the same seven checks, each
   failing row with a copy-pasteable fix:

   ```bash
   python scripts/setup.py --check-only     # look for the "local qwen:" rows
   ```

   or, in the running app: **Admin → Backends → Machine annotation** — the
   requirements panel under the backend selector.

2. **Install the local runtime** into the app's virtualenv (~a few minutes;
   this pulls `mlx-vlm` and its dependencies — it is *not* part of the normal
   install because it only exists for Apple Silicon):

   ```bash
   source .venv/bin/activate
   pip install -e ".[local_qwen]"
   ```

3. **Download the model weights once** (~18 GB — allow time on slow
   connections; the download resumes if interrupted):

   ```bash
   hf download mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit
   ```

   The weights land in `~/.cache/huggingface/` and are shared with anything
   else that uses Hugging Face. To store them elsewhere, set `HF_HOME` before
   downloading (and start the app with the same variable set).

4. **Install ffmpeg** if the check flagged it:

   ```bash
   brew install ffmpeg
   ```

5. **Switch the backend.** Restart the app if it was running during the
   installs, then go to **Admin → Backends → Machine annotation**, confirm the
   requirements panel is all green, and set the backend to `qwen_local`.
   From now on Data Management → Enrichment's annotator runs the local model
   (the card shows a `qwen_local` badge). Switch back to `gemini` at any
   time — it's just a setting.

6. **Sanity-check with a small batch first.** Queue a handful of videos and
   start the annotator. The first item is slow (the model loads once,
   ~1–2 minutes); after that expect roughly **30 s per video**. Spot-check
   the results in Video Analysis before committing to a big run.

### Tips and things to know

* **Your annotations get their own version.** The local model, its prompt
  addendum and its sampling parameters are part of the annotation-version
  identity, so Qwen-produced rows land under their own `av_` id — they never
  silently mix with Gemini rows. See Admin → Annotation Versions. The same
  applies when you later switch back.
* **Try it in Annotation Testing first.** An A/B run with the live contract
  on `gemini` vs `qwen_local` (Admin → Annotation Testing, per-arm backend
  selector) shows you field-by-field agreement on your own data before you
  change the production backend.
* **It's sequential.** One video at a time — the model occupies the whole
  GPU. Batch-mode (async) annotation stays Gemini-only; the app refuses the
  combination rather than failing mid-run.
* **Laptop practicalities.** Keep the Mac on power and awake for long runs
  (`caffeinate -i` in the terminal that runs the worker, or just run it from
  the app and disable sleep). Expect the fans.
* **Tuning** lives in `config/config.toml` `[machine.qwen_local]` (frames per
  video, frame resolution, audio on/off). The defaults are pilot-validated —
  doubling the frame budget measurably did not improve agreement, so change
  them only with an A/B run to back it up (changes fork a new version).
* **mlx-vlm version pin.** The integration patches two known mlx-vlm 0.6.x
  bugs at load ([#1619](https://github.com/Blaizzy/mlx-vlm/issues/1619),
  [#1620](https://github.com/Blaizzy/mlx-vlm/issues/1620)); on newer mlx-vlm
  releases the patches step aside automatically. Don't upgrade the pin in
  `pyproject.toml` without re-validating annotation output.

### Troubleshooting

* **"mlx-vlm installed: not installed in this environment"** — you installed
  it into a different virtualenv. Activate the app's `.venv` and re-run
  step 2.
* **"Model downloaded: … not found"** after downloading — the app is looking
  in a different Hugging Face cache. If you set `HF_HOME`/`HF_HUB_CACHE` when
  downloading, start the app with the same variable.
* **The backend selector is greyed out** — hover the requirements panel: any
  red row explains why, with the fix command.
* **First annotation takes minutes** — that's the one-time model load; watch
  the worker log line "Loading local Qwen model…". Subsequent items are fast.
* **Out-of-memory crashes** — close memory-heavy apps (browsers with many
  tabs, IDEs); on 32 GB machines consider reducing `max_frames` to 6 in
  `[machine.qwen_local]`.

## Enabling hosted Qwen annotation

The `qwen_api` backend annotates via Alibaba Model Studio's hosted Qwen omni
models (default `qwen3.5-omni-flash`, DashScope OpenAI-compatible endpoint,
international/Singapore region). Unlike the local backends it needs no special
hardware, runs on Cloud Run, and consumes the whole video natively — audio
track included (verified in the 2026-07 pilot: near-verbatim transcripts,
enum agreement vs Gemini above both local backends).

1. Create an Alibaba Cloud account, activate **Model Studio** in the
   **international** region, and create an API key.
2. Set the `DASHSCOPE_API_KEY` environment variable where the annotation
   worker runs (locally in your shell; on Cloud Run on the `fyp-task-runner`
   service).
3. Select **Hosted Qwen (DashScope)** under Admin → Backends → Annotation.
   The requirements panel runs the key/endpoint checks.

Notes:

* Data residency: donated media is uploaded (base64) to Alibaba's Singapore
  region for inference. Clear this with your ethics process before enabling.
* Throughput is bound by the account-level rate limits (2026-07: 60 requests
  and 100k tokens per minute for the omni models — roughly 5 videos/minute at
  typical short-form lengths), not by worker threads. The backend uses a small
  thread pool (`[machine.qwen_api].max_workers`, default 4) and retries 429s
  with backoff; raising the worker count does not raise throughput.
* JSON output uses the API's `json_object` mode with the contract schema
  embedded in the prompt; occasional unparseable responses are retried and
  otherwise recorded as in-band failures, exactly like other backends.
* Overrides live in `[machine.qwen_api]` (`model_id`, `temperature`,
  `max_tokens`, `max_workers`, `max_attempts`, `max_video_mb`). Changing the
  model or params forks a new `av_` annotation version automatically.

### Checking it worked

Data Management → Enrichment shows the annotator's status, and refuses to
start with the reason when Gemini is not configured. From a shell:

```bash
python -c "from fyp.annotation.machine_annotation import annotation_configured; print(annotation_configured())"
```

`(True, '')` means you're set. Otherwise the message names exactly what to fix.

## Enabling local MiniCPM annotation

A second local backend runs **MiniCPM-o 4.5** (OpenBMB, 9B, Apache 2.0) — the
lighter alternative to the Qwen backend for Macs with less memory. In the
20-video evaluation it ran at ~24 s/video with an 8 GB memory peak (vs ~30 s
and 23 GB for the 30B Qwen model), with agreement against Gemini slightly
below Qwen overall but stronger on some fields (on-screen text, country,
advertising) and weaker on others (face demographics, cultural-trend
detection). It hears the audio track: transcripts were near-verbatim,
including non-English speech.

| Requirement | Minimum | Notes |
|---|---|---|
| Computer | Apple Silicon Mac (M1–M4) | same restriction as the Qwen backend |
| Memory | 16 GB unified memory | 24 GB+ recommended; the model peaks at ~8 GB |
| Disk | ~8 GB free | one-time model download (~6 GB) |
| Tools | `ffmpeg` | frame sampling + audio extraction |

Setup mirrors the [Qwen steps](#enabling-local-qwen-annotation) exactly, with
these substitutions:

```bash
python scripts/setup.py --check-only     # look for the "local minicpm:" rows
pip install -e ".[local_minicpm]"        # same mlx-vlm runtime as local_qwen
hf download mlx-community/MiniCPM-o-4_5-4bit   # ~6 GB, one-time
```

Then set the backend to `minicpm_local` in **Admin → Backends → Machine
annotation** (requirements panel must be green). Everything from the Qwen
section's tips applies unchanged: MiniCPM rows get their own `av_` annotation
version, A/B testing supports a `minicpm_local` arm, annotation is
sequential, and tuning lives in `[machine.minicpm_local]`.

One MiniCPM-specific note: the published MLX conversions of this model fail
to load on stock mlx-vlm 0.6.x ("Missing … parameters" — a checkpoint-naming
mismatch in mlx-vlm's weight sanitizer). The app patches this automatically
at model load (`minicpm_sanitize_fix`); on newer mlx-vlm releases the patch
steps aside. If you see that error anyway, you are likely running the model
outside the app.

## Embedding backends

The Semantic Space pipeline (video embeddings → niche clustering → 2D map)
normally embeds with Gemini. Its API details are explicit in
`config/config.toml` under `[embedding.gemini]` (`model_id`, `dim`,
`location`, `task_type`) — upgrading the Gemini embedding model is a config
edit; the model-scoped store re-embeds under the new model on the next
refresh while keeping the old vectors.

Two alternatives are available from the same Admin → Backends → Embeddings
dropdown:

- **`qwen_api` — hosted Qwen embeddings** (Alibaba Model Studio / DashScope,
  `[embedding.qwen_api]`, default `text-embedding-v4` at 1024 dims). Needs the
  same `DASHSCOPE_API_KEY` environment variable as hosted Qwen annotation
  (see above) and runs fine on Cloud Run. Note the text of each annotated
  video is sent to Alibaba's Singapore region.
- **`qwen_local` — local Qwen embeddings** (below).

## Enabling local embeddings

A local embedding
backend runs a small Qwen3-Embedding model instead, so combined with local
Qwen annotation the whole embeddings + semantic map path makes **no cloud
calls**. Unlike the 30B annotation model this is lightweight: the default
`Qwen/Qwen3-Embedding-0.6B` is ~1.2 GB and runs via sentence-transformers on
Apple Silicon (MPS), NVIDIA GPUs (CUDA), or plain CPU (slower, still fine for
small corpora).

### Step-by-step

```bash
pip install -e ".[local_embeddings]"      # sentence-transformers + torch
hf download Qwen/Qwen3-Embedding-0.6B    # ~1.2 GB, one-time
```

Then in **Admin → Backends → Embeddings**, set the embedding backend to
`qwen_local`. The requirements panel below the dropdown shows what (if
anything) is still missing, with the exact command to fix it.

### Things to know

- **The embedding store is scoped per model.** Every stored vector records
  which model produced it, and all readers filter to the active backend's
  model. Switching backends means the next *Embeddings refresh* re-embeds the
  corpus under the new model; the old model's vectors are kept, so switching
  back costs nothing. Rebuild the video map after the re-embed — the Semantic
  Space tab shows a "built with a different model" banner until you do.
- **Niche naming degrades gracefully.** Naming the ~150 niches is a Gemini
  text call when Gemini is configured; without it, niches get deterministic
  labels built from their most distinctive terms. (Routing naming through the
  local annotation model is a possible future `local_llm` mode.)
- **Cloud Run never runs a local backend.** On a deployed instance the
  embeddings worker refuses to start (and the consolidate pipeline skips the
  embeddings step) while a local backend is selected — run the refresh on the
  host machine, or switch back to Gemini.
- Config lives in `config/config.toml` under `[embedding.qwen_local]`
  (`model_id`, `dim` for Matryoshka truncation, `batch_size`).

### Checking it worked

```bash
python -c "from fyp.analysis.embedding_backends import get_backend, active_backend_name; \
b = get_backend(active_backend_name()); print(b.name, b.model_id(), b.availability())"
```

Then run `python web_interface/run_embeddings_refresh.py --batch-size 50` —
the boot log names the backend and model, and a new
`video_embeddings__*.parquet` shard appears with the model stamped per row.

## First run

```bash
python web_interface/fyp_data_hub.py     # → http://localhost:5002
```

On the first boot with an empty user store, the app creates a default admin
account and prints a **one-time random password** to the console:

```
[AUTH]   username: admin@admin.net
[AUTH]   password: <random>
```

Copy it, log in, and change it under Settings. If you lose it: stop the app,
delete `users/admin@admin.net.json` from your data directory, and start
again — a fresh password is generated. Set `FLASK_DEBUG=1` for the
auto-reloading development server.

## First data

With a fresh install every tab is empty. To load data:

1. Log in as admin → **Data Management** → **Ingestion**, and upload a
   data-donation zip (TikTok/Instagram DDP export, or a Google Takeout for
   YouTube watch history), then run an ingest refresh. The results table
   shows, per file, how many rows were read and kept and — in plain
   language — why any rows were left out; the same report persists in the
   "Ingestion history" panel on that page.
2. On the **Enrichment** panel, queue and run the scraper for the platform
   (`queue_scraper_<platform>`), then **Consolidate & Refresh**.
3. If Gemini is configured, queue annotation the same way.

Once a study is defined and refreshed, its **Methods** button on the Explore
tab shows the auto-generated provenance note (filters, counts, annotation
versions, refresh dates). New non-admin users get a "Getting started" panel
on the Home tab keyed to their permissions, plus the public `/guide` pages.

Workers run as local subprocesses started from the Data Management tab, or
manually:

```bash
python web_interface/run_queue_scraper.py --platform tiktok
python web_interface/run_queue_annotator.py
python web_interface/run_timelines_refresh.py
```

## Verify the install

```bash
bash scripts/verify.sh
```

Runs lint, the checkout-only test subset, schema-hash guards, the golden
annotation safety net (no API cost), and an app import smoke — all designed
to pass on a fresh checkout with no data and no credentials.

## Optional services

**Gemini (annotation, embeddings, semantic map).** Two auth modes:
- *Plain API key*: set `vertexai = false` under `[machine.gemini]` in
  `config.local.toml` (the wizard does this) and export `GEMINI_API_KEY`.
- *Vertex AI*: set `project = "your-gcp-project"` under `[machine.gemini]` and run
  `gcloud auth application-default login`. Annotation stays cleanly disabled
  (with a log message) until one of these is configured.

**Google Cloud Storage.** Optional — local disk is the default and fully
supported. If you are new to Google Cloud, the one-time setup is:

1. Create a GCP project and enable billing
   ([guide](https://cloud.google.com/resource-manager/docs/creating-managing-projects)).
2. Create a Cloud Storage bucket, ideally in a region near you
   ([guide](https://cloud.google.com/storage/docs/creating-buckets)). FYP
   stores everything under `data/` and `media/` prefixes inside one bucket.
3. Install the `gcloud` CLI ([guide](https://cloud.google.com/sdk/docs/install))
   and run `gcloud auth application-default login` — this creates the
   Application Default Credentials (ADC) the app picks up automatically;
   there are no key files or FYP-specific credential settings. Your account
   needs the *Storage Object Admin* role on the bucket
   ([IAM guide](https://cloud.google.com/storage/docs/access-control/iam-roles)).
4. In `config.local.toml`, set the three `use_gcs_for_*` toggles under
   `[data_io]` to `true` and provide the bucket via `FYP_GCS_BUCKET_NAME`
   (or `GCS_bucket_name` in the overlay). The setup wizard can write this.

If the connection fails (no ADC, no access), the app logs the error and
falls back to local storage rather than crashing. Nothing else in GCP is
required for local use — Cloud Run/Cloud Tasks only matter for the
production deployment described in `CLAUDE.md`.

**AIO donation fetch (AWS).** The Data Management "AIO fetch" action pulls
donations from the [Australian Internet Observatory](https://internetobservatory.org.au/)
AWS stack used by the original research project — the DynamoDB table and S3
bucket names are that stack's; it is not useful for other installations
(upload donation zips through the UI instead). If you do have access,
credentials come from the standard boto3 chain (`~/.aws/credentials`
locally, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_DEFAULT_REGION`
env vars otherwise). Without credentials the AIO card simply does not
appear on the Ingestion page; the rest of the app is unaffected.

## PATH and environment variables

Two things trip people up on a first install:

**PATH** is the list of directories your shell searches for commands. If
`python3.12`, `ffmpeg`, or `gcloud` is installed but the terminal says
"command not found", it isn't on your PATH. Quick recipe:

- Check with `which ffmpeg` (macOS/Linux) or `where ffmpeg` (Windows).
- Homebrew and most installers update PATH for **new** terminals — open a
  fresh terminal window first.
- To add a directory permanently, append a line like
  `export PATH="$PATH:/opt/homebrew/bin"` to `~/.zshrc` (macOS) or
  `~/.bashrc` (Linux), then open a new terminal. On Windows use
  *Settings → System → About → Advanced system settings → Environment
  Variables*.
- Tools installed by pip into the virtualenv (like `yt-dlp`) are only on
  PATH while the venv is active (`source .venv/bin/activate`).

The wizard's environment check (`python scripts/setup.py --check-only`)
tells you which tools it can and cannot find.

**Environment variables** (`GEMINI_API_KEY`, `FYP_GCS_BUCKET_NAME`, ...) are
read by the app *at startup* from the shell that launches it. Three ways to
set them:

- One-off, current terminal only: `export GEMINI_API_KEY="..."` before
  starting the app (`set` on Windows cmd, `$env:GEMINI_API_KEY="..."` in
  PowerShell).
- Permanently: put the `export` line in `~/.zshrc` / `~/.bashrc`.
- Via a `.env` file: the wizard can write one, but note the app does
  **not** auto-load it — run `set -a; source .env; set +a` in the terminal
  before starting the app.

After changing an environment variable, restart the app (and use a terminal
where the variable is actually set — check with `echo $GEMINI_API_KEY`).

**Slack feed on the Home tab.** Set `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID`.

**Outbound email (welcome/approval mails).** Set the sender address —
`[site] mail_sender` in `config.local.toml`, or the `FYP_MAIL_SENDER` env
var — plus `MAIL_PASSWORD` (its Gmail app password); optionally
`[site] app_url` / `FYP_APP_URL` so the emails can link to your instance.
Without sender + password, mail silently no-ops.

**Public contact email.** Set `[site] contact_email` in `config.local.toml`
(the wizard asks for it) or the `FYP_CONTACT_EMAIL` env var to show a
contact address on the public guide/FAQ pages and the home-tab feedback
note; those passages are hidden when it is unset. For all three `[site]`
values the env var overrides the config file.

All environment variables are listed in [.env.example](../.env.example);
production/Cloud Run deployment is covered in `CLAUDE.md` and
[architecture.md](architecture.md).
