"""Freeze the current refinement output as the golden snapshot.

Runs the live ``refine_one_raw_annotation_batch`` over the frozen fixture and
writes three artifacts to ``fixtures/``:

  * ``golden_refined.parquet``     — the expected refined dataframe
  * ``var_schema.snapshot.csv``    — the schema the golden was built against
  * ``manifest.json``              — provenance (counts, schema hash, models)

Run this once to establish the baseline, and again ONLY when a change to the
refinement pipeline is *intended* — at which point you review the diff that
``test_refinement_golden.py`` reports and, if correct, re-bless it here.

Usage:
    python tests/golden/build_golden.py
"""

from __future__ import annotations

import json
import shutil

import pandas as pd
from _harness import (
    FIXTURE_PATH,
    GOLDEN_PARQUET,
    MANIFEST_PATH,
    SCHEMA_SNAPSHOT,
    fyp_cf,
    load_fixture,
    normalize_frame,
    run_current_refinement,
)


def _snapshot_schema() -> str:
    """Copy the live var_schema.csv next to the golden output; return its hash."""
    from fyp.recode_variables import compute_var_schema_hash

    live = fyp_cf["var_schema"]
    SCHEMA_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    live.to_csv(SCHEMA_SNAPSHOT, index=False)
    return compute_var_schema_hash()


def build() -> None:
    if not FIXTURE_PATH.exists():
        raise SystemExit(
            f"Fixture missing: {FIXTURE_PATH}\nRun: python tests/golden/build_fixture.py"
        )

    schema_hash = _snapshot_schema()
    raw = load_fixture()
    print(f"Refining {len(raw)} fixture responses against pinned schema {schema_hash} ...")

    df = run_current_refinement(raw, quiet=True)
    if df is None or df.empty:
        raise SystemExit("Refinement returned no rows — cannot build a golden snapshot.")

    # Persist the normalized (all-string) representation: it is the comparison
    # form and, unlike the raw refined frame, survives a pandas parquet
    # round-trip (nested pyarrow list dtypes do not).
    normalize_frame(df).reset_index().to_parquet(GOLDEN_PARQUET, index=False)

    models = sorted({str(v.get("model")) for v in raw.values()})
    prompts = sorted({str(v.get("prompt_fn")) for v in raw.values()})
    manifest = {
        "n_raw_responses": len(raw),
        "n_refined_rows": int(df.shape[0]),
        "n_refined_columns": int(df.shape[1]),
        "refined_columns": sorted(map(str, df.columns)),
        "var_schema_hash": schema_hash,
        "var_schema_shape": list(fyp_cf["var_schema"].shape),
        "models": models,
        "prompt_files": prompts,
        "note": (
            "Golden snapshot of the current refinement pipeline. Rebuild with "
            "tests/golden/build_golden.py ONLY when a pipeline change is intended."
        ),
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"  golden parquet : {GOLDEN_PARQUET}  shape={df.shape}")
    print(f"  schema snapshot: {SCHEMA_SNAPSHOT}")
    print(f"  manifest       : {MANIFEST_PATH}")
    print(f"  models={models} prompts={prompts}")


if __name__ == "__main__":
    build()
