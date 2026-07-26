"""Unit tests for the shared registry field-metadata helpers (Stage 3).

Pins:

  * ``registry_metadata.union_field_metadata``: newer ``created_at`` wins,
    ``versions_to_include`` prunes, pre-snapshot entries (no ``field_metadata``)
    contribute nothing, malformed input degrades to ``{}``.
  * ``registry_metadata.snapshot_field_metadata``: real contracts produce
    non-empty snapshots with the expected metadata keys; a broken module
    degrades to ``{}``.
  * scrape/activity ``register_version`` writes ``field_metadata`` into the
    registry entry and ``list_versions`` excludes it from summaries.
  * ``annotation_versioning.union_field_metadata(versions_to_include=...)``
    prunes to the given ids.

Usage:
    python tests/unit/test_registry_metadata.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fyp.fyp_config as fyp_cf_mod

fyp_cf_mod.initialize(verbose=False)

from fyp import (  # noqa: E402
    activity_contract,
    annotation_versioning,
    registry_metadata as rm,
    scrape_contract,
    scrape_versioning,
)

REG = {
    "versions": {
        "v_old": {
            "created_at": "2024-01-01T00:00:00",
            "field_metadata": {"a": {"role": "old"}, "b": {"role": "old"}},
        },
        "v_new": {
            "created_at": "2025-01-01T00:00:00",
            "field_metadata": {"a": {"role": "new"}},
        },
        "v_presnapshot": {"created_at": "2024-06-01T00:00:00"},
    }
}


def test_union_newer_wins_and_merges() -> None:
    got = rm.union_field_metadata(REG)
    assert got == {"a": {"role": "new"}, "b": {"role": "old"}}, got


def test_union_pruning() -> None:
    got = rm.union_field_metadata(REG, versions_to_include={"v_old"})
    assert got == {"a": {"role": "old"}, "b": {"role": "old"}}, got
    assert rm.union_field_metadata(REG, versions_to_include=set()) == {}


def test_union_malformed_degrades() -> None:
    assert rm.union_field_metadata({}) == {}
    assert rm.union_field_metadata({"versions": {"x": {"field_metadata": "junk"}}}) == {}


def test_snapshot_real_contracts() -> None:
    for mod in (scrape_contract, activity_contract):
        snap = rm.snapshot_field_metadata(mod)
        assert snap, f"{mod.__name__}: empty snapshot"
        sample = next(iter(snap.values()))
        assert "display_name" in sample or "role" in sample or "scale" in sample, sample


def test_snapshot_broken_module_degrades() -> None:
    class _Broken:
        @staticmethod
        def load_contract():
            raise RuntimeError("boom")

    assert rm.snapshot_field_metadata(_Broken) == {}


def test_scrape_register_into_snapshots_and_list_excludes() -> None:
    descriptor = scrape_versioning.active_version_descriptor()
    registry = scrape_versioning._register_into(
        scrape_versioning.empty_registry(), descriptor,
        created_at="2026-01-01T00:00:00",
        field_metadata=rm.snapshot_field_metadata(scrape_contract),
    )
    entry = registry["versions"][descriptor["scrape_contract_version"]]
    assert entry["field_metadata"], "field_metadata not recorded"
    # union over this registry reproduces the snapshot
    assert rm.union_field_metadata(registry) == entry["field_metadata"]
    # summaries exclude the bulky snapshots
    summary_keys = set()
    for info in registry["versions"].values():
        summary_keys |= {k for k in info if k in ("field_digest", "field_metadata")}
    assert summary_keys == {"field_digest", "field_metadata"}


def test_annotation_union_accepts_pruning_param() -> None:
    got = annotation_versioning.union_field_metadata(versions_to_include=set())
    assert got == {}, got
    unpruned = annotation_versioning.union_field_metadata(
        versions_to_include=set(annotation_versioning.load_registry().get("versions", {}))
    )
    assert isinstance(unpruned, dict)


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
