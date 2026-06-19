"""Shared helpers for the golden-corpus regression harness.

The golden corpus pins the *current* behaviour of the machine-annotation
refinement pipeline (``refine_one_raw_annotation_batch`` and everything it
calls: JSON repair, flattening, rare-column consolidation, transcript
de-duplication, schema-driven recoding and label clean-up) against a frozen
sample of real raw Gemini responses.

Any refactor of the refinement / recode code must reproduce the golden output
exactly, or surface a reviewed diff.  This protects the most fragile and least
tested part of the codebase (the fuzzy JSON repair + recode engine) with zero
API cost, because it runs entirely on saved raw responses.

Determinism notes:
  * Refinement is deterministic: ``clean_up_machine_annotations`` now samples
    with a fixed ``random_state`` (machine_annotation.py), so output is
    reproducible run-to-run regardless of global RNG state.  ``test_refinement_
    is_deterministic`` validates this directly (no test-side seeding crutch).
  * Refinement reads ``fyp_cf['var_schema']``.  The harness pins a frozen
    snapshot of the schema so edits to the live ``var_schema.csv`` never
    invalidate the golden output.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_THIS = Path(__file__).resolve()
PROJECT_ROOT = _THIS.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from fyp.fyp_config import fyp_cf

GOLDEN_DIR = _THIS.parent
FIXTURE_DIR = GOLDEN_DIR / "fixtures"
FIXTURE_PATH = FIXTURE_DIR / "raw_sample.json"
GOLDEN_PARQUET = FIXTURE_DIR / "golden_refined.parquet"
SCHEMA_SNAPSHOT = FIXTURE_DIR / "var_schema.snapshot.csv"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"


@contextlib.contextmanager
def pinned_var_schema(schema_csv: Path = SCHEMA_SNAPSHOT):
    """Temporarily swap in a frozen ``var_schema`` for deterministic output.

    Falls back to the live schema (no swap) if the snapshot is missing, so the
    builder can run before a snapshot exists.
    """
    from fyp.fyp_config import _apply_contract_accepted_labels

    original = fyp_cf.get("var_schema")
    try:
        if Path(schema_csv).exists():
            fyp_cf["var_schema"] = pd.read_csv(
                schema_csv, dtype_backend="pyarrow", encoding="utf-8"
            )
            # accepted_labels is contract-owned and not stored in the CSV/snapshot;
            # rebuild it in memory exactly as the live load path does.
            _apply_contract_accepted_labels(fyp_cf)
        yield fyp_cf.get("var_schema")
    finally:
        fyp_cf["var_schema"] = original


@contextlib.contextmanager
def isolated_storage():
    """Redirect machine-annotation storage paths to a throwaway temp dir.

    ``refine_one_raw_annotation_batch`` writes the refined parquet to the
    ``machine_annotations_refined`` location as a side effect; this keeps that
    write out of the real data tree.
    """
    keys = [
        "machine_annotations",
        "machine_annotations_raw",
        "machine_annotations_refined",
        "temp",
    ]
    original = {k: fyp_cf["paths"].get(k) for k in keys}
    tmp = tempfile.mkdtemp(prefix="fyp_golden_")
    try:
        for k in keys:
            p = os.path.join(tmp, k)
            os.makedirs(p, exist_ok=True)
            fyp_cf["paths"][k] = p
        yield tmp
    finally:
        for k, v in original.items():
            fyp_cf["paths"][k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def run_current_refinement(raw_outputs: dict, quiet: bool = True) -> pd.DataFrame:
    """Run the live refinement pipeline on a raw-response dict, deterministically.

    Args:
        raw_outputs: ``{index: {item_id, response, finish_reason, ...}}`` shaped
            exactly like a ``machine_annotations_raw`` file.
        quiet: suppress the pipeline's verbose stdout (dots / progress).

    Returns:
        The refined dataframe (also written to an isolated temp parquet).
    """
    from fyp.machine_annotation import refine_one_raw_annotation_batch

    sink = io.StringIO()
    redirect = contextlib.redirect_stdout(sink) if quiet else contextlib.nullcontext()
    with isolated_storage(), redirect:
        df = refine_one_raw_annotation_batch(
            raw_outputs_from_machine=raw_outputs,
            raw_json_filename="golden_fixture.json",
            verbose=False,
            notebook_mode=False,
        )
    return df


def _normalize_cell(value) -> str:
    """Canonical, comparison-stable string for any cell value.

    Handles pandas NA / numpy nan, lists / tuples / arrays, dicts and floats
    (rounded) so that pyarrow-backed and object-backed frames compare equal when
    their *content* is equal.
    """
    if isinstance(value, (list, tuple, np.ndarray)):
        return "[" + ", ".join(_normalize_cell(v) for v in value) + "]"
    if isinstance(value, dict):
        return json.dumps({k: _normalize_cell(v) for k, v in sorted(value.items())})
    try:
        if pd.isna(value):
            return "<NA>"
    except (TypeError, ValueError):
        pass
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.6f}"
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return str(int(value))
    return str(value)


def normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return an all-string, ``item_id``-indexed, column-sorted copy.

    This is both the comparison representation *and* the on-disk golden format:
    nested pyarrow dtypes (``list<item: string>[pyarrow]``) do not survive a
    pandas parquet round-trip, but the normalized all-string frame does.
    ``_normalize_cell`` is idempotent, so normalizing an already-normalized
    golden frame is a no-op and comparison stays consistent.
    """
    out = df.copy()
    out["item_id"] = out["item_id"].astype(str)
    out = out.drop_duplicates("item_id").set_index("item_id").sort_index()
    out = out.reindex(sorted(out.columns), axis=1)
    return out.map(_normalize_cell).astype("string")


def compare_refined(got: pd.DataFrame, golden: pd.DataFrame) -> list[str]:
    """Compare two refined frames by ``item_id``; return human-readable diffs.

    An empty list means the frames are content-identical (column order and
    dtype backend are ignored; cell content is compared via ``_normalize_cell``).
    The ``golden`` argument may be a raw refined frame or an already-normalized
    one — normalization is idempotent.
    """
    diffs: list[str] = []
    for label, df in (("new", got), ("golden", golden)):
        if df is None:
            diffs.append(f"{label} frame is None")
        elif "item_id" not in df.columns:
            diffs.append(f"{label} frame has no item_id column")
    if diffs:
        return diffs

    g = normalize_frame(got)
    gold = normalize_frame(golden)

    if set(g.index) != set(gold.index):
        only_new = sorted(set(g.index) - set(gold.index))[:5]
        only_gold = sorted(set(gold.index) - set(g.index))[:5]
        diffs.append(
            f"row/item_id mismatch: {len(g)} new vs {len(gold)} golden; "
            f"new_only(≤5)={only_new} golden_only(≤5)={only_gold}"
        )

    if set(g.columns) != set(gold.columns):
        new_only = sorted(set(g.columns) - set(gold.columns))
        gold_only = sorted(set(gold.columns) - set(g.columns))
        diffs.append(f"column mismatch: new_only={new_only} golden_only={gold_only}")

    common_idx = sorted(set(g.index) & set(gold.index))
    common_cols = sorted(set(g.columns) & set(gold.columns))
    for c in common_cols:
        a = g.loc[common_idx, c].map(_normalize_cell).tolist()
        b = gold.loc[common_idx, c].map(_normalize_cell).tolist()
        mismatches = [
            (idx, av, bv)
            for idx, av, bv in zip(common_idx, a, b, strict=True)
            if av != bv
        ]
        if mismatches:
            sample = mismatches[:3]
            diffs.append(
                f"column '{c}': {len(mismatches)}/{len(common_idx)} cells differ; "
                f"e.g. {[(i, f'new={x!r}', f'gold={y!r}') for i, x, y in sample]}"
            )
    return diffs


def load_fixture(path: Path = FIXTURE_PATH) -> dict:
    """Load the frozen raw-response fixture."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)
