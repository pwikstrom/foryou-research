# FYP — For You Project

A research data platform for studying algorithmically curated social-media
feeds. FYP ingests personal feed activity — TikTok data captures and zipped
data-donation exports from TikTok, Instagram, and YouTube — enriches it via
web scraping and LLM annotation (Google Gemini), runs statistical analysis
(PCA, ANOVA, PERMANOVA, sequence and timeline analysis), and presents
findings through a Flask web dashboard with role-based access control.

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
| `prompts/` | Gemini prompt templates |
| `scripts/` | Maintenance/migration scripts + `verify.sh` (the verification gate) |
| `docs/` | Human-oriented documentation (architecture, configuration, web layer, pipeline) |

`AGENT.md` (symlinked as `CLAUDE.md`/`GEMINI.md`) is the AI-agent-oriented
project instruction file; it is the most detailed single reference and worth
reading even as a human.

## Quickstart (local development)

Prerequisites: Python 3.14 for local dev (production runs 3.12 — keep code
3.12-compatible, see [docs/python-versions.md](docs/python-versions.md)),
plus `ffmpeg` and `node`/`deno` if you run scrapers.

```bash
python3.14 -m venv .fypenv314
source .fypenv314/bin/activate
pip install -r requirements312.txt
pip install pytest            # dev-only, not in the runtime pin file

# Configure paths: edit [paths] in config/config.toml to point local_data
# at a writable directory (see docs/configuration.md for every section).

python web_interface/fyp_data_hub.py    # → http://localhost:5002
```

Background workers run as plain subprocesses locally (started from the web
UI's Data Management tab, or manually):

```bash
python web_interface/run_queue_annotator.py
python web_interface/run_queue_scraper.py --platform tiktok
```

## Verification

Every change should pass the gate before merging:

```bash
source .fypenv314/bin/activate
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
base-image/app-image split are documented in `AGENT.md` §"Running the
Project" and [docs/architecture.md](docs/architecture.md).

## Documentation

- [docs/architecture.md](docs/architecture.md) — system overview, key design patterns
- [docs/configuration.md](docs/configuration.md) — config.toml sections, contracts, environment variables
- [docs/pipeline.md](docs/pipeline.md) — ingestion → scrape → annotation → recode → analysis
- [docs/web_interface.md](docs/web_interface.md) — Flask app structure, auth, workers, route inventory
- [docs/python-versions.md](docs/python-versions.md) — why dev is 3.14 and prod is 3.12
- [CONTRIBUTING.md](CONTRIBUTING.md) — workflow, coding style, invariants you must not break

## License

No license has been chosen yet — until one is added, all rights are
reserved; contact the repository owner before reusing any part of the code.
