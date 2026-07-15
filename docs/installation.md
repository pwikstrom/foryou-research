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
git clone https://github.com/pwikstrom/fyp_main_v02.git
cd fyp_main_v02
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
   YouTube watch history), then run an ingest refresh.
2. On the **Enrichment** panel, queue and run the scraper for the platform
   (`queue_scraper_<platform>`), then **Consolidate & Refresh**.
3. If Gemini is configured, queue annotation the same way.

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
- *Plain API key*: set `vertexai = false` under `[machine]` in
  `config.local.toml` (the wizard does this) and export `GEMINI_API_KEY`.
- *Vertex AI*: set `project = "your-gcp-project"` under `[machine]` and run
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
