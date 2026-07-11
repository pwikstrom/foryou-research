"""Regression test: the schema hash must be import-order independent.

Root cause pinned 2026-07-02: importing ``fyp.annotation_versioning`` before
``fyp.fyp_config`` (as the gunicorn web app's import graph does) triggered
fyp_config's module-level ``load_var_schema`` while annotation_versioning was
still partially initialized — the legacy-metadata overlay's
``av.union_field_metadata()`` raised AttributeError, was silently swallowed,
and boot frames lost all legacy field metadata. The schema hash then drifted
per-instance (prod computed ``v2:907cc58f...`` while every fresh single-module
load computed ``v2:0dbc279c...``), and legacy fields were recoded with blank
role/scale. Fixed by making fyp.data_io / fyp_config imports lazy inside the
three versioning modules.

Each ordering must run in its own interpreter, so this test shells out.

Usage:
    python tests/unit/test_import_cycle_hash.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PROBE = (
    "import fyp.recode_variables as rv;"
    "from fyp.fyp_config import fyp_cf;"
    "vs = fyp_cf['var_schema'];"
    "row = vs[vs['variable_name']=='trend'];"
    "role = str(row['role'].iloc[0]) if len(row) else 'MISSING';"
    "print('ROLE=' + role);"
    "print('HASH=' + rv.compute_var_schema_hash())"
)


def _run(prelude: str) -> dict:
    out = subprocess.run(
        [sys.executable, "-c", prelude + PROBE],
        capture_output=True, text=True, cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
    )
    assert out.returncode == 0, out.stderr[-2000:]
    values = dict(
        line.split("=", 1) for line in out.stdout.splitlines() if "=" in line and
        (line.startswith("ROLE") or line.startswith("HASH"))
    )
    return values


def test_hash_is_import_order_independent() -> None:
    baseline = _run("")  # fyp_config first (the "clean" order)
    orders = {
        "annotation_versioning_first": "import fyp.annotation_versioning;",
        "scrape_versioning_first": "import fyp.scrape_versioning;",
        "activity_versioning_first": "import fyp.activity_versioning;",
        "machine_annotation_first": "import fyp.machine_annotation;",
    }
    for name, prelude in orders.items():
        got = _run(prelude)
        assert got["HASH"] == baseline["HASH"], (
            f"{name}: hash {got['HASH'][:16]} != baseline {baseline['HASH'][:16]} — "
            "import-order-dependent schema state (legacy overlay lost?)"
        )
        assert got["ROLE"] == baseline["ROLE"], (
            f"{name}: trend role {got['ROLE']!r} != baseline {baseline['ROLE']!r}"
        )
    # The legacy field 'trend' only exists when the annotation version
    # registry is on disk (live data). On a fresh checkout / CI the hash
    # order-independence above is the whole guard.
    if baseline["ROLE"] == "MISSING":
        import os

        from fyp.fyp_config import fyp_cf

        registry = os.path.join(fyp_cf["paths"]["recoded"], "annotation_versions.json")
        assert not os.path.exists(registry), (
            "trend role is MISSING although the annotation version registry exists — "
            "the legacy overlay silently dropped registry metadata"
        )


def _main() -> int:
    try:
        test_hash_is_import_order_independent()
        print("PASS  test_hash_is_import_order_independent")
        print("\n1/1 passed")
        return 0
    except AssertionError as exc:
        print(f"FAIL  test_hash_is_import_order_independent: {exc}")
        print("\n0/1 passed")
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
