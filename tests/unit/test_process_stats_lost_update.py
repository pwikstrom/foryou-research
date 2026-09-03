"""Poll-path writers must not put a stale consolidate entry back over a fresh one.

2026-09-03, prod: an admin armed "Consolidate & Refresh" while a scrape ran
(auto_armed saved to process_stats at 03:07:51). A browser stats poll reached
the hub 0.7 s later. _evaluate_consolidation_staleness had loaded
process_stats at its top, spent a second reading per-step status files, found
every downstream step fresh, popped consolidation_impact from its NOW-STALE
copy of the consolidate entry and saved — and save_process_stats writes the
whole top-level key, so the entry went back to its pre-arm state. When the
scrape finished at 03:08:38 the task runner found no arm and stayed silent;
the refresh only ran because the arming instance still held the flag in
memory and the browser fired it 41 s later.

The rule (see feedback_process_stats_reload): reload immediately before
mutating a shared entry, and save only what you changed. This file pins it
for the two poll-path sites: the staleness evaluator and the stats endpoint's
stale pipeline_in_flight cleanup.

Usage:
    python -m pytest tests/unit/test_process_stats_lost_update.py
"""

from __future__ import annotations

import copy

import pytest

from web_interface.services import stats_service as ss

T_IMPACT = "2026-09-03T00:23:20+00:00"
T_LATER = "2026-09-03T00:30:00+00:00"

DOWNSTREAM = ["recode_refresh_studies", "meta_refresh_groups", "timelines_refresh",
              "pca_refresh", "embeddings_refresh", "video_map_refresh", "sessions_refresh"]


def _store_without_arm() -> dict:
    return {
        "consolidate_enrichment": {
            "consolidation_impact": {
                "timestamp": T_IMPACT,
                "affected_study_names": ["s1"],
                "affected_collection_ids": ["c1"],
                "new_annotation_item_count": 0,
            },
            "last_run_outcome": "Success",
        },
        **{p: {"last_success": T_LATER} for p in DOWNSTREAM},
    }


@pytest.fixture
def interleaved(monkeypatch):
    """A 'GCS' whose consolidate entry gains auto_armed between the function's
    first load and any later one — another instance's write landing mid-call."""
    gcs = {"doc": _store_without_arm(), "loads": 0}
    saved: list[dict] = []
    live: dict = {}

    def fake_load():
        gcs["loads"] += 1
        if gcs["loads"] >= 2:
            gcs["doc"]["consolidate_enrichment"]["auto_armed"] = True
            gcs["doc"]["consolidate_enrichment"]["auto_armed_auto_refresh"] = True
        live.clear()
        live.update(copy.deepcopy(gcs["doc"]))

    def fake_save():
        saved.append(copy.deepcopy(live["consolidate_enrichment"]))
        gcs["doc"]["consolidate_enrichment"] = copy.deepcopy(live["consolidate_enrichment"])

    monkeypatch.setattr(ss, "process_stats", live)
    monkeypatch.setattr(ss, "load_process_stats", fake_load)
    monkeypatch.setattr(ss, "save_process_stats", fake_save)
    return gcs, saved


def test_clearing_the_impact_keeps_an_arm_set_by_another_instance(interleaved):
    gcs, saved = interleaved

    out = ss._evaluate_consolidation_staleness()

    assert all(not p["stale"] for p in out["processes"].values())
    assert saved, "all steps were fresh, so the impact must have been cleared"
    final = gcs["doc"]["consolidate_enrichment"]
    assert "consolidation_impact" not in final
    assert final.get("auto_armed") is True, (
        "the save put back a copy loaded before the arm was written — lost update")
    assert gcs["loads"] >= 2, "must reload immediately before mutating"


def test_no_save_when_someone_already_cleared_the_impact(monkeypatch):
    """If the fresh copy has no impact left, there is nothing to write."""
    gcs = {"doc": _store_without_arm(), "loads": 0}
    saved: list[dict] = []
    live: dict = {}

    def fake_load():
        gcs["loads"] += 1
        if gcs["loads"] >= 2:
            gcs["doc"]["consolidate_enrichment"].pop("consolidation_impact", None)
        live.clear()
        live.update(copy.deepcopy(gcs["doc"]))

    monkeypatch.setattr(ss, "process_stats", live)
    monkeypatch.setattr(ss, "load_process_stats", fake_load)
    monkeypatch.setattr(ss, "save_process_stats", lambda: saved.append(1))

    ss._evaluate_consolidation_staleness()
    assert not saved, "nothing changed in the fresh copy, so no write"


def test_stale_steps_leave_the_impact_alone(monkeypatch):
    doc = _store_without_arm()
    doc["timelines_refresh"] = {"last_success": "2026-09-01T00:00:00+00:00"}  # older than the impact
    live: dict = {}
    saved: list[int] = []
    monkeypatch.setattr(ss, "process_stats", live)
    monkeypatch.setattr(ss, "load_process_stats", lambda: (live.clear(), live.update(copy.deepcopy(doc))))
    monkeypatch.setattr(ss, "save_process_stats", lambda: saved.append(1))

    out = ss._evaluate_consolidation_staleness()
    assert out["processes"]["timelines_refresh"]["stale"] is True
    assert not saved


def test_stats_endpoint_in_flight_cleanup_reloads_before_saving():
    """Source-level pin for the second poll-path site (a Flask route, so it is
    checked by inspection): between reading the stale in-flight flag and saving,
    the entry must be re-fetched from a fresh load."""
    import inspect
    from web_interface.routes.management import enrichment as en

    src = inspect.getsource(en)
    i = src.index('consolidate_entry.get("pipeline_in_flight")')
    j = src.index("save_process_stats()", i)
    window = src[i:j]
    assert "load_process_stats()" in window, (
        "the in-flight cleanup saves the consolidate entry without reloading first")
    assert 'process_stats.get("consolidate_enrichment"' in window, (
        "the entry saved must come from the fresh load, not the copy read at the top")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
