# Public-release checklist

Everything that must happen between "the JOSS draft is ready" and "the
repository is public and submitted". Work through it in order; items marked
**BLOCKER** must be done before flipping the repo public.

## 1. Secrets & history audit — **BLOCKER**

- [ ] Run a secret scanner over the *full history*, not just the tree:
      `gitleaks detect --source .` (or `trufflehog git file://.`).
- [ ] Known issue to resolve: a `GEMINI_API_KEY` value was once committed in
      `launch.json` and lives in git history. **Rotate that key** regardless
      of the history decision below.
- [ ] Decide: **rewrite history** (`git filter-repo` to purge leaked blobs)
      vs **publish a fresh-history mirror** (new public repo, single initial
      commit of the sanitized tree). A fresh mirror is the low-risk default;
      the trade-off is losing the public commit history that evidences
      development effort for JOSS. If the scanner comes back clean after key
      rotation, keeping history is fine.

## 2. Tree sanitization — **BLOCKER**

- [ ] `config/config.toml` and `config/*.toml`: no real bucket names,
      GCP project ids, personal paths, or study-specific values that
      shouldn't be public (move to `config.local.toml` / env).
- [ ] `CLAUDE.md`, `docs/`, `DEVELOPMENT_ROADMAP.md`: review for internal
      infrastructure specifics (project ids, service-account emails,
      deployment URLs) and either genericize or accept as public.
- [ ] `cloudbuild*.yaml`, `Dockerfile*`, `.github/workflows/`: same review.
- [ ] Grep tree *and* history for participant references: donor names,
      real usernames, donation filenames, study participant ids —
      including `tests/` fixtures and any `tmp/`/sandbox stragglers.
- [ ] `web_interface/mail_utils.py` / admin settings: no hardcoded personal
      email addresses.
- [ ] Confirm `patrik_secrets/`, `.env`, cookies files etc. are untracked
      and gitignored.

## 3. Identity & metadata placeholders

- [x] Decide the final public name. The software is **"The For You Data
      Hub"** ("the Hub" on later mention); the abbreviation "FYP" is retired
      from prose because it is heavily overloaded ("For You Page", "Final
      Year Project"). Note this is distinct from **The For You Project**,
      the research project within which the Hub is developed — that name
      still refers to the project, never to the software. Docs, the paper
      and CITATION.cff now follow this; code identifiers (the `fyp`
      package, `FYP_*` environment variables, the `fyp-*` Cloud Run
      services and images) deliberately keep the old short form.
- [ ] Fill in the real ORCID iD in `CITATION.cff` and `paper/paper.md`.
- [ ] Replace `<REPO-URL>` placeholders in `CITATION.cff` and
      `pyproject.toml` `[project.urls]` with the public URL.
- [ ] Fill in the ethics-approval number in
      `docs/ethics_and_data_handling.md` and the Acknowledgements
      (funding, approval) in `paper/paper.md`.
- [ ] Check PyPI for the `fyp-pipeline` name if PyPI publication is ever
      intended (not required by JOSS).

## 4. Reviewer demo path (JOSS-review critical)

JOSS reviewers must install the software and exercise it without an LLM
API key, GCS access, or real donation data.

- [x] Synthetic sample dataset — **done, and better than the `examples/`
      snapshot originally planned**: `fyp/ingest/demo_dataset.py` generates
      donor DDP exports, a canonical scrape batch, and raw machine-annotation
      entries from a seeded content model, installable either with
      `python scripts/generate_demo_dataset.py --write` or one-click from
      Data Management → Ingestion. Nothing is checked into the tree, so the
      repo still ships zero data.
- [ ] Write `docs/demo.md`: install (`scripts/setup.py` wizard) → generate
      and install the demo dataset → Ingest refresh → Consolidate & Refresh
      → create the demo study → open the dashboard → run `scripts/verify.sh`.
      Document how to create the first admin login. State which features
      need an API key — noting that annotation and embeddings can also run
      on local open-weight backends (docs/installation.md), so the only
      hard external dependency left for a reviewer is scraping.

## 5. Publish & submit

- [ ] Flip the repo public (or push the mirror). Confirm CI runs green
      publicly and the issue tracker is enabled.
- [ ] Verify `CONTRIBUTING.md` covers all three JOSS community items:
      how to contribute, how to report issues, where to seek support.
- [ ] Enable the GitHub↔Zenodo integration, then tag a release
      (e.g. `v1.0.0`; bump `pyproject.toml` version to match). Check the
      Zenodo record's title/author/ORCID/license match the paper exactly.
- [ ] Build the paper locally to check rendering:
      `docker run --rm -v $PWD/paper:/data openjournals/inara -o pdf paper.md`
- [ ] Submit at <https://joss.theoj.org/papers/new> (repo URL + branch
      containing `paper/`).
- [ ] Expect: editor assignment (1–4 weeks), then an open checklist review
      by ≥2 reviewers on GitHub (2–5 months wall-clock). Review requests
      arrive as issues/PRs — responsiveness matters with a single
      maintainer.
- [ ] On acceptance: tag the final release, archive on Zenodo, give the
      editor the version + archive DOI, then uncomment and complete the
      `preferred-citation` block in `CITATION.cff` and add the JOSS badge
      to `README.md`.
