# Python version: 3.12 everywhere

FYP standardizes on **Python 3.12** for both local development and
production, so what you run locally matches what ships.

- **Production** is `python:3.12-slim` (Cloud Run), with dependencies pinned
  in `requirements312.txt` (used by `Dockerfile.base`).
- **Local dev** uses a 3.12 virtual environment named `.venv`:

  ```bash
  python3.12 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements-dev.txt   # runtime pins + pytest/ruff/pre-commit
  ```

- `ruff` enforces `target-version = "py312"`, and CI runs on 3.12, so the
  local gate (`scripts/verify.sh`) and CI agree.

Keep code 3.12-compatible — don't reach for 3.13+ syntax or stdlib
features. The only environment difference from production is the OS (local
macOS vs. Debian slim), which is not something the project pins.

> The dependency lock is still named `requirements312.txt` for its Python
> version; the name is historical and kept because `Dockerfile.base` and
> `cloudbuild-base.yaml` reference it.
