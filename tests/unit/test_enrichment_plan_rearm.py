"""Arming an Idle enrichment plan again restarts its walk.

A finished plan's cursors sit wherever its walk ended — for a plan whose deep
dive used to skip every quiet day, at the oldest month. Re-arming with a
raised target (or a fixed planner) must start from the newest day again, or
``plan_cycle`` finds nothing and the plan goes straight back to Idle
(2026-09-08, tiktok_ddp_20260907T074212Z_f4b10005). Pausing and resuming keeps
the cursors, so a resume continues where it stopped.
"""

import pytest

import web_interface.routes.management.collections as routes
import web_interface.services.collection_enrichment as ce


@pytest.fixture
def arm(monkeypatch):
    from web_interface.fyp_data_hub import app

    saved: list[dict] = []
    world = {"existing": None}
    monkeypatch.setattr(ce, "get_plan", lambda cid: world["existing"])
    monkeypatch.setattr(ce, "save_plan", lambda cid, patch: saved.append(patch))
    monkeypatch.setattr(ce, "progress", lambda cid, entry=None: {})
    monkeypatch.setattr(routes, "_journal_plan_save", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_tick_now", lambda cid: {"ok": True})
    monkeypatch.setattr(routes, "_actor", lambda: "tester", raising=False)
    monkeypatch.setattr(routes.activity_log, "record", lambda **k: None)
    view = routes.save_collection_enrichment
    while hasattr(view, "__wrapped__"):
        view = view.__wrapped__

    def run(existing, payload):
        world["existing"] = existing
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
