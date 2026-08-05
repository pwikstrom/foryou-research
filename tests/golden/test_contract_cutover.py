"""Cutover integration test for the simplified annotation contract.

New annotations carry FEWER columns than the existing corpus (dropped:
scenes/scene_sentiments_*, framing FA_*, cultural CRA_*, ideological IA_*,
aussie_political_*; scores became plain ints; audio renamed) and a NEW
``annotation_version``. The consolidated dataset is therefore a MIX of old-shape
and new-shape rows. This pins that:

  * consolidation unions the heterogeneous columns and keeps EVERY (item,
    version) pair (no data loss, re-annotation never overwrites an old version);
  * the new-shape reduced column set flows through the downstream
    factor/feature selection without referencing the now-missing columns.

Everything runs on ISOLATED temp storage so the live recoded/cache datasets are
never touched. No API calls.

Usage:
    python tests/golden/test_contract_cutover.py
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))        # tests/golden
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))    # project root

import pandas as pd

import fyp.data_io as data_io
import fyp.machine_annotation as ma
import fyp.recode_variables as rv
from fyp.fyp_config import fyp_cf

_ARCHIVE_FN = f"{ma.MACHINE_ANNOTATIONS_LABEL}_all_versions.parquet"
_AV_NEW = "av_f6a7915e6141"
_AV_OLD = "av_oldcontract"

# Recoded columns the contract simplification DROPPED (present only in old rows).
_DROPPED_COLS = [
    "scene_sentiments_energy", "scene_sentiments_valence",
    "FA_problem_definition", "CRA_key_groups", "IA_dominant_ideologies",
    "aussie_political_positioning",
]
# Columns only the NEW contract produces.
_NEW_ONLY_COLS = ["notable_sounds"]
# Kept columns present in both shapes.
_KEPT = ["type_of_story", "content_category", "political_score",
         "sensitivity_score", "speech_vs_music", "faces_age_estimate",
         "video_story", "main_gender", "main_ethnicity", "annotated_ok"]


@contextlib.contextmanager
def _isolated():
    """Redirect ALL consolidation/study storage to a throwaway temp dir.

    GCS is off in local dev, so every read/write resolves to fyp_cf["paths"].
    Redirecting these keys guarantees the live local data root is untouched.
    """
    keys = ["machine_annotations", "machine_annotations_raw",
            "machine_annotations_refined", "recoded", "cache", "temp"]
    assert not fyp_cf["data_io"].get("use_gcs_for_data"), "refuse to run with GCS data on"
    assert not fyp_cf["data_io"].get("use_gcs_for_cache"), "refuse to run with GCS cache on"
    original = {k: fyp_cf["paths"].get(k) for k in keys}
    tmp = tempfile.mkdtemp(prefix="fyp_cutover_")
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


def _old_row(i: int) -> dict:
    return {
        "item_id": str(i), "annotation_version": _AV_OLD,
        "type_of_story": "Human-Interest", "content_category": "daily life",
        "political_score": 0.1, "sensitivity_score": 0.2, "speech_vs_music": 0.5,
        "faces_age_estimate": 25.0, "video_story": "old story", "annotated_ok": True,
        "main_gender": "female", "main_ethnicity": "caucasian",
        "scene_sentiments_energy": 0.3, "scene_sentiments_valence": -0.1,
        "FA_problem_definition": "framing text", "CRA_key_groups": "cra text",
        "IA_dominant_ideologies": "ia text", "aussie_political_positioning": "-",
    }


def _new_row(i: int) -> dict:
    return {
        "item_id": str(i), "annotation_version": _AV_NEW,
        "type_of_story": "Descriptive", "content_category": "comedy",
        "political_score": 0.0, "sensitivity_score": 0.15, "speech_vs_music": 1.0,
        "faces_age_estimate": 30.0, "video_story": "new story", "annotated_ok": True,
        "main_gender": "male", "main_ethnicity": "caucasian",
        "notable_sounds": "laughter | applause",
    }


def _save_refined(filename: str, rows: list[dict]) -> None:
    data_io.save_parquet(
        df=pd.DataFrame(rows),
        storage_location="machine_annotations_refined",
        filename=filename,
    )


def test_mixed_consolidation_unions_columns_and_keeps_all_versions() -> None:
    with _isolated():
        # OLD-shape batch (items 1,2,3) then NEW-shape batch (items 3,4,5).
        # Item 3 is re-annotated under the new contract.
        # Filenames must start with MACHINE_ANNOTATIONS_LABEL to be picked up.
        _save_refined(f"{ma.MACHINE_ANNOTATIONS_LABEL}_001_old.parquet",
                      [_old_row(1), _old_row(2), _old_row(3)])
        _save_refined(f"{ma.MACHINE_ANNOTATIONS_LABEL}_002_new.parquet",
                      [_new_row(3), _new_row(4), _new_row(5)])
        _changed, active, _ids = ma.consolidate_and_save_refined_annotations(
            force_consolidation=True, verbose=False
        )
        archive = data_io.load_parquet(storage_location="recoded", filename=_ARCHIVE_FN)

    # Archive keeps every (item, version) pair — re-annotation never overwrites.
    pairs = set(zip(archive["item_id"].astype(str), archive["annotation_version"]))
    assert ("3", _AV_OLD) in pairs and ("3", _AV_NEW) in pairs
    assert len(archive) == 6                                  # 3 old + 3 new, no loss

    # Active = one row per item; the singly-annotated items are unambiguous.
    by_item = dict(zip(active["item_id"].astype(str), active["annotation_version"]))
    assert set(active["item_id"].astype(str)) == {"1", "2", "3", "4", "5"}
    assert by_item["1"] == _AV_OLD and by_item["2"] == _AV_OLD
    assert by_item["4"] == _AV_NEW and by_item["5"] == _AV_NEW
    assert by_item["3"] in (_AV_OLD, _AV_NEW)   # order-dependent w/o promotion

    # Column UNION across the two shapes — nothing is required to exist in both.
    for c in _DROPPED_COLS + _NEW_ONLY_COLS + _KEPT:
        assert c in active.columns, f"union missing column {c}"
    a = active.set_index(active["item_id"].astype(str))
    assert pd.isna(a.loc["4", "scene_sentiments_energy"])    # dropped -> NaN for new rows
    assert a.loc["1", "scene_sentiments_energy"] == 0.3      # but present for old rows
    assert pd.isna(a.loc["1", "notable_sounds"])             # new-only -> NaN for old rows
    assert a.loc["4", "notable_sounds"] == "laughter | applause"
    # A kept feature is present + correct for BOTH shapes.
    assert a.loc["1", "political_score"] == 0.1 and a.loc["4", "political_score"] == 0.0


def test_factor_feature_selection_ignores_dropped_columns() -> None:
    """get_factors_and_features only returns columns present in the frame, so a
    dropped field never gets referenced downstream (this is what protects PCA /
    study recoding from the reduced new-contract column set)."""
    vs = fyp_cf["var_schema"]
    # "measure" is the post-2026-08 name of the old "feature" role.
    feat_names = sorted(vs[vs["role"] == "measure"]["variable_name"].dropna().astype(str).tolist())
    assert feat_names, "var_schema has no 'measure' rows; fixture assumption broken"

    present = feat_names[0]
    # A frame that carries the first feature but NONE of the others.
    frame = pd.DataFrame({"item_id": ["1", "2"], present: [0.1, 0.2],
                          "annotated_ok": [True, True]})
    factors, features = rv.get_factors_and_features_from_var_schema(frame)

    assert all(c in frame.columns for c in factors + features), "selected a column not in the frame"
    assert present in features
    leaked = set(feat_names[1:]) & set(features)
    assert not leaked, f"absent var_schema features leaked into selection: {sorted(leaked)}"


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
