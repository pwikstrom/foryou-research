"""Arming an Idle enrichment plan again restarts its walk, and stamps a run.

A finished plan's cursors sit wherever its walk ended — for a plan whose deep
dive used to skip every quiet day, at the oldest month. Re-arming with a
raised target (or a fixed planner) must start from the newest day again, or
``plan_cycle`` finds nothing and the plan goes straight back to Idle
(2026-09-08, tiktok_ddp_20260907T074212Z_f4b10005). Pausing and resuming keeps
the cursors, so a resume continues where it stopped.

Arming also stamps the run's starting line — when, and how many of the
collection's videos were annotated at that moment — which is the only thing
that lets the modal report how far a RUN has come rather than how far the
collection has. A resume keeps the run it already had.
"""

import pytest

import web_interface.routes.management.collections as routes
import web_interface.services.collection_enrichment as ce


@pytest.fixture
def arm(monkeypatch):
    from web_interface.fyp_data_hub import app

    saved: list[dict] = []
    world = {"existing": None, "floor": 0}
    monkeypatch.setattr(ce, "get_plan", lambda cid: world["existing"])
    monkeypatch.setattr(ce, "save_plan", lambda cid, patch: saved.append(patch))
    monkeypatch.setattr(ce, "progress",
                        lambda cid, entry=None: {"target_floor": world["floor"]})
    monkeypatch.setattr(routes, "_journal_plan_save", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_tick_now", lambda cid: {"ok": True})
    monkeypatch.setattr(routes, "_actor", lambda: "tester", raising=False)
    monkeypatch.setattr(routes.activity_log, "record", lambda **k: None)
    view = routes.save_collection_enrichment
    while hasattr(view, "__wrapped__"):
        view = view.__wrapped__

    def run(existing, payload, floor=0):
        world["existing"] = existing
        world["floor"] = floor
        saved.clear()
        with app.test_request_context(json=payload):
            view("c1")
        return saved[-1]

    return run


def test_rearming_an_idle_plan_resets_both_cursors(arm):
    idle = {"state": ce.STATE_DONE, "a_cursor": "2026-03", "b_cursor": "2026-03-05",
            "settings": dict(ce.DEFAULT_SETTINGS)}
    patch = arm(idle, {"state": "running"})
    assert patch["state"] == ce.STATE_RUNNING
    assert patch["a_cursor"] is None and patch["b_cursor"] is None


def test_resuming_a_paused_plan_keeps_its_cursors(arm):
    paused = {"state": ce.STATE_PAUSED, "a_cursor": "2026-03", "b_cursor": "2026-03-05",
              "settings": dict(ce.DEFAULT_SETTINGS)}
    patch = arm(paused, {"state": "running"})
    assert "a_cursor" not in patch and "b_cursor" not in patch


def test_saving_settings_on_an_idle_plan_does_not_touch_the_cursors(arm):
    idle = {"state": ce.STATE_DONE, "a_cursor": "2026-03", "b_cursor": "2026-03-05",
            "settings": dict(ce.DEFAULT_SETTINGS)}
    patch = arm(idle, {"settings": {"annotation_target": 300}})
    assert "a_cursor" not in patch and "state" not in patch


# --------------------------------------------------------------------------- #
# The run's starting line
# --------------------------------------------------------------------------- #

def test_arming_a_new_plan_stamps_the_runs_starting_line(arm):
    """The modal's run meter measures from here, so the count comes from the
    DATA (progress), not from the ledger's own spent_items — work done by hand
    or by another route counts toward the same target."""
    patch = arm(None, {"state": "running", "settings": {"annotation_target": 800}},
                floor=120)
    assert patch["run_start_annotated"] == 120
    assert patch["run_started_at"]


def test_arming_an_idle_plan_again_moves_the_starting_line(arm):
    """A raised target is a NEW run: measuring it from the old run's start
    would report a meter that begins near 100% and barely moves."""
    idle = {"state": ce.STATE_DONE, "run_start_annotated": 0,
            "settings": dict(ce.DEFAULT_SETTINGS)}
    patch = arm(idle, {"state": "running"}, floor=800)
    assert patch["run_start_annotated"] == 800


def test_resuming_a_paused_plan_keeps_the_starting_line(arm):
    """A pause is a break in one run, not the end of it."""
    paused = {"state": ce.STATE_PAUSED, "run_start_annotated": 120,
              "settings": dict(ce.DEFAULT_SETTINGS)}
    patch = arm(paused, {"state": "running"}, floor=400)
    assert "run_start_annotated" not in patch
    assert "run_started_at" not in patch


def test_a_settings_only_save_never_moves_the_starting_line(arm):
    """Settings now save themselves on every change; a run's start must not
    creep forward each time somebody nudges the target."""
    running = {"state": ce.STATE_RUNNING, "run_start_annotated": 120,
               "settings": dict(ce.DEFAULT_SETTINGS)}
    patch = arm(running, {"settings": {"annotation_target": 900}}, floor=400)
    assert "run_start_annotated" not in patch


def test_progress_reports_the_starting_line_it_was_given():
    """progress() passes the stamp through untouched — the panel needs both
    halves (when, and from what) and neither is derivable from the data."""
    entry = {"state": ce.STATE_RUNNING, "run_started_at": "2026-09-01T08:00:00+00:00",
             "run_start_annotated": 2400, "settings": dict(ce.DEFAULT_SETTINGS)}
    out = ce.progress("nope-no-such-collection", entry)
    assert out["run_started_at"] == "2026-09-01T08:00:00+00:00"
    assert out["run_start_annotated"] == 2400


def test_arming_again_forgets_where_the_last_run_ended(arm):
    """The finished run's end (what the modal's meter froze on) belongs to
    that run; a new run starts with no end yet."""
    idle = {"state": ce.STATE_DONE, "run_start_annotated": 0,
            "run_finished_at": "2026-09-09T09:15:39+00:00",
            "run_end_annotated": 4400, "run_end_target": 4400,
            "settings": dict(ce.DEFAULT_SETTINGS)}
    patch = arm(idle, {"state": "running"}, floor=4400)
    assert patch["run_finished_at"] is None
    assert patch["run_end_annotated"] is None and patch["run_end_target"] is None


def test_a_settings_only_save_keeps_the_last_runs_end(arm):
    idle = {"state": ce.STATE_DONE, "run_end_annotated": 4400, "run_end_target": 4400,
            "settings": dict(ce.DEFAULT_SETTINGS)}
    patch = arm(idle, {"settings": {"annotation_target": 6000}})
    assert "run_end_target" not in patch and "run_end_annotated" not in patch
