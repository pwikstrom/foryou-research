"""Which store the refresh-pipeline step list reads, and when.

Two failure modes are pinned here, both observed on the Refresh Caches page:

1. **Per-instance divergence.** ``processes["consolidate_enrichment"]["data"]``
   is the subprocess ``::DATA::`` mirror. On Cloud Run the consolidate worker
   runs in the *other* service, so the web service never updates that dict —
   but the dispatch endpoint used to seed it with the ``steps: []`` marker. The
   hub instance that served the click then served a one-row list forever while
   every other instance served the real plan, and the 2s poll flipped between
   them. The view must ignore the in-memory copy on Cloud Run and honour it
   locally (where it is the only mid-run source).

2. **A forecast plan reading as "skipped".** The marker seeded at dispatch now
   carries the whole pipeline so the user sees what is queued. Its steps must
   stay ``pending`` until the consolidation that confirms them has finished,
   including in the gap before any step reports itself running.
"""

import pytest

from web_interface.services import worker_status as ws


T0 = "2026-08-16T10:00:00+00:00"   # dispatch — marker seeded
T1 = "2026-08-16T10:20:00+00:00"   # consolidation done — real plan published

REAL_STEPS = ["embeddings_refresh", "video_map_refresh", "recode_refresh_studies",
              "meta_refresh_groups", "pca_refresh", "timelines_refresh",
              "sessions_refresh"]


@pytest.fixture
def stores(monkeypatch):
    """Swap both stores for empty ones and hand back a seeding helper."""
    state = {"process_stats": {}, "processes": {}, "task_status": {}}
    monkeypatch.setattr(ws, "process_stats", state["process_stats"])
    monkeypatch.setattr(ws, "processes", state["processes"])
    monkeypatch.setattr(ws, "read_task_status", lambda step: state["task_status"].get(step))
    return state


def _states(view):
    return {row["step"]: row["state"] for row in view}


def test_cloud_run_ignores_the_stale_in_memory_marker(stores, monkeypatch):
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["process_stats"]["consolidate_enrichment"] = {
        "pipeline_plan": {"steps": REAL_STEPS, "started_ts": T1},
        "last_run_end_time": T1,
        "last_run_outcome": "Success",
    }
    # The marker this instance wrote when it served POST /consolidate.
    stores["processes"]["consolidate_enrichment"] = {
        "data": {"pipeline_plan": {"steps": [], "started_ts": T0}}
    }

    view = ws._build_pipeline_step_view(pipeline_active=True)

    assert [row["step"] for row in view] == ["consolidate_enrichment"] + REAL_STEPS


def test_local_mode_still_reads_the_in_memory_mirror(stores, monkeypatch):
    """Locally the worker's plan lands in memory before process_stats has it."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: False)
    stores["process_stats"]["consolidate_enrichment"] = {
        "pipeline_plan": {"steps": [], "started_ts": T0},
    }
    stores["processes"]["consolidate_enrichment"] = {
        "data": {"pipeline_plan": {"steps": REAL_STEPS, "started_ts": T1}}
    }

    view = ws._build_pipeline_step_view(pipeline_active=True)

    assert [row["step"] for row in view] == ["consolidate_enrichment"] + REAL_STEPS


def test_forecast_plan_is_pending_until_consolidation_finishes(stores, monkeypatch):
    """The seeded forecast shows every step, pending, from the first poll."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["process_stats"]["consolidate_enrichment"] = {
        "pipeline_plan": {"steps": REAL_STEPS, "started_ts": T0,
                          "mode": "refresh", "provisional": True},
    }

    # pipeline_active=False is the worst case: the dispatch has landed but no
    # status file has been written yet.
    view = ws._build_pipeline_step_view(pipeline_active=False)
    states = _states(view)

    assert len(view) == len(REAL_STEPS) + 1
    assert all(states[step] == "pending" for step in REAL_STEPS), states
    assert all(row["provisional"] for row in view if row["step"] != "consolidate_enrichment")


def test_forecast_steps_go_skipped_once_consolidation_ends_without_a_real_plan(
        stores, monkeypatch):
    """A consolidation that died before publishing its plan must not read as pending."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["process_stats"]["consolidate_enrichment"] = {
        "pipeline_plan": {"steps": REAL_STEPS, "started_ts": T0,
                          "mode": "refresh", "provisional": True},
        "last_run_end_time": T1,
        "last_run_outcome": "Fail",
    }

    states = _states(ws._build_pipeline_step_view(pipeline_active=False))

    assert states["consolidate_enrichment"] == "failed"
    assert all(states[step] == "skipped" for step in REAL_STEPS), states


def test_real_plan_clears_the_provisional_flag(stores, monkeypatch):
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["process_stats"]["consolidate_enrichment"] = {
        "pipeline_plan": {"steps": ["embeddings_refresh"], "started_ts": T1},
        "last_run_end_time": T1,
        "last_run_outcome": "Success",
    }

    view = ws._build_pipeline_step_view(pipeline_active=True)

    assert [row["step"] for row in view] == ["consolidate_enrichment", "embeddings_refresh"]
    assert not any(row["provisional"] for row in view)
