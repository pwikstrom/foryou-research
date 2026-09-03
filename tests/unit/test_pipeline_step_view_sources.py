"""Which store the refresh-pipeline step list reads, and when.

Two failure modes are pinned here, both observed on the Dataset Assembly page:

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

    # Every canonical step is listed either way now, so the tell is whether the
    # steps count as PLANNED: the stale marker's empty plan would mark all seven
    # "not_planned".
    assert [row["step"] for row in view] == ["consolidate_enrichment"] + REAL_STEPS
    assert not any(row["state"] == "not_planned" for row in view), _states(view)


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
    assert not any(row["state"] == "not_planned" for row in view), _states(view)


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

    # The narrow plan no longer shortens the list — the six steps it left out
    # stay on screen as "not_planned".
    assert [row["step"] for row in view] == ["consolidate_enrichment"] + REAL_STEPS
    assert not any(row["provisional"] for row in view)
    states = _states(view)
    assert states["embeddings_refresh"] != "not_planned", states
    assert all(states[s] == "not_planned" for s in REAL_STEPS[1:]), states


# -------- Every step, every run --------
#
# The chart used to draw only the steps a run planned, so its row count moved
# with whatever the consolidation happened to touch and an absent step was
# indistinguishable from a step that does not exist. The view now emits the
# whole canonical order every time and marks the ones this run's plan left out.


def test_every_canonical_step_gets_a_row_with_unplanned_ones_marked(stores, monkeypatch):
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    planned = ["recode_refresh_studies", "meta_refresh_groups"]
    stores["process_stats"]["consolidate_enrichment"] = {
        "pipeline_plan": {"steps": planned, "started_ts": T0, "mode": "refresh"},
        "last_run_end_time": T1, "last_run_outcome": "Success",
    }

    view = ws._build_pipeline_step_view(pipeline_active=False)
    states = _states(view)

    assert [row["step"] for row in view] == ["consolidate_enrichment"] + list(
        ws.PIPELINE_STEPS_ORDER)
    assert all(states[s] == "not_planned"
               for s in ws.PIPELINE_STEPS_ORDER if s not in planned), states
    # A planned step that never ran is "skipped" — a different thing, kept apart.
    assert all(states[s] == "skipped" for s in planned), states
    assert all(row["plan_mode"] == "refresh" for row in view)


def test_unplanned_rows_carry_no_timing_even_with_a_fresh_terminal_record(stores, monkeypatch):
    """An unplanned row is inert: it must not adopt work that ran outside the run.

    A hand-started Timelines refresh (or a sessions run self-chained from a
    study save) can land inside the pipeline's window. Reading its status into
    an unplanned row would draw foreign work as part of this pipeline and, worse,
    stretch the chart's shared time axis.
    """
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["process_stats"]["consolidate_enrichment"] = {
        "pipeline_plan": {"steps": ["recode_refresh_studies"], "started_ts": T0,
                          "mode": "refresh"},
        "last_run_end_time": T1, "last_run_outcome": "Success",
    }
    # Ran during the window, but this pipeline never planned it.
    stores["process_stats"]["timelines_refresh"] = {
        "last_run_end_time": T1, "last_run_duration": 99.0, "last_run_outcome": "Success",
    }
    stores["task_status"]["sessions_refresh"] = {
        "state": "running", "start_time": T1, "updated_at": T1,
        "progress": {"percent": 40, "message": "segmenting"},
    }

    rows = {row["step"]: row for row in ws._build_pipeline_step_view(pipeline_active=True)}

    for step in ("timelines_refresh", "sessions_refresh"):
        row = rows[step]
        assert row["state"] == "not_planned", row
        assert all(row[k] is None for k in
                   ("started_at", "ended_at", "queued_at", "duration_s",
                    "ran_at", "percent", "message")), row


def test_consolidate_only_run_marks_every_refresh_step_not_planned(stores, monkeypatch):
    """Refresh unticked: the steps were not requested, not "not needed"."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["process_stats"]["consolidate_enrichment"] = {
        "pipeline_plan": {"steps": [], "started_ts": T0, "mode": "consolidate_only",
                          "provisional": False},
        "last_run_end_time": T1, "last_run_outcome": "Success",
    }

    view = ws._build_pipeline_step_view(pipeline_active=False)
    states = _states(view)

    assert states["consolidate_enrichment"] == "success"
    assert all(states[s] == "not_planned" for s in ws.PIPELINE_STEPS_ORDER), states
    # The renderer words these "not requested" rather than "not needed".
    assert all(row["plan_mode"] == "consolidate_only" for row in view)


def test_a_planned_step_outside_the_canonical_order_still_gets_a_row(stores, monkeypatch):
    """A plan record is data — a planned step must never lose its row."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["process_stats"]["consolidate_enrichment"] = {
        "pipeline_plan": {"steps": ["pca_refresh", "some_future_refresh"],
                          "started_ts": T0, "mode": "refresh"},
        "last_run_end_time": T1, "last_run_outcome": "Success",
    }

    view = ws._build_pipeline_step_view(pipeline_active=False)

    assert [row["step"] for row in view] == (
        ["consolidate_enrichment"] + list(ws.PIPELINE_STEPS_ORDER) + ["some_future_refresh"])
    assert _states(view)["some_future_refresh"] != "not_planned"


def test_unplanned_rows_are_never_provisional(stores, monkeypatch):
    """The forecast lists every step, so "provisional" and "not_planned" can't collide."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["process_stats"]["consolidate_enrichment"] = {
        "pipeline_plan": {"steps": ["pca_refresh"], "started_ts": T0,
                          "mode": "refresh", "provisional": True},
    }

    view = ws._build_pipeline_step_view(pipeline_active=True)

    assert not any(row["provisional"] for row in view if row["state"] == "not_planned")
