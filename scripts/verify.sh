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
#   1. ruff       — lint (same rule set as pre-commit / CI)
#   2. pytest     — unit tests, excluding data/GCS-dependent and stale tests
#   3. hash guard — the import-cycle / var-schema-hash regression test
#   4. golden net — replay saved Gemini responses through the annotation
#                   pipeline (tests/golden/README.md)
#   5. boot smoke — the Flask app (and therefore the whole import graph)
#                   must import cleanly

set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "ruff check (pyflakes bar)"
# The full pyproject rule set still reports ~850 pre-existing findings (style
# debt worked down separately — see .pre-commit-config.yaml). The enforced bar
# here matches pre-commit: pyflakes, minus three codes with known pre-existing
# hits (F841 unused locals ×12, F601 duplicate dict keys ×2, F403 star import
# ×1 — as of 2026-07). Tighten as the debt is cleared.
ruff check --select=F --ignore=F841,F601,F403 .

step "unit tests (checkout-only subset)"
python -m pytest -m "not requires_data and not requires_gcs and not slow and not stale"

step "import-cycle / schema-hash guard"
python -m pytest tests/unit/test_import_cycle_hash.py

step "golden annotation safety net"
python tests/golden/run_safety_net.py

step "app import smoke"
python -c "import web_interface.fyp_data_hub; print('app import OK')"

printf '\n\033[1;32mAll verification steps passed.\033[0m\n'
