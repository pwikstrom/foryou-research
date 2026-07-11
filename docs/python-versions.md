# Python versions: 3.12 in production, 3.14 for local dev

Two Python versions are in play; this is deliberate but easy to trip over.

- **Production truth is 3.12.** The Docker image is `python:3.12-slim` and
  `requirements312.txt` (the single pinned dependency file, used by
  `Dockerfile.base`) is resolved for 3.12. **All code must stay
  3.12-compatible** — `ruff` enforces `target-version = "py312"`, so don't
  use 3.13+/3.14-only syntax or stdlib features.
- **Local dev uses 3.14** in the `.fypenv314` venv, installed from the same
  `requirements312.txt` (the pins happen to resolve on 3.14 too). This is a
  convenience, not a support statement: if a behavior differs between
  versions, 3.12 wins.
- There is no separate dev requirements file yet; dev-only tools (`pytest`,
  `pre-commit`) are installed ad hoc into the venv. A
  `requirements-dev.txt` is planned alongside packaging work.

If you only want to run the app locally with minimum surprise, a 3.12 venv
built from `requirements312.txt` is the most production-faithful setup.
