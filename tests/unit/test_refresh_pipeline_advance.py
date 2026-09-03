"""Driving a refresh run through the real advance code, one completion at a time.

The planner is tested in ``test_pipeline_order.py``; this exercises what the
task runner does with its decisions — what gets dispatched, what the run record
ends up saying, and the cases where a run must STOP rather than carry on:

* a run started from a worker card dispatches its dependents and finishes;
* a run whose upstream changed nothing dispatches nothing at all;
* a failed or cancelled step ends the run instead of advancing it onto inputs
  that were never rebuilt;
* a study save's own chain, which carries no run record, still advances and
  leaves the record alone;
* the weekly shadow verification, which shares the consolidate status key, never
  touches a run.

Usage:
    python -m pytest tests/unit/test_refresh_pipeline_advance.py
"""

from datetime import UTC, datetime

import pytest

import web_interface.process_manager as pm
import web_interface.routes.process_routes as pr
from web_interface.services import refresh_pipeline as rp


@pytest.fixture
def runner(monkeypatch):
    """An in-memory process_stats plus a recording Cloud Tasks dispatcher."""
    store: dict = {}
    live: dict = {}
    dispatched: list = []
    statuses: dict = {}

    def fake_load():
        live.clear()
        live.update({k: dict(v) if isinstance(v, dict) else v for k, v in store.items()})

    def fake_save():
        store.update({k: dict(v) if isinstance(v, dict) else v for k, v in live.items()})

    monkeypatch.setattr(pm, "process_stats", live)
    monkeypatch.setattr(pm, "load_process_stats", fake_load)
    monkeypatch.setattr(pm, "save_process_stats", fake_save)
    monkeypatch.setattr(pr, "process_stats", live)
    monkeypatch.setattr(pr, "load_process_stats", fake_load)
    monkeypatch.setattr(pr, "save_process_stats", fake_save)

    def fake_dispatch(name, args, **kwargs):
        dispatched.append((name, dict(args)))
        return True, "queued"

    # The advance imports the dispatcher from process_manager at call time.
    monkeypatch.setattr(pm, "_dispatch_cloud_task", fake_dispatch)
    monkeypatch.setattr(pr, "dispatch_deadline_for", lambda n, a: 1800)
    monkeypatch.setattr(pr, "stamp_task_status", lambda *a, **kw: None)
    monkeypatch.setattr(pr, "read_task_status", lambda n: statuses.get(n))
    monkeypatch.setattr(pr.run_logs, "open_run", lambda *a, **kw: None)
    monkeypatch.setattr(pr.run_logs, "new_run_id", lambda: "log-1")

    class Harness:
        dispatched = None
        statuses = None

        def finish(self, step, data=None, outcome="Success"):
            """Pretend the worker ran: write its stats entry as the runner does."""
            fake_load()
            end = datetime.now(UTC).isoformat()
            live[step] = {**(live.get(step) or {}), **(data or {}),
                          "last_run_end_time": end, "last_run_duration": 5.0,
                          "last_run_outcome": outcome}
            fake_save()
            statuses[step] = {
                "state": "completed" if outcome == "Success" else "failed",
                "updated_at": end,
            }

        def seed(self, origin, **kwargs):
            record = rp.plan_run(origin, **kwargs)
            rp.seed_run(record)
            return record

        def states(self):
            return {k: v.get("state") for k, v in (rp.load_run() or {})["steps"].items()}

        def args_for(self, step):
            for name, args in dispatched:
                if name == step:
                    return args
            return None

        def names(self):
            return [name for name, _ in dispatched]

    harness = Harness()
    harness.dispatched = dispatched
    harness.statuses = statuses
    return harness


def test_a_card_run_dispatches_its_dependents(runner):
    record = runner.seed("video_map_refresh", kind="card", started_by="patrik")
    runner.finish("video_map_refresh", {"map_niche_changed": 3120})

    pr._advance_refresh_run("video_map_refresh",
                            {"pipeline_run_id": record["run_id"],
                             "started_by": "patrik"}, "Success")

    assert runner.names() == ["recode_refresh_studies"]
    args = runner.args_for("recode_refresh_studies")
    assert args["pipeline_run_id"] == record["run_id"]
    # Attribution survives the hop, so the run log says who is behind it.
    assert args["started_by"] == "patrik (via video_map_refresh)"
    assert runner.states()["recode_refresh_studies"] == "dispatched"
    assert rp.run_in_flight() is True


def test_a_run_that_changed_nothing_dispatches_nothing(runner):
    record = runner.seed("video_map_refresh", kind="card", started_by="patrik")
    runner.finish("video_map_refresh", {"map_niche_changed": 0, "map_cold_start": False})

    pr._advance_refresh_run("video_map_refresh",
                            {"pipeline_run_id": record["run_id"]}, "Success")

    assert runner.names() == []
    run = rp.load_run()
    assert run["in_flight"] is False
    assert "Nothing downstream needed refreshing" in run["summary"]
    # Every skipped step says why, so the chart reads as a decision, not a gap.
    assert all(step.get("reason") for name, step in run["steps"].items()
               if step["state"] == "pruned")


def test_the_fork_dispatches_every_remaining_leaf_at_once(runner):
    record = runner.seed("video_map_refresh", kind="card", started_by="patrik")
    record["steps"]["recode_refresh_studies"] = {"state": "dispatched"}
    rp.seed_run(record)
    runner.finish("video_map_refresh", {"map_niche_changed": 12})
    runner.finish("recode_refresh_studies", {"studies_changed": ["s1"]})

    pr._advance_refresh_run("recode_refresh_studies",
                            {"pipeline_run_id": record["run_id"]}, "Success")

    assert set(runner.names()) == set(rp.LEAVES)
    run = rp.load_run()
    assert run["fork_at"] == "recode_refresh_studies"
    assert set(run["fork"]["leaves"]) == set(rp.LEAVES)
    # Every leaf carries the same fork timestamp, so none can be tripped by a
    # sibling's stale terminal status from an earlier run.
    stamps = {runner.args_for(l)["pipeline_fork_ts"] for l in rp.LEAVES}
    assert len(stamps) == 1


def test_a_failed_step_stops_the_run(runner):
    record = runner.seed("embeddings_refresh", kind="card", started_by="patrik")
    runner.finish("embeddings_refresh", {}, outcome="Fail")

    pr._advance_refresh_run("embeddings_refresh",
                            {"pipeline_run_id": record["run_id"]}, "Fail")

    run = rp.load_run()
    assert runner.names() == []
    assert run["in_flight"] is False and run["partial"] is True
    assert run["failed_at"] == "embeddings_refresh"
    # Planned-and-never-ran is "skipped", which is a different statement from
    # the deliberate "pruned".
    assert runner.states()["video_map_refresh"] == "skipped"


def test_a_cancelled_step_stops_the_run(runner):
    """Cancelling a step must not advance the run onto caches it never rebuilt."""
    record = runner.seed("video_map_refresh", kind="card", started_by="patrik")
    runner.finish("video_map_refresh", {"map_niche_changed": 500})

    pr._advance_refresh_run("video_map_refresh",
                            {"pipeline_run_id": record["run_id"]},
                            "Success", cancelled=True)

    run = rp.load_run()
    assert runner.names() == []
    assert run["in_flight"] is False and run["partial"] is True
    assert run["reason"] == "cancelled"


def test_a_stale_run_id_never_advances_a_newer_run(runner):
    """A late completion from a replaced run must not stamp the current one."""
    old = runner.seed("video_map_refresh", kind="card")
    new = runner.seed("consolidate_enrichment", kind="consolidate")
    runner.finish("video_map_refresh", {"map_niche_changed": 999})

    pr._advance_refresh_run("video_map_refresh",
                            {"pipeline_run_id": old["run_id"]}, "Success")

    assert runner.names() == []
    assert rp.load_run()["run_id"] == new["run_id"]


def test_a_chain_without_a_run_record_still_advances(runner):
    """The study save's own sessions chain predates the run record and stays."""
    record = runner.seed("consolidate_enrichment", kind="consolidate")

    pr._advance_refresh_run("study_refresh", {"pipeline_remaining": [
        {"task": "sessions_refresh",
         "task_args": {"stale_only": True, "skip_if_busy": True}}]}, "Success")

    assert runner.names() == ["sessions_refresh"]
    args = runner.args_for("sessions_refresh")
    assert args["stale_only"] is True and "pipeline_run_id" not in args
    # And it leaves the run record alone — it is not part of a run.
    assert rp.load_run()["run_id"] == record["run_id"]
    assert runner.states()["sessions_refresh"] == "planned"


def test_a_leaf_completion_runs_the_barrier_not_the_planner(runner):
    record = runner.seed("consolidate_enrichment", kind="consolidate")
    fork_ts = datetime.now(UTC).isoformat()
    leaves = ["meta_refresh_groups", "pca_refresh"]
    runner.finish("meta_refresh_groups", {})
    # pca has not finished, so the barrier must wait and dispatch nothing.
    pr._advance_refresh_run("meta_refresh_groups", {
        "pipeline_run_id": record["run_id"],
        "pipeline_leaves": leaves,
        "pipeline_fork_ts": fork_ts,
    }, "Success")

    assert runner.names() == []
    assert rp.load_run()["in_flight"] is True


def test_the_shadow_verification_never_touches_a_run(runner):
    """It shares the consolidate status key but consolidates nothing."""
    record = runner.seed("consolidate_enrichment", kind="consolidate")
    runner.finish("consolidate_enrichment", {})

    pr._advance_refresh_run("consolidate_enrichment",
                            {"verify_consolidation": True}, "Success")

    assert runner.names() == []
    assert rp.load_run()["run_id"] == record["run_id"]
    assert runner.states()["embeddings_refresh"] == "planned"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_a_consolidation_started_without_auto_refresh_plans_no_cascade(runner):
    """The plain-API door must not cascade when the caller did not ask it to.

    ``/api/start/consolidate_enrichment`` plans ``consolidate_only`` unless
    ``auto_refresh`` is set, so a consolidation started that way records its
    impact as deferred debt (as it always has) instead of quietly rebuilding
    every cache behind the operator's back.
    """
    record = runner.seed("consolidate_enrichment", kind="card",
                         mode="consolidate_only")
    # The consolidation still writes its impact — the run just does not act on it.
    runner.finish("consolidate_enrichment", {"consolidation_impact": {
        "new_annotation_item_count": 900, "affected_study_names": ["s1"],
        "affected_collection_ids": ["c1"]}})

    pr._advance_refresh_run("consolidate_enrichment",
                            {"pipeline_run_id": record["run_id"]}, "Success")

    assert runner.names() == []
    assert all(state == "not_planned"
               for step, state in runner.states().items()
               if step != "consolidate_enrichment")
    assert rp.load_run()["in_flight"] is False


def test_an_outstanding_fan_out_is_not_declared_abandoned(runner, monkeypatch):
    """The 60s abandoned-run sweep must not race the fork's own 600s grace.

    A leaf dropped by a 429 is redelivered minutes later, and
    ``resolve_forked_pipeline`` is what owns that window. Between the fork and
    the leaf booting, no step is "running" and the record has not been written
    for over a minute — exactly the shape the sweep looks for — so the fork has
    to veto it, or a run that is about to finish normally is failed instead.
    """
    import inspect

    from web_interface.routes.management import enrichment as en

    src = inspect.getsource(en.get_enrichment_stats)
    i = src.index("flag_in_flight and not any_step_running")
    line = src[i:src.index("\n", i)]
    assert 'refresh_run.get("fork")' in line, (
        "the abandoned-run sweep must stand down while a fan-out is outstanding")
