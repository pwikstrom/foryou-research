"""What the refresh-run chart reads, and which rows it refuses to fill in.

The chart is built from the run record (``process_stats["refresh_pipeline"]``),
which any origin writes — a consolidation, a worker card, "Refresh All
Affected". Three failure modes are pinned here, all observed on the Dataset
Assembly page:

1. **A plan reading as "skipped".** The record seeded at dispatch carries the
   whole run so the operator sees what is queued. Its steps must stay
   ``pending`` until the origin that confirms them has finished, including in
   the gap before any step reports itself running.

2. **Foreign work drawn into a run.** A step that is not part of this run —
   pruned, upstream of the origin, or unrelated — must read no status file at
   all. A hand-started refresh or a sessions run chained from a study save can
   land inside the window, and adopting it would both misreport the run and
   stretch the chart's shared time axis.

3. **A moving chart.** Every canonical step gets a row every run, so the shape
   is the same each time and the reader learns what was NOT needed as well as
   what ran.
"""

import pytest

from web_interface.services import refresh_pipeline as rp
from web_interface.services import worker_status as ws


T0 = "2026-08-16T10:00:00+00:00"   # run seeded
T1 = "2026-08-16T10:20:00+00:00"   # origin finished

REAL_STEPS = list(rp.DOWNSTREAM_ORDER)


def _record(origin, *, states=None, mode="refresh", provisional=False,
            started_ts=T0, in_flight=True, kind="consolidate"):
    """A run record with every step in a named state (planned by default)."""
    record = rp.plan_run(origin, kind=kind, mode=mode, provisional=provisional)
    record["started_ts"] = started_ts
    record["in_flight"] = in_flight
    for step, state in (states or {}).items():
        record["steps"][step] = {**(record["steps"].get(step) or {}), "state": state}
    return record


@pytest.fixture
def stores(monkeypatch):
    """Swap every store for an empty one and hand back a seeding helper."""
    state = {"process_stats": {}, "processes": {}, "task_status": {}}
    monkeypatch.setattr(ws, "process_stats", state["process_stats"])
    monkeypatch.setattr(ws, "processes", state["processes"])
    monkeypatch.setattr(ws, "read_task_status", lambda step: state["task_status"].get(step))
    # load_run reads process_stats through process_manager; point it at the
    # same dict the view uses so a seeded record is visible to both.
    monkeypatch.setattr(rp, "load_run",
                        lambda reload=True: state["process_stats"].get(rp.RUN_KEY))
    state["seed"] = lambda record: state["process_stats"].__setitem__(rp.RUN_KEY, record)
    return state


def _states(view):
    return {row["step"]: row["state"] for row in view}


def test_no_run_recorded_still_lists_every_worker_as_idle(stores, monkeypatch):
    """The block is a standing list of the workers, not only a run's record.

    With no run ever recorded it still shows every step in dependency order —
    that is what gives a quiet system a route into each worker's log — but the
    rows are inert: no state to read, no timing to draw an axis from.
    """
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    view = ws._build_pipeline_step_view(pipeline_active=False)

    assert [row["step"] for row in view] == ["consolidate_enrichment"] + REAL_STEPS
    assert {row["state"] for row in view} == {"idle"}
    for row in view:
        assert row["started_at"] is None
        assert row["ended_at"] is None
        assert row["duration_s"] is None
        assert row["message"] is None
        assert row["is_origin"] is False


def test_idle_rows_are_named_without_a_verb(stores, monkeypatch):
    """The chart lists workers by name; a column of gerunds reads as noise."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    labels = {r["step"]: r["label"]
              for r in ws._build_pipeline_step_view(pipeline_active=False)}

    assert labels["embeddings_refresh"] == "Semantic embeddings"
    assert labels["video_map_refresh"] == "Semantic map"
    assert labels["recode_refresh_studies"] == "Study definitions"
    for label in labels.values():
        assert not label.startswith(("Refreshing", "Rebuilding", "Consolidating"))


def test_plan_is_pending_until_the_origin_finishes(stores, monkeypatch):
    """The seeded plan shows every step, pending, from the first poll."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["seed"](_record("consolidate_enrichment", provisional=True))

    # pipeline_active=False is the worst case: the dispatch has landed but no
    # status file has been written yet.
    view = ws._build_pipeline_step_view(pipeline_active=False)
    states = _states(view)

    assert len(view) == len(REAL_STEPS) + 1
    assert all(states[step] == "pending" for step in REAL_STEPS), states
    assert all(row["provisional"] for row in view
               if row["step"] != "consolidate_enrichment")


def test_planned_steps_go_skipped_once_the_origin_fails(stores, monkeypatch):
    """An origin that died before reaching them must not leave them pending."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["seed"](_record("consolidate_enrichment", provisional=True))
    stores["process_stats"]["consolidate_enrichment"] = {
        "last_run_end_time": T1, "last_run_outcome": "Fail",
    }

    states = _states(ws._build_pipeline_step_view(pipeline_active=False))

    assert states["consolidate_enrichment"] == "failed"
    assert all(states[step] == "skipped" for step in REAL_STEPS), states


def test_a_card_origin_marks_the_steps_before_it_upstream(stores, monkeypatch):
    """A map rebuild does not re-embed — those rows are not part of the run."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["seed"](_record("video_map_refresh", kind="card", provisional=True))

    view = ws._build_pipeline_step_view(pipeline_active=True)
    rows = {row["step"]: row for row in view}

    assert [row["step"] for row in view] == ["consolidate_enrichment"] + REAL_STEPS
    assert rows["consolidate_enrichment"]["state"] == "upstream"
    assert rows["embeddings_refresh"]["state"] == "upstream"
    assert rows["video_map_refresh"]["is_origin"] is True
    assert rows["recode_refresh_studies"]["state"] == "pending"


def test_upstream_rows_carry_no_timing_even_with_a_fresh_terminal_record(
        stores, monkeypatch):
    """An earlier step that ran recently is still not part of THIS run."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["seed"](_record("video_map_refresh", kind="card"))
    stores["process_stats"]["embeddings_refresh"] = {
        "last_run_end_time": T1, "last_run_duration": 99.0, "last_run_outcome": "Success",
    }

    rows = {row["step"]: row for row in
            ws._build_pipeline_step_view(pipeline_active=True)}

    row = rows["embeddings_refresh"]
    assert row["state"] == "upstream", row
    assert all(row[k] is None for k in
               ("started_at", "ended_at", "queued_at", "duration_s",
                "ran_at", "percent", "message")), row


def test_pruned_rows_are_inert_and_carry_their_reason(stores, monkeypatch):
    """A skipped step says WHY, and adopts no work that ran outside the run."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    record = _record("video_map_refresh", kind="card", in_flight=False)
    record["steps"]["timelines_refresh"] = {
        "state": "pruned", "reason": "no video changed niche"}
    stores["seed"](record)
    # Ran during the window, but this run deliberately skipped it.
    stores["process_stats"]["timelines_refresh"] = {
        "last_run_end_time": T1, "last_run_duration": 99.0, "last_run_outcome": "Success",
    }

    rows = {row["step"]: row for row in
            ws._build_pipeline_step_view(pipeline_active=False)}

    row = rows["timelines_refresh"]
    assert row["state"] == "pruned", row
    assert row["reason"] == "no video changed niche"
    assert all(row[k] is None for k in
               ("started_at", "ended_at", "queued_at", "duration_s",
                "ran_at", "percent", "message")), row


def test_unplanned_rows_carry_no_timing_even_when_running_now(stores, monkeypatch):
    """A sessions run chained from a study save must not be drawn into the run."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["seed"](_record("recode_refresh_studies", kind="card"))
    stores["task_status"]["sessions_refresh"] = {
        "state": "running", "start_time": T1, "updated_at": T1,
        "progress": {"percent": 40, "message": "segmenting"},
    }

    rows = {row["step"]: row for row in
            ws._build_pipeline_step_view(pipeline_active=True)}

    row = rows["sessions_refresh"]
    assert row["state"] == "not_planned", row
    assert all(row[k] is None for k in
               ("started_at", "ended_at", "queued_at", "duration_s",
                "ran_at", "percent", "message")), row


def test_every_canonical_step_gets_a_row(stores, monkeypatch):
    """The chart's shape never moves with what a run happened to need."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["seed"](_record("recode_refresh_studies", kind="card", in_flight=False))

    view = ws._build_pipeline_step_view(pipeline_active=False)

    assert [row["step"] for row in view] == ["consolidate_enrichment"] + list(
        ws.PIPELINE_STEPS_ORDER)


def test_consolidate_only_run_marks_every_refresh_step_not_planned(stores, monkeypatch):
    """Refresh unticked: the steps were not requested, not "not needed"."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["seed"](_record("consolidate_enrichment", mode="consolidate_only",
                           in_flight=False))
    stores["process_stats"]["consolidate_enrichment"] = {
        "last_run_end_time": T1, "last_run_outcome": "Success",
    }

    view = ws._build_pipeline_step_view(pipeline_active=False)
    states = _states(view)

    assert states["consolidate_enrichment"] == "success"
    assert all(states[s] == "not_planned" for s in ws.PIPELINE_STEPS_ORDER), states
    # The renderer words these "not requested" rather than "not needed".
    assert all(row["plan_mode"] == "consolidate_only" for row in view)


def test_a_planned_step_outside_the_canonical_order_still_gets_a_row(stores, monkeypatch):
    """A run record is data — a planned step must never lose its row."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    record = _record("consolidate_enrichment", in_flight=False)
    record["steps"]["some_future_refresh"] = {"state": "planned"}
    stores["seed"](record)

    view = ws._build_pipeline_step_view(pipeline_active=False)

    assert [row["step"] for row in view] == (
        ["consolidate_enrichment"] + list(ws.PIPELINE_STEPS_ORDER)
        + ["some_future_refresh"])
    assert _states(view)["some_future_refresh"] != "not_planned"


def test_inert_rows_are_never_provisional(stores, monkeypatch):
    """A plan lists every step, so "provisional" and the inert states can't collide."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    stores["seed"](_record("video_map_refresh", kind="card", provisional=True))

    view = ws._build_pipeline_step_view(pipeline_active=True)

    assert not any(row["provisional"] for row in view
                   if row["state"] in ("not_planned", "upstream", "pruned"))


def test_run_header_names_its_origin(stores, monkeypatch):
    """The chart has to say where a run started, or every run reads as a consolidation."""
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    record = _record("video_map_refresh", kind="card")
    record["started_by"] = "patrik"
    stores["seed"](record)

    run = ws.refresh_run_view()

    assert run["origin"] == "video_map_refresh"
    assert run["origin_label"] == "Semantic map"
    assert run["origin_kind"] == "card"
    assert run["started_by"] == "patrik"
    assert run["in_flight"] is True
