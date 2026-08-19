# Contributing to The For You Data Hub

The For You Data Hub is an academic research tool, and contributions —
bug reports, fixes, documentation, and new platform support — are welcome.
Start below if you have a problem to report or a question to ask; the
**Workflow** section onwards covers contributing code.

## Reporting a bug or requesting a feature

Open a [GitHub issue](https://github.com/pwikstrom/foryou-research/issues).
A useful bug report includes:

- what you did, what you expected, and what happened instead;
- the version or commit you are on, your OS, and your Python version;
- whether you are running locally or on a hosted deployment;
- the relevant traceback or log lines, with any participant data removed.

Please **do not** attach donation exports, activity data, or anything else
that identifies a participant. A structural description or a synthetic
reproduction is enough — `scripts/generate_demo_dataset.py` produces
shareable synthetic data for exactly this purpose.

Security vulnerabilities are the exception: report those privately, as
described in [SECURITY.md](SECURITY.md), not as public issues.

## Getting support

Questions about installing or using the Hub are welcome as
[GitHub issues](https://github.com/pwikstrom/foryou-research/issues) —
please label them `question`. There is no separate support channel; the
issue tracker is the place for everything that is not a security report.

The project is maintained by a small academic team, so please allow a few
working days for a reply.

## Workflow

1. Branch off `main`; keep branches short-lived and focused.
2. Install dev tools (`pip install -r requirements-dev.txt`) and the
   pre-commit hook once: `pre-commit install`.
   It currently gates only pyflakes-level errors (`ruff --select=F`) while
   pre-existing style debt is worked down; the full ruff rule set in
   `pyproject.toml` is the target bar for new code.
3. Before opening a PR, run the verification gate:

   ```bash
   source .venv/bin/activate
   bash scripts/verify.sh
   ```

4. PRs should be independently deployable — production deploys straight from
   `main` (Cloud Run, see `DEVELOPING.md`).

## Tests

- `pytest` runs `tests/unit/` (configured in `pyproject.toml`). Markers:
  - `requires_data` — needs local/production data files not in a fresh checkout
  - `requires_gcs` — needs live GCS/GCP credentials
  - `slow` — long-running
  - `stale` — known-broken against current contracts/data shapes (a to-fix
    list, not a regression signal)
  The checkout-only gate is `pytest -m "not requires_data and not requires_gcs and not slow and not stale"`.
- Nine legacy test files cannot currently be collected at all and are listed
  (with reasons) in `tests/unit/conftest.py::collect_ignore` — converting one
  to proper pytest style and removing its entry is a welcome contribution.
- `tests/golden/` is the annotation safety net: it replays saved raw Gemini
  responses through the full pipeline, so it is free and offline. Run it with
  `python tests/golden/run_safety_net.py`. If you touch annotation code, this
  is your regression suite (see `tests/golden/README.md`).
- New tests go in `tests/unit/`. Throwaway debug scripts belong in
  `tests/debug/` and one-off maintenance scripts in `scripts/adhoc/`; both
  are gitignored, because such code tends to accumulate real collection ids,
  bucket names and donation filenames.

## Coding style

The authoritative style rules live in `DEVELOPING.md` §"Coding Style". Highlights:

- Python type hints in signatures; Google-style docstrings; imports at the
  top of the file; f-strings; PyArrow dtypes for DataFrames.
- Frontend: never hardcode colors/fonts/sizes — use the CSS custom-property
  token system in `web_interface/static/style.css` (semantic tokens, type
  scale, weight utilities). Both dark and light themes must be covered.
- All file access goes through `fyp/core/data_io.py` named locations — never
  raw paths — so code works unchanged against local disk and GCS.
- Cite and import modules by their canonical subpackage path
  (`fyp.scrape.platform_scraper`, not the flat back-compat shim
  `fyp.platform_scraper`).

## Invariants you must not break

These are load-bearing conventions; each has a guard, but know them up front:

1. **The config import cycle.** `fyp/core/fyp_config.py` runs `initialize()` +
   `load_var_schema()` at module import. Modules that the load-time contract
   overlays call into (`data_io`, the three `*_versioning` modules,
   `var_presentation`) must NOT import `fyp_cf` (or `fyp.data_io`) at module
   level — they use function-level `_cf()` / `_data_io()` accessors. Guard:
   `tests/unit/test_import_cycle_hash.py`.
2. **The worker stdout contract.** In subprocess mode,
   `web_interface/process_manager.py` parses worker stdout line-by-line for
   `::PROGRESS::` / `::DATA::` markers emitted by `LocalStatusReporter`
   (`web_interface/task_status.py`); every other stdout line becomes a UI log
   line. Do not redirect worker output to stderr or alter those marker lines.
3. **The var-schema hash.** Study caches key on a hash of the synthesized
   variable schema. Metadata-only changes (display names, descriptions,
   surface checkboxes) must never change the hash; genuine schema changes
   must. If your change bumps the hash, every study re-recodes on the next
   refresh — do it knowingly.
4. **Contracts own their schemas.** The four TOML contracts in `config/`
   (annotation, scrape, activity, derived) are the single declarative source
   for variable metadata; `var_schema` is synthesized from them at config
   load. Don't hand-edit synthesized outputs.
5. **Cross-service stats.** Both Cloud Run services share
   `process_stats.json` on GCS — always call `load_process_stats()` before
   reading or writing so you don't clobber the other service's data.
6. **No flat shims inside thread pools.** Every flat `fyp/<name>.py` module
   is a back-compat alias for its subpackage home. Code that runs inside a
   thread-pool worker body must import the canonical
   `fyp.<subpackage>.<module>` path — two cold shims imported concurrently
   in one worker can deadlock CPython's per-module import lock. Guard:
   `tests/unit/test_pool_import_race.py`. The module-placement rules behind
   the layout are in [docs/fyp-import-graph.md](docs/fyp-import-graph.md).

## Adding a platform

The codebase is designed so a new platform is two classes and a contract
block — no orchestration edits. The complete checklist (including the
supporting steps that are easy to miss) is in
[docs/extending.md](docs/extending.md); see also the docstrings on
`ForYouBaseCollection` (`fyp/ingest/base.py`) and `BaseScraper`
(`fyp/scrape/platform_scraper.py`).
