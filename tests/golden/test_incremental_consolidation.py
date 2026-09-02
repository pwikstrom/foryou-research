"""Incremental consolidation must equal a full rebuild — byte-for-value.

The incremental fold (scrape + annotation lanes) and the enrichment-status
patch reuse the full rebuild's transforms, so their outputs must match a
force_consolidation full rebuild over the same files exactly. These tests run
synthetic multi-cycle sequences incrementally, then force a full rebuild in
the SAME isolated store and assert canonical frame equality — covering
re-scrape value backfills, seed addition/eviction, version promotion,
contract-bump fallback to the full path, crash replay, and the status patch.

Everything runs on throwaway temp storage (tests/_storage_guard.py keeps GCS
out of reach regardless).

Usage:
    python tests/golden/test_incremental_consolidation.py
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

import fyp.annotation_versioning as av
import fyp.data_io as data_io
import fyp.machine_annotation as ma
import fyp.organize_datasets as od
from fyp.fyp_config import fyp_cf
from fyp.scrape import scrape as sc_mod
from fyp.scrape.scrape import consolidate_and_save_scrape_data

_SCRAPES_RECODED = "scrapes_recoded.parquet"
_LEDGER = "consolidated_enrichment_files.json"
_ARCHIVE_FN = f"{ma.MACHINE_ANNOTATIONS_LABEL}_all_versions.parquet"
_ANNO_RECODED = f"{ma.MACHINE_ANNOTATIONS_LABEL}_recoded.parquet"


@contextlib.contextmanager
def _isolated():
    """Redirect scrape, annotation, and recoded storage to a temp dir."""
    keys = [
        "scrape",
        "machine_annotations",
        "machine_annotations_raw",
        "machine_annotations_refined",
        "recoded",
        "temp",
    ]
    original = {k: fyp_cf["paths"].get(k) for k in keys}
    tmp = tempfile.mkdtemp(prefix="fyp_inc_")
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


def _scrape_rows(rows: list[dict]) -> pd.DataFrame:
    base = {
        "source_platform": "tiktok",
        "video_downloaded": False,
        "scraped_ok": True,
        "play_count": 1,
        "storage_link": "",
    }
    df = pd.DataFrame([{**base, **r} for r in rows])
    df["item_id"] = df["item_id"].astype("string[pyarrow]")
    df["source_platform"] = df["source_platform"].astype("string[pyarrow]")
    df["video_downloaded"] = df["video_downloaded"].astype("bool[pyarrow]")
    df["scraped_ok"] = df["scraped_ok"].astype("bool[pyarrow]")
    df["play_count"] = df["play_count"].astype("int64[pyarrow]")
    df["scrape_ts"] = df["scrape_ts"].astype("string[pyarrow]")
    df["storage_link"] = df["storage_link"].astype("string[pyarrow]")
    return df


def _save_scrape_file(filename: str, rows: list[dict]) -> None:
    data_io.save_parquet(df=_scrape_rows(rows), storage_location="scrape", filename=filename)


def _save_seed_file(filename: str, rows: list[dict]) -> None:
    df = _scrape_rows(rows)
    data_io.save_parquet(df=df, storage_location="recoded", filename=filename)


def _save_refined(filename: str, item_ids, version: str, story: str = "Human-Interest") -> None:
    df = pd.DataFrame(
        {
            "item_id": [str(i) for i in item_ids],
            "annotation_version": version,
            "type_of_story": [story] * len(item_ids),
            "annotated_ok": [True] * len(item_ids),
            "annotated_fail": [False] * len(item_ids),
        }
    )
    data_io.save_parquet(
        df=df, storage_location="machine_annotations_refined", filename=filename
    )


def _canon(df: pd.DataFrame, key_cols: list[str]) -> pd.DataFrame:
    """Sort rows and columns, normalize dtypes to strings, for equality checks."""
    df = df.copy()
    if df.index.name is not None:
        df = df.reset_index()
    df = df.reindex(sorted(df.columns), axis=1)
    df = df.sort_values(key_cols, kind="mergesort").reset_index(drop=True)
    return df.astype("string").fillna("<NA>")


def _assert_frames_equal(a: pd.DataFrame, b: pd.DataFrame, key_cols: list[str], label: str) -> None:
    ca, cb = _canon(a, key_cols), _canon(b, key_cols)
    assert list(ca.columns) == list(cb.columns), (
        f"{label}: column drift {sorted(set(ca.columns) ^ set(cb.columns))}")
    pd.testing.assert_frame_equal(ca, cb, obj=label)


# ---------------------------------------------------------------------------
# Scrape lane
# ---------------------------------------------------------------------------

def test_scrape_fold_equals_full_rebuild_over_cycles() -> None:
    with _isolated():
        # Cycle 1 (no ledger yet → full path establishes it + the seed flag).
        _save_scrape_file("scrapes_20260101.parquet", [
            {"item_id": "a1", "scrape_ts": "2026-01-01", "play_count": 10},
            {"item_id": "a2", "scrape_ts": "2026-01-01", "play_count": 20},
        ])
        ok, df1, ids1 = consolidate_and_save_scrape_data(incremental=True)
        assert ok and ids1 == {"a1", "a2"}
        assert "is_enrichment_seed" in df1.columns

        # Cycle 2: new item + a re-scrape backfilling a1's value (newer ts).
        _save_scrape_file("scrapes_20260102.parquet", [
            {"item_id": "b1", "scrape_ts": "2026-01-02", "play_count": 5},
            {"item_id": "a1", "scrape_ts": "2026-01-02", "play_count": 999},
        ])
        ok, df2, ids2 = consolidate_and_save_scrape_data(incremental=True)
        assert ok and ids2 == {"b1", "a1"}, f"changed ids: {ids2}"
        a1 = df2[df2["item_id"] == "a1"]
        assert len(a1) == 1 and int(a1["play_count"].iloc[0]) == 999

        # Cycle 3: a seed file appears (s1 new; a2 already has a real scrape).
        _save_seed_file("tiktok_donated_enrichment_seed.parquet", [
            {"item_id": "s1", "scrape_ts": "2026-01-03", "play_count": 7},
            {"item_id": "a2", "scrape_ts": "2026-01-03", "play_count": 777},
        ])
        ok, df3, ids3 = consolidate_and_save_scrape_data(incremental=True)
        assert ok
        assert "s1" in ids3 and "a2" not in ids3, f"changed ids: {ids3}"
        s1 = df3[df3["item_id"] == "s1"]
        assert len(s1) == 1 and bool(s1["is_enrichment_seed"].iloc[0])
        assert len(df3[df3["item_id"] == "a2"]) == 1  # seed anti-joined away

        # Cycle 4: a real scrape for the seeded item evicts the seed row.
        _save_scrape_file("scrapes_20260104.parquet", [
            {"item_id": "s1", "scrape_ts": "2026-01-04", "play_count": 70},
        ])
        ok, df4, ids4 = consolidate_and_save_scrape_data(incremental=True)
        assert ok and "s1" in ids4
        s1 = df4[df4["item_id"] == "s1"]
        assert len(s1) == 1
        assert not bool(s1["is_enrichment_seed"].iloc[0])
        assert int(s1["play_count"].iloc[0]) == 70
        assert bool(s1["scraped_ok"].iloc[0])

        # The reference: a full rebuild over the same files must match exactly.
        # Its value diff runs against the folded frame — zero changed ids IS
        # the equality signal, doubled by the canonical frame comparison.
        ok, full_df, full_ids = consolidate_and_save_scrape_data(force_consolidation=True)
        assert ok and full_ids == set(), f"full rebuild diverged on: {full_ids}"
        _assert_frames_equal(df4, full_df, ["source_platform", "item_id"], "scrapes fold-vs-full")


def test_scrape_crash_replay_is_idempotent() -> None:
    """A crash after the save but before the ledger write replays to the same frame."""
    with _isolated():
        _save_scrape_file("scrapes_20260101.parquet", [
            {"item_id": "a1", "scrape_ts": "2026-01-01", "play_count": 10},
        ])
        consolidate_and_save_scrape_data(incremental=True)
        _save_scrape_file("scrapes_20260102.parquet", [
            {"item_id": "b1", "scrape_ts": "2026-01-02", "play_count": 5},
        ])
        ok, folded, _ = consolidate_and_save_scrape_data(incremental=True)
        assert ok

        # Simulate the crash: roll the ledger back so the second file reads as
        # unconsolidated while the recoded parquet already contains it.
        ledger = data_io.load_json(storage_location="recoded", filename=_LEDGER)
        ledger[sc_mod._scrapes_label()]["filenames"] = ["scrapes_20260101.parquet"]
        data_io.save_json(data=ledger, storage_location="recoded", filename=_LEDGER)

        ok, replayed, replay_ids = consolidate_and_save_scrape_data(incremental=True)
        assert ok
        # The replayed batch dedupes away — identical output, and the ids the
        # replay reports are the batch's (the impact recipient re-refreshes
        # the same scope, which is idempotent downstream).
        assert replay_ids == set()
        _assert_frames_equal(folded, replayed, ["source_platform", "item_id"], "scrapes crash replay")


def test_contract_bump_declines_the_fold() -> None:
    with _isolated():
        _save_scrape_file("scrapes_20260101.parquet", [
            {"item_id": "a1", "scrape_ts": "2026-01-01", "play_count": 10},
        ])
        consolidate_and_save_scrape_data(incremental=True)
        ledger = data_io.load_json(storage_location="recoded", filename=_LEDGER)
        ledger[sc_mod._scrapes_label()]["scrape_contract_version"] = "sv_other"
        data_io.save_json(data=ledger, storage_location="recoded", filename=_LEDGER)
        _save_scrape_file("scrapes_20260102.parquet", [
            {"item_id": "b1", "scrape_ts": "2026-01-02", "play_count": 5},
        ])

        calls = []
        orig_fold = sc_mod._fold_scrape_batch
        sc_mod._fold_scrape_batch = lambda **k: calls.append(1) or orig_fold(**k)
        try:
            ok, df, ids = consolidate_and_save_scrape_data(incremental=True)
        finally:
            sc_mod._fold_scrape_batch = orig_fold
        assert ok
        assert not calls, "contract-version bump must take the full path, not the fold"
        # A contract bump keeps the FULL value diff (not batch-scoped): items
        # whose values actually changed flag — here only the new one.
        assert ids == {"b1"}, f"changed ids: {ids}"


def test_dry_run_persists_nothing() -> None:
    with _isolated():
        _save_scrape_file("scrapes_20260101.parquet", [
            {"item_id": "a1", "scrape_ts": "2026-01-01", "play_count": 10},
        ])
        ok, df, _ = consolidate_and_save_scrape_data(force_consolidation=True, dry_run=True)
        assert ok and len(df) == 1
        assert not data_io.exists(storage_location="recoded", filename=_SCRAPES_RECODED)
        assert not data_io.exists(storage_location="recoded", filename=_LEDGER)


# ---------------------------------------------------------------------------
# Annotation lane
# ---------------------------------------------------------------------------

def test_annotation_fold_equals_full_rebuild_with_promotion() -> None:
    with _isolated():
        # Cycle 1: v1 annotations (full path establishes the ledger + archive).
        _save_refined("machine_annotations_a.parquet", ["i1", "i2"], "v1")
        ok, view1, ids1 = ma.consolidate_and_save_refined_annotations(incremental=True)
        assert ok and ids1 == {"i1", "i2"}

        # Cycle 2: v2 re-annotates i1 and adds i3 — the fold path.
        _save_refined("machine_annotations_b.parquet", ["i1", "i3"], "v2")
        ok, view2, ids2 = ma.consolidate_and_save_refined_annotations(incremental=True)
        assert ok and ids2 == {"i1", "i3"}
        by_item = dict(zip(view2["item_id"].astype(str), view2["annotation_version"]))
        assert by_item == {"i1": "v2", "i2": "v1", "i3": "v2"}  # latest-per-item

        # Archive kept every (item, version) pair.
        archive = data_io.load_parquet(storage_location="recoded", filename=_ARCHIVE_FN)
        pairs = set(zip(archive["item_id"].astype(str), archive["annotation_version"]))
        assert {("i1", "v1"), ("i1", "v2"), ("i2", "v1"), ("i3", "v2")} <= pairs

        # Promote v1 — the next consolidation detects the promotion via the
        # ledger's recorded preferred_version and DECLINES the fold (a fold
        # would leave untouched keys on the pre-promotion view), taking the
        # full path, which applies the promotion everywhere.
        av.register_version(descriptor={"annotation_version": "v1", "label": "v1"},
                            prompt_text="p1", schema_json=None)
        av.register_version(descriptor={"annotation_version": "v2", "label": "v2"},
                            prompt_text="p2", schema_json=None)
        av.promote_version("v1")
        _save_refined("machine_annotations_c.parquet", ["i4"], "v2")
        ok, view3, ids3 = ma.consolidate_and_save_refined_annotations(incremental=True)
        assert ok and ids3 == {"i4"}
        by_item = dict(zip(view3["item_id"].astype(str), view3["annotation_version"]))
        assert by_item == {"i1": "v1", "i2": "v1", "i3": "v2", "i4": "v2"}

        # With the promotion recorded, the following cycle folds again.
        _save_refined("machine_annotations_d.parquet", ["i5"], "v2")
        ok, view4, ids4 = ma.consolidate_and_save_refined_annotations(incremental=True)
        assert ok and ids4 == {"i5"}
        by_item = dict(zip(view4["item_id"].astype(str), view4["annotation_version"]))
        assert by_item["i5"] == "v2" and by_item["i1"] == "v1"

        # The registry saw the whole archive's version set.
        recorded = av.versions_in_data()
        assert {"v1", "v2"} <= set(recorded)

        # Reference: full rebuild equality of both the view and the archive.
        folded_archive = data_io.load_parquet(storage_location="recoded", filename=_ARCHIVE_FN)
        ok, full_view, _ = ma.consolidate_and_save_refined_annotations(force_consolidation=True)
        assert ok
        full_archive = data_io.load_parquet(storage_location="recoded", filename=_ARCHIVE_FN)
        _assert_frames_equal(view4, full_view,
                             ["source_platform", "item_id"], "annotation view fold-vs-full")
        _assert_frames_equal(folded_archive, full_archive,
                             ["source_platform", "item_id", "annotation_version"],
                             "annotation archive fold-vs-full")


def test_annotation_fold_view_matches_full_when_no_promotion() -> None:
    """Without a promoted version the folded view equals the full rebuild's."""
    with _isolated():
        _save_refined("machine_annotations_a.parquet", ["i1", "i2"], "v1")
        ma.consolidate_and_save_refined_annotations(incremental=True)
        _save_refined("machine_annotations_b.parquet", ["i1", "i3"], "v2", story="Issue-Based")
        ok, folded_view, _ = ma.consolidate_and_save_refined_annotations(incremental=True)
        assert ok
        ok, full_view, _ = ma.consolidate_and_save_refined_annotations(force_consolidation=True)
        assert ok
        _assert_frames_equal(folded_view, full_view, ["source_platform", "item_id"],
                             "annotation fold-vs-full view")


# ---------------------------------------------------------------------------
# Status patch
# ---------------------------------------------------------------------------

def test_status_patch_equals_full_rebuild() -> None:
    with _isolated():
        collections = pd.DataFrame({
            "item_id": pd.array(["a1", "a1", "a2", "b1", "s1"], dtype="string[pyarrow]"),
            od.collection_id_column: pd.array(["c1", "c2", "c1", "c1", "c2"], dtype="string[pyarrow]"),
        })
        scrapes = _scrape_rows([
            {"item_id": "a1", "scrape_ts": "2026-01-01", "play_count": 10},
            {"item_id": "a2", "scrape_ts": "2026-01-01", "play_count": 20},
        ])
        annotations = pd.DataFrame({
            "item_id": pd.array(["a1"], dtype="string[pyarrow]"),
            "annotated_ok": pd.array([True], dtype="bool[pyarrow]"),
            "annotated_fail": pd.array([False], dtype="bool[pyarrow]"),
        })

        baseline_annotations = pd.DataFrame({
            "item_id": pd.array(["a2"], dtype="string[pyarrow]"),
            "annotated_ok": pd.array([True], dtype="bool[pyarrow]"),
            "annotated_fail": pd.array([False], dtype="bool[pyarrow]"),
        })
        annotations = pd.concat([baseline_annotations, annotations], ignore_index=True)

        orig_failed = od.load_failed_scrapes
        od.load_failed_scrapes = lambda **k: ["b1"]
        try:
            # Baseline status from the full rebuild — both lanes non-empty so
            # the flag columns are already in merge (NA-where-unmatched)
            # semantics, the regime status_patch_allowed requires.
            od.update_enrichment_status(all_datasets={
                od._collections_label(): collections,
                od._machine_annotations_label(): baseline_annotations,
                od._scrapes_label(): scrapes,
            }, save_to_disk=True)

            # Enrichment moves: b1 gets scraped, a1 gets annotated, and the
            # failed list changes (b1 recovers, s1 fails).
            scrapes2 = pd.concat([scrapes, _scrape_rows([
                {"item_id": "b1", "scrape_ts": "2026-01-02", "play_count": 5},
            ])], ignore_index=True)
            od.load_failed_scrapes = lambda **k: ["s1"]

            patched = od.patch_enrichment_status(
                {"b1", "a1"}, scrape_frame=scrapes2, annotation_frame=annotations)
            assert patched is not None

            full = od.update_enrichment_status(all_datasets={
                od._collections_label(): collections,
                od._machine_annotations_label(): annotations,
                od._scrapes_label(): scrapes2,
            }, save_to_disk=False)
        finally:
            od.load_failed_scrapes = orig_failed

        _assert_frames_equal(patched, full, ["item_id"], "status patch-vs-full")
        assert bool(patched.loc["b1", "scraped_ok"])
        assert bool(patched.loc["a1", "annotated_ok"])
        assert bool(patched.loc["s1", "scrape_fail"])
        assert pd.isna(patched.loc["b1", "scrape_fail"])


_TESTS = [
    test_scrape_fold_equals_full_rebuild_over_cycles,
    test_scrape_crash_replay_is_idempotent,
    test_contract_bump_declines_the_fold,
    test_dry_run_persists_nothing,
    test_annotation_fold_equals_full_rebuild_with_promotion,
    test_annotation_fold_view_matches_full_when_no_promotion,
    test_status_patch_equals_full_rebuild,
]


def _main() -> int:
    passed = 0
    for test in _TESTS:
        try:
            test()
            print(f"PASS  {test.__name__}")
            passed += 1
        except Exception as exc:
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{passed}/{len(_TESTS)} passed")
    return 0 if passed == len(_TESTS) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
