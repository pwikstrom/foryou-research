"""Integration tests for version-aware consolidation + promotion.

Exercises the data-layer guarantees of annotation versioning end-to-end on
isolated temp storage (no real data, no API calls):

  * the refinement pipeline re-attaches each row's annotation_version (dropped
    by recode), defaulting legacy raw files to the legacy version;
  * consolidation archives ALL (item, version) annotations — re-annotating an
    item never overwrites or deletes its earlier version;
  * with nothing promoted, the active dataset is latest-per-item (the historical
    version-agnostic behaviour);
  * promoting a version rebuilds the active dataset as that version, with a
    coverage fallback to other versions for items it does not cover.

Usage:
    python tests/golden/test_versioning_consolidation.py
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))        # tests/golden
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))    # project root

import pandas as pd

from _harness import pinned_var_schema
from test_structured_refinement_path import _structured_response

import fyp.annotation_versioning as av
import fyp.data_io as data_io
import fyp.machine_annotation as ma
from fyp.fyp_config import fyp_cf

_ARCHIVE_FN = f"{ma.MACHINE_ANNOTATIONS_LABEL}_all_versions.parquet"


@contextlib.contextmanager
def _isolated():
    """Redirect machine-annotation AND recoded storage to a throwaway temp dir.

    Recoded must be isolated too: it holds the consolidated parquet, the archive
    parquet, and the version registry.
    """
    keys = [
        "machine_annotations",
        "machine_annotations_raw",
        "machine_annotations_refined",
        "recoded",
        "temp",
    ]
    original = {k: fyp_cf["paths"].get(k) for k in keys}
    tmp = tempfile.mkdtemp(prefix="fyp_ver_")
    try:
        for k in keys:
            path = os.path.join(tmp, k)
            os.makedirs(path, exist_ok=True)
            fyp_cf["paths"][k] = path
        yield tmp
    finally:
        for k, v in original.items():
            fyp_cf["paths"][k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def _save_refined(filename: str, item_ids, version: str) -> None:
    df = pd.DataFrame(
        {
            "item_id": [str(i) for i in item_ids],
            "annotation_version": version,
            "type_of_story": ["Human-Interest"] * len(item_ids),
            "annotated_ok": [True] * len(item_ids),
        }
    )
    data_io.save_parquet(
        df=df, storage_location="machine_annotations_refined", filename=filename
    )


def test_refine_stamps_annotation_version() -> None:
    raw = {
        "0": {
            "item_id": "111",
            "structured": True,
            "annotation_version": "av_test123",
            "finish_reason": "FinishReason.STOP",
            "response": json.dumps(_structured_response("Human-Interest")),
        },
        "1": {  # no annotation_version field → legacy default
            "item_id": "222",
            "structured": True,
            "finish_reason": "FinishReason.STOP",
            "response": json.dumps(_structured_response("Issue-Based")),
        },
    }
    with pinned_var_schema(), _isolated():
        df = ma.refine_one_raw_annotation_batch(
            raw_outputs_from_machine=raw,
            raw_json_filename="machine_annotations_x.json",
            verbose=False,
        )
    vmap = dict(zip(df["item_id"].astype(str), df["annotation_version"]))
    assert vmap["111"] == "av_test123"
    assert vmap["222"] == av.LEGACY_VERSION


def test_consolidation_archives_all_versions_and_active_is_latest() -> None:
    with _isolated():
        _save_refined("machine_annotations_a.parquet", ["i1", "i2"], "v1")
        _save_refined("machine_annotations_b.parquet", ["i1", "i3"], "v2")
        _changed, active_df, _ids = ma.consolidate_and_save_refined_annotations(
            force_consolidation=True, verbose=False
        )
        archive = data_io.load_parquet(storage_location="recoded", filename=_ARCHIVE_FN)

    pairs = set(zip(archive["item_id"].astype(str), archive["annotation_version"]))
    assert ("i1", "v1") in pairs and ("i1", "v2") in pairs   # both kept; no overwrite
    assert len(archive) == 4
    assert set(active_df["item_id"].astype(str)) == {"i1", "i2", "i3"}
    assert len(active_df) == 3                                # one row per item


def test_promotion_rebuilds_active_view_without_deleting_history() -> None:
    with _isolated():
        _save_refined("machine_annotations_a.parquet", ["i1", "i2"], "v1")
        _save_refined("machine_annotations_b.parquet", ["i1", "i3"], "v2")
        av.register_version(descriptor={"annotation_version": "v1", "label": "v1"},
                            prompt_text="p1", schema_json=None)
        av.register_version(descriptor={"annotation_version": "v2", "label": "v2"},
                            prompt_text="p2", schema_json=None)
        av.promote_version("v1")
        _changed, active_df, _ids = ma.consolidate_and_save_refined_annotations(
            force_consolidation=True, verbose=False
        )
        archive = data_io.load_parquet(storage_location="recoded", filename=_ARCHIVE_FN)

    by_item = dict(zip(active_df["item_id"].astype(str), active_df["annotation_version"]))
    assert by_item["i1"] == "v1"      # active version preferred where available
    assert by_item["i2"] == "v1"
    assert by_item["i3"] == "v2"      # coverage fallback to the other version
    pairs = set(zip(archive["item_id"].astype(str), archive["annotation_version"]))
    assert ("i1", "v2") in pairs      # promotion did NOT delete i1's v2 annotation


def test_rebuild_active_from_archive_reflects_promotion() -> None:
    with _isolated():
        archive = pd.DataFrame(
            {
                "item_id": ["i1", "i1", "i2", "i3"],
                "annotation_version": ["v1", "v2", "v1", "v2"],
                "val": ["a", "b", "c", "d"],
            }
        )
        data_io.save_parquet(df=archive, storage_location="recoded", filename=_ARCHIVE_FN)
        av.register_version(descriptor={"annotation_version": "v1", "label": "v1"},
                            prompt_text="p", schema_json=None)
        av.register_version(descriptor={"annotation_version": "v2", "label": "v2"},
                            prompt_text="p", schema_json=None)
        av.promote_version("v2")
        n = ma.rebuild_preferred_annotations_from_archive(verbose=False)
        recoded = data_io.load_parquet(
            storage_location="recoded", filename=f"{ma.MACHINE_ANNOTATIONS_LABEL}_recoded.parquet"
        )
    by_item = dict(zip(recoded["item_id"].astype(str), recoded["annotation_version"]))
    assert by_item == {"i1": "v2", "i2": "v1", "i3": "v2"}  # v2 preferred, i2 fallback
    assert n == 3


def test_study_pin_resolver_uses_pinned_version() -> None:
    import fyp.organize_datasets as od

    active_df = pd.DataFrame(
        {"item_id": ["i1", "i2", "i3"], "annotation_version": ["v1", "v1", "v2"], "val": ["x", "y", "z"]}
    )
    with _isolated():
        archive = pd.DataFrame(
            {
                "item_id": ["i1", "i1", "i2", "i3"],
                "annotation_version": ["v1", "v2", "v1", "v2"],
                "val": ["a", "b", "c", "d"],
            }
        )
        data_io.save_parquet(df=archive, storage_location="recoded", filename=_ARCHIVE_FN)
        orig_defs = fyp_cf.get("study_defs")
        try:
            fyp_cf["study_defs"] = {"S_pinned": {"annotation_version": "v2"}, "S_plain": {}}
            pinned = od._annotations_for_study("S_pinned", active_df)
            plain = od._annotations_for_study("S_plain", active_df)
        finally:
            fyp_cf["study_defs"] = orig_defs
    by_item = dict(zip(pinned["item_id"].astype(str), pinned["annotation_version"]))
    assert by_item == {"i1": "v2", "i3": "v2"}            # only v2 rows from archive
    assert len(plain) == 3                                # unpinned study unchanged


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
