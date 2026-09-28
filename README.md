# The For You Data Hub

[![CI](https://github.com/pwikstrom/foryou-research/actions/workflows/ci.yml/badge.svg)](https://github.com/pwikstrom/foryou-research/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.21994399-007ec6.svg)](https://doi.org/10.5281/zenodo.21994399)

Hyper-personalised short-video feeds have become one of the main ways people
encounter culture, news, and each other. The recommender systems behind them
decide what large audiences see each day, and their influence now reaches deep
into society, culture, and commerce.

That influence is hard to study from the outside. What is actually in a given
person's feed? How does it change over the weeks they spend with it? Do two
people who share an interest end up seeing much the same thing, or something
quite different?

The For You Data Hub helps researchers answer questions like these by examining
feeds just as the people using them are experiencing them — on TikTok, Instagram
Reels, and YouTube Shorts. The data used in the Hub are donated by real platform
users. Participants request their own data export from the platform, then review and prune it in the browser before anything is uploaded — nothing leaves their machine during that review. On donating they get an instant preview of their own "short-video persona", and they can later withdraw their donation themselves, with a 30-day restore window. Read more about ethical considerations in [docs/ethics_and_data_handling.md](docs/ethics_and_data_handling.md).

From there, the Hub carries a donation through the whole pipeline. It reads
datasets from different platforms into a single activity table, enriches every watched item
with its metadata and media, annotates content with multimodal AI models, and
opens the result to analysis — both cross-sectional, comparing participants and
groups, and temporal, following how one person's feed shifts day by day or video by video. Read more about the pipeline in [docs/pipeline.md](docs/pipeline.md).

The For You Data Hub's User Guide is available here [docs/user-guide.md](docs/user-guide.md), where you can read about the different analyses the Hub allows.

The Hub also serves the participants themselves. Recruitment runs through a
guided /participate funnel, donations are uploaded self-serve with the
browser-side review step above (My Collections), a donation can be withdrawn
by its owner with a 30-day restore window, and every donor gets a pair of
auto-managed personal studies — "Just Me" and "Everyone & Me" — so they can
explore their own feed alongside the wider corpus.

The For You Data Hub is built for academic researchers with high expectations for transparency. Ingestion produces a per-file intake report
(rows read, rows kept, plain-language drop reasons), and every study carries
an auto-generated methods/provenance note — filters, sample sizes, and
the exact annotation/contract versions behind the data — surfaced in the
dashboard and exportable as JSON. AI annotation is driven by contracts to maximise replicability and transparency. Researchers can experiment with different models and prompts and run evaluations with human input to ensure coding reliability and validity.

The Hub currently supports the three largest short-video platforms, but it is essentially platform-agnostic. Researchers can extend the Hub to support new platforms by adding a new ingestion class, scraper class, and a contract block (the complete checklist is in [docs/extending.md](docs/extending.md)).

## Repository layout

| Path | What it is |
|---|---|
| `fyp/` | Core Python package: ingestion, scraping, annotation, recoding, analysis |
| `web_interface/` | Flask app (dashboard + API, plus the public mini-site with its SEO plumbing) and background worker scripts (`run_*.py`) |
| `config/` | `config.toml` plus four declarative TOML contracts that own the variable schemas |
| `tests/` | `unit/` (pytest suite) and `golden/` (cost-free annotation regression suite) — both run by CI |
| `scripts/` | Setup, verification (`verify.sh`), and doc generators |
| `docs/` | Documentation (listed [below](#documentation)), including the [decision log](docs/decisions/README.md) |

[DEVELOPING.md](DEVELOPING.md) is the developer guide — setup, coding style,
tests, the project tree and deployment — and the starting point for
contributors.

## Quickstart

You can take the Hub for a spin straight away by requesting a user account at
<https://www.tinyurl.com/foryoudatahub>. To run your own installation locally
(Python 3.12; `ffmpeg` and `node` or `deno` if you run the scrapers):

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e .
python scripts/setup.py                # setup wizard → config/config.local.toml
python web_interface/fyp_data_hub.py   # → http://localhost:5002
```

The first boot prints a one-time password for `admin@admin.net`. The full
walkthrough — prerequisites, optional services, first data, enabling
annotation — is [docs/installation.md](docs/installation.md).

## Verification

Every change should pass the gate before merging:

```bash
source .venv/bin/activate
bash scripts/verify.sh
```

It runs ruff (lint and format check), the checkout-only unit-test subset
(including the var-schema hash guard), the golden annotation safety net
(replays saved Gemini responses — no API cost), and an app import smoke test. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the details and the test markers.

## Deployment

Production runs on Google Cloud Run as two services sharing one Docker
image: `fyp-data-hub` (web) and `fyp-task-runner` (background Cloud Tasks).
Storage is Google Cloud Storage; locally it is the filesystem — both behind
the same `fyp/core/data_io.py` abstraction. Build/deploy commands and the
base-image/app-image split are documented in
[DEVELOPING.md](DEVELOPING.md#cloud-run-deployment) and
[docs/architecture.md](docs/architecture.md).

## Documentation

- [docs/installation.md](docs/installation.md) — installing from scratch: prerequisites, setup wizard, first run, optional services
- [docs/user-guide.md](docs/user-guide.md) — the web app, tab by tab, for researchers and students
- [docs/correlations-tab-guide.md](docs/correlations-tab-guide.md) — the Correlations tab: statistics, views, interpretation
- [docs/ethics_and_data_handling.md](docs/ethics_and_data_handling.md) — consent, data handling, and the ethics posture of the software
- [docs/architecture.md](docs/architecture.md) — system overview: how the pieces fit, execution modes, key design patterns
- [docs/fyp-import-graph.md](docs/fyp-import-graph.md) — `fyp/` package layout: module placement and import rules
- [docs/configuration.md](docs/configuration.md) — config.toml sections, environment variables, storage locations
- [docs/contracts.md](docs/contracts.md) — the contract system: authoring, validation, versioning, runtime editing
- [docs/pipeline.md](docs/pipeline.md) — ingestion → scrape → annotation → consolidation → analysis, stage by stage
- [docs/web_interface.md](docs/web_interface.md) — Flask app structure, auth, background workers, frontend
- [docs/routes.md](docs/routes.md) — generated HTTP endpoint inventory
- [docs/extending.md](docs/extending.md) — adding a platform, an annotation backend, or an embedding backend
- [docs/decisions/README.md](docs/decisions/README.md) — the decision log: dated records of why the Hub works the way it does
- [DEVELOPING.md](DEVELOPING.md) — the developer guide: setup, coding style, tests, project tree, deployment
- [CONTRIBUTING.md](CONTRIBUTING.md) — workflow, coding style, invariants you must not break
- [SECURITY.md](SECURITY.md) — reporting vulnerabilities
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
