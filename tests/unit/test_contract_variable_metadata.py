"""Unit tests for contract-sourced Gemini variable metadata.

``role`` / ``scale`` / ``display_name`` / ``description`` for the annotation
contract's output columns no longer live in ``var_schema.csv``; the contract
(``config/annotation_contract.toml``) is the single source, and
``fyp_config._apply_contract_variable_metadata`` overlays them in memory at load.
Every Gemini-origin row is also forced under a single ``"GenAI"`` UI section.
Pins:

  * the overlay sets role/scale/display_name/description for contract-owned columns;
  * the overlay forces section="GenAI" for all Gemini-origin rows, leaving the three
    computed columns' role/scale/display_name (trend / australian_relevance /
    call_to_action_words) intact and non-Gemini rows fully untouched;
  * contract_column_metadata keys equal the flattener's output columns (rename +
    audio_summary prefix-strip), guarding the column-name mapping against drift;
  * THE HASH GATE: blanking the contract cells from the CSV and reconstructing them
    via the overlay does NOT change the var_schema hash (no study invalidation);
  * save_var_schema blanks only the contract cells (+ section for Gemini rows) and a
    real on-disk save → reload round-trip preserves the hash;
  * validate_contract rejects an invalid role/scale.

No Gemini API calls.

Usage:
    python tests/unit/test_contract_variable_metadata.py
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

import fyp.annotation_contract as ac
import fyp.recode_variables as rv
from fyp.annotation_schema import flatten_structured
from fyp.fyp_config import (
    _apply_contract_variable_metadata,
    _var_schema_path,
    fyp_cf,
    load_var_schema,
    save_var_schema,
)

META_COLS = ("role", "scale", "display_name", "description")
COMPUTED_GEMINI = ["trend", "australian_relevance", "call_to_action_words"]
# Contract-owned columns whose final name differs from the contract field name.
EXPECTED_RENAMED = {"transcript_no_repetitions", "speech_vs_music",
                    "background_music", "notable_sounds",
                    "faces_gender", "faces_age_estimate", "faces_ethnicity"}


def _raw_schema() -> pd.DataFrame:
    """The on-disk var_schema (contract cells still populated verbatim)."""
    return pd.read_csv(_var_schema_path(fyp_cf), dtype_backend="pyarrow", encoding="utf-8")


@contextlib.contextmanager
def _swapped(frame: pd.DataFrame):
    saved = fyp_cf.get("var_schema")
    fyp_cf["var_schema"] = frame
    try:
        yield
    finally:
        fyp_cf["var_schema"] = saved


def _blank_contract_cells(df: pd.DataFrame) -> pd.DataFrame:
    """Mimic what save_var_schema persists: contract cells + Gemini section blanked."""
    out = df.copy()
    contract_cols = set(ac.contract_column_metadata(ac.load_contract()).keys())
    owned = out["variable_name"].isin(contract_cols)
    for col in META_COLS:
        out.loc[owned, col] = pd.NA
    src = out["source"].astype("string").fillna("")
    gemini = src.eq("Gemini") | src.str.startswith("derived: Gemini")
    out.loc[gemini, "section"] = pd.NA
    return out


def test_overlay_sets_contract_metadata() -> None:
    meta = ac.contract_column_metadata(ac.load_contract())
    frame = _blank_contract_cells(_raw_schema())  # start from the post-deploy CSV shape
    with _swapped(frame):
        _apply_contract_variable_metadata(fyp_cf)
    for col, m in meta.items():
        row = frame.loc[frame["variable_name"] == col]
        assert not row.empty, f"{col} missing from var_schema"
        for k in META_COLS:
            got = str(row[k].iloc[0])
            assert got == str(m[k]), f"{col}.{k}: {got!r} != contract {m[k]!r}"


def test_overlay_forces_genai_section() -> None:
    frame = _raw_schema()
    with _swapped(frame):
        _apply_contract_variable_metadata(fyp_cf)
    src = frame["source"].astype("string").fillna("")
    gemini = frame[src.eq("Gemini") | src.str.startswith("derived: Gemini")]
    assert (gemini["section"].astype(str) == "GenAI").all(), "all Gemini rows must be GenAI"
    # A non-Gemini row keeps its CSV section.
    non = frame.loc[frame["variable_name"] == "stats_playCount", "section"].iloc[0]
    assert str(non) != "GenAI", "non-Gemini row must keep its section"


def test_overlay_keeps_computed_columns() -> None:
    raw = _raw_schema().set_index("variable_name")
    frame = _raw_schema()
    with _swapped(frame):
        _apply_contract_variable_metadata(fyp_cf)
    ov = frame.set_index("variable_name")
    for col in COMPUTED_GEMINI:
        assert str(ov.at[col, "section"]) == "GenAI", f"{col} should be GenAI"
        for k in ("role", "scale", "display_name"):
            assert str(ov.at[col, k]) == str(raw.at[col, k]), (
                f"{col}.{k} must keep its CSV value (computed column, not contract-owned)"
            )


def test_overlay_leaves_non_gemini_untouched() -> None:
    raw = _raw_schema().set_index("variable_name")
    frame = _raw_schema()
    with _swapped(frame):
        _apply_contract_variable_metadata(fyp_cf)
    ov = frame.set_index("variable_name")
    for col in ["stats_playCount", "desc_hashtags"]:
        if col in raw.index:
            for k in (*META_COLS, "section"):
                assert str(ov.at[col, k]) == str(raw.at[col, k]), f"{col}.{k} changed"


def test_column_mapping_matches_flattener() -> None:
    contract = ac.load_contract()
    keys = set(ac.contract_column_metadata(contract).keys())
    assert len(keys) == 27, f"expected 27 contract-owned columns, got {len(keys)}"
    # The renamed/exploded columns must be present (and the raw field names absent).
    assert EXPECTED_RENAMED <= keys, f"missing renamed columns: {EXPECTED_RENAMED - keys}"
    for raw_name in ("transcript", "faces", "audio_summary",
                     "audio_summary_speech_vs_music"):
        assert raw_name not in keys, f"{raw_name} should not be a final column"
    # Cross-check the object explode/strip against the real flattener + rename chain.
    response = {
        "faces": [{"gender": "Female", "age_estimate": 30, "ethnicity": "Caucasian"}],
        "audio_summary": {"speech_vs_music": 50, "background_music": "upbeat",
                          "notable_sounds": ["siren"]},
    }
    flat = flatten_structured(response)
    renamed = set(rv.rename_columns(pd.DataFrame([flat])).columns)
    for col in ("faces_gender", "faces_age_estimate", "faces_ethnicity",
                "speech_vs_music", "background_music", "notable_sounds"):
        assert col in renamed, f"flattener did not emit {col}"
        assert col in keys, f"{col} emitted by flattener but not in metadata"


def test_hash_invariant_to_overlay() -> None:
    """THE GATE: contract cells in the CSV vs blanked+overlaid must hash identically."""
    verbatim = _raw_schema()  # CSV still carries role/scale for contract cols
    with _swapped(verbatim):
        _apply_contract_variable_metadata(fyp_cf)  # section→GenAI either way
        hash_verbatim = rv.compute_var_schema_hash()

    blanked = _blank_contract_cells(_raw_schema())  # post-deploy on-disk shape
    with _swapped(blanked):
        _apply_contract_variable_metadata(fyp_cf)
        hash_overlay = rv.compute_var_schema_hash()

    assert hash_verbatim == hash_overlay, (
        "blanking contract role/scale from the CSV and reconstructing via the overlay "
        "changed the var_schema hash -> would invalidate every study cache"
    )


def test_save_blanks_only_contract_cells_and_preserves_hash() -> None:
    """Real save_var_schema round-trip against a temp dir (never touches live CSV)."""
    live_path = _var_schema_path(fyp_cf)
    orig_local = fyp_cf["paths"]["local_data"]
    orig_use_gcs = fyp_cf["data_io"]["use_gcs_for_data"]
    orig_vs = fyp_cf.get("var_schema")
    contract_cols = set(ac.contract_column_metadata(ac.load_contract()).keys())
    try:
        with tempfile.TemporaryDirectory() as td:
            fyp_cf["data_io"]["use_gcs_for_data"] = False
            fyp_cf["paths"]["local_data"] = td
            shutil.copy2(live_path, os.path.join(td, "var_schema.csv"))
            load_var_schema(fyp_cf)                       # overlay applied
            overlaid_hash = rv.compute_var_schema_hash()
            save_var_schema(fyp_cf["var_schema"].copy(), cf=fyp_cf)  # blanks + writes + reloads
            reloaded_hash = rv.compute_var_schema_hash()
            assert reloaded_hash == overlaid_hash, "save→reload changed the hash"

            raw = pd.read_csv(os.path.join(td, "var_schema.csv"),
                              dtype_backend="pyarrow", encoding="utf-8")
            owned = raw[raw["variable_name"].isin(contract_cols)]
            for col in META_COLS:
                assert owned[col].isna().all(), f"on-disk {col} not blanked for contract rows"
            src = raw["source"].astype("string").fillna("")
            gem = raw[src.eq("Gemini") | src.str.startswith("derived: Gemini")]
            assert gem["section"].isna().all(), "on-disk section not blanked for Gemini rows"
            # A row owned by neither contract keeps its role/scale on disk.
            sp = raw.loc[raw["variable_name"] == "activity_type"]
            assert not sp.empty and not pd.isna(sp["role"].iloc[0]), "uncontracted role wiped"
    finally:
        fyp_cf["paths"]["local_data"] = orig_local
        fyp_cf["data_io"]["use_gcs_for_data"] = orig_use_gcs
        fyp_cf["var_schema"] = orig_vs


def test_validate_contract_rejects_bad_role_scale() -> None:
    contract = ac.load_contract()
    contract["fields"][0]["role"] = "bogus_role"
    contract["fields"][0]["scale"] = "bogus_scale"
    errors = ac.validate_contract(contract)
    assert any("invalid role" in e for e in errors), f"no role error: {errors}"
    assert any("invalid scale" in e for e in errors), f"no scale error: {errors}"


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception:
            failures += 1
            import traceback

            print(f"ERROR {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
