# The For You Data Hub

[![CI](https://github.com/pwikstrom/foryou-research/actions/workflows/ci.yml/badge.svg)](https://github.com/pwikstrom/foryou-research/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.21994399-007ec6.svg)](https://doi.org/10.5281/zenodo.21994399)

Hyper-personalised short-video feeds have become one of the main ways people
encounter culture, news, and each other. The recommender systems behind them
decide what large audiences see each day, and their influence now reaches well
into society, culture, and commerce.

That influence is hard to study from the outside. What is actually in a given
person's feed? How does it drift over the weeks they spend with it? Do two
people who share an interest end up seeing much the same thing, or something
quite different?

The For You Data Hub helps researchers answer questions like these by examining
feeds as the people using them actually experienced them — on TikTok, Instagram
Reels, and YouTube Shorts. It works from data donations: participants request
their own data export from a platform, look through it, and decide whether to
share it. Consent means something here, because the record belongs to them
first, and what researchers receive in return is real viewing history rather
than a simulation of one.

From there the Hub carries a donation through the whole pipeline. It reads
heterogeneous exports into a single activity table, enriches every watched item
with its metadata and media, annotates content with multimodal AI models, and
opens the result to analysis — both cross-sectional, comparing participants and
groups, and temporal, following how one person's feed shifts day by day.
Findings can be explored and shared with collaborators through the dashboard,
which covers data exploration, per-video analysis, correlations, timelines, a
**Semantic Space** map of video embeddings, and a **Sessions** explorer for
binge episodes and low-entropy feed sequences.

Long-form YouTube watches in the same donated history are ingested and keep
their metadata, but exceed the media duration cap and are therefore not
annotated.

Transparency is built in: ingestion produces a per-file intake report
(rows read, rows kept, plain-language drop reasons), and every study carries
an auto-generated **methods/provenance note** — filters, sample sizes, and
the exact annotation/contract versions behind the data — surfaced in the
dashboard and exportable as JSON.

Built for academic research on feed personalization; the core is
platform-agnostic (adding a platform is one ingestion class and one scraper
class — see [docs/pipeline.md](docs/pipeline.md)).

## Repository layout

| Path | What it is |
|---|---|
| `fyp/` | Core Python package: ingestion, scraping, annotation, recoding, analysis |
| `web_interface/` | Flask app (dashboard + API) and background worker scripts (`run_*.py`) |
| `config/` | `config.toml` plus four declarative TOML contracts that own the variable schemas |
| `tests/` | `unit/` (pytest), `golden/` (cost-free annotation regression suite), ad-hoc scripts |
| `scripts/` | Maintenance/migration scripts + `verify.sh` (the verification gate) |
| `docs/` | Human-oriented documentation (architecture, configuration, web layer, pipeline) |

`DEVELOPING.md` is the maintainer guide: environment, coding style, module
layout, key patterns, and deployment. It is the most detailed single
reference in the repository and the best starting point for contributors.

## Quickstart (local development)

Prerequisites: Python 3.12 (matches the production runtime — see
[docs/python-versions.md](docs/python-versions.md)), plus `ffmpeg` and
`node`/`deno` if you run scrapers.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime pins + pytest/ruff/pre-commit
pip install -e .                       # recommended: editable install of the fyp package

python scripts/setup.py                # interactive setup wizard → config/config.local.toml

python web_interface/fyp_data_hub.py    # → http://localhost:5002
```

The first boot prints a one-time random password for the default
`admin@admin.net` account — copy it from the console and change it after
logging in. Data storage defaults to `~/fyp_local` on the local disk; the
wizard can point it elsewhere or enable GCS/Gemini. (Manual alternative to
the wizard: `cp config/config.local.toml.example config/config.local.toml`
and edit it.) The full walkthrough — prerequisites per platform, optional
services, first data upload — is in
[docs/installation.md](docs/installation.md). Environment variables (Gemini
key, GCS bucket, ...) are documented in `.env.example`. Installed without
Gemini and want annotation later? See
[Enabling Gemini later](docs/installation.md#enabling-gemini-later) — no
reinstall needed and your data is untouched.

The editable install is recommended but never required — the app also runs
from a plain checkout (cwd imports and the workers' `sys.path` bootstrap keep
working, and the Docker image installs nothing from `pyproject.toml`). Note
that reusing `fyp` in *another* project requires a config file: either a
project root containing `__proj__.py` and `config/config.toml`, or the
`FYP_CONFIG_PATH` environment variable pointing at a config TOML directly.
Configuration loads lazily, on first use rather than at import.

Background workers run as plain subprocesses locally (started from the web
UI's Data Management tab, or manually):

```bash
python web_interface/run_queue_annotator.py
python web_interface/run_queue_scraper.py --platform tiktok
```

## Verification

Every change should pass the gate before merging:

```bash
source .venv/bin/activate
bash scripts/verify.sh
```

It runs ruff, the checkout-only unit-test subset, the var-schema hash guard,
the golden annotation safety net (replays saved Gemini responses — no API
cost), and an app import smoke test. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the details and the test markers.

## Deployment

Production runs on Google Cloud Run as two services sharing one Docker
image: `fyp-data-hub` (web) and `fyp-task-runner` (background Cloud Tasks).
Storage is Google Cloud Storage; locally it is the filesystem — both behind
the same `fyp/data_io.py` abstraction. Build/deploy commands and the
base-image/app-image split are documented in `DEVELOPING.md` §"Running the
Project" and [docs/architecture.md](docs/architecture.md).

## Documentation

- [docs/installation.md](docs/installation.md) — installing from scratch: prerequisites, setup wizard, first run
- [docs/architecture.md](docs/architecture.md) — system overview, key design patterns
- [docs/configuration.md](docs/configuration.md) — config.toml sections, contracts, environment variables
- [docs/pipeline.md](docs/pipeline.md) — ingestion → scrape → annotation → recode → analysis
- [docs/web_interface.md](docs/web_interface.md) — Flask app structure, auth, workers, route inventory
- [docs/correlations-tab-guide.md](docs/correlations-tab-guide.md) — the Correlations tab: statistics, views, interpretation
- [docs/python-versions.md](docs/python-versions.md) — Python 3.12 everywhere (dev and prod)
- [CONTRIBUTING.md](CONTRIBUTING.md) — workflow, coding style, invariants you must not break
- [CHANGELOG.md](CHANGELOG.md) — release history

## License & citation

MIT — see [LICENSE](LICENSE). If you use The For You Data Hub in your
research, please cite it using the metadata in
[CITATION.cff](CITATION.cff).

## AI assistance

The For You Data Hub was developed with substantial assistance from Anthropic's Claude
models. Claude Code was used for code generation based on Wikstrom's designs as well as for 
refactoring, test scaffolding, and drafting of documentation. Wikstrom framed
the research problems, designed the architecture and its central abstractions — the
versioned contract system, the backend interfaces, the validation harnesses —
and reviewed, tested and accepted every change. Responsibility for the
correctness, originality and licensing of the code rests with Patrik Wikstrom.

Distinct from that, large language models are also *runtime components* of the
Hub: content annotation and text embedding are performed by Gemini or by
open-weight Qwen and MiniCPM models, depending on configuration. That is a
function of the software rather than an authoring aid, and the A/B evaluation
and human-coding harnesses described in
[docs/pipeline.md](docs/pipeline.md) exist precisely to make those model
outputs auditable rather than taken on trust.
