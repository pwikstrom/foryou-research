#!/usr/bin/env bash
# Standard verification gate for For You Data Hub changes.
#
# Run from the project root, inside the dev venv:
#     source .venv/bin/activate && bash scripts/verify.sh
#
# Every refactoring / cleanup PR must pass this gate. It is intentionally
# cost-free: no Gemini calls, no GCS writes, no production data needed beyond
# the committed fixtures and config.
#
# Steps:
#   1. ruff       — lint + format check (same bar as pre-commit / CI)
#   2. pytest     — unit tests, excluding data/GCS-dependent and stale tests
#                   (includes the import-cycle / var-schema-hash guard, the
#                   routes.md freshness check and the version-consistency check)
#   3. golden net — replay saved Gemini responses through the annotation
#                   pipeline (tests/golden/README.md)
#   4. boot smoke — the Flask app (and therefore the whole import graph)
#                   must import cleanly

set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "ruff (lint + format)"
# Same bar as pre-commit and CI: the enforced rule set is pyproject.toml
# [tool.ruff.lint] (its comments list the next families to enable).
ruff check .
ruff format --check .

step "unit tests (checkout-only subset)"
python -m pytest -m "not requires_data and not requires_gcs and not slow and not stale"

step "golden annotation safety net"
python tests/golden/run_safety_net.py

step "app import smoke"
python -c "import web_interface.fyp_data_hub; print('app import OK')"

printf '\n\033[1;32mAll verification steps passed.\033[0m\n'
