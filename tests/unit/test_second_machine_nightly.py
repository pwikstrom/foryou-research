"""The nightly Consolidate & Refresh for a second machine's results, and the gating by the admin switch."""

from datetime import UTC, datetime

import pytest

from web_interface.workers import run_enrichment_supervisor as sup


class _Reporter:
    def __init__(self):
        self.lines = []

    def log(self, line):
        self.lines.append(line)


@pytest.fixture
def night(monkeypatch):
    """Everything in place for the nightly run; each test breaks one condition."""
    state = {
        "meta": {},
        "started": [],
        "blocking": [],
        "on": True,
        "consolidated": "2026-11-25T03:00:00+10:00",
    }
    monkeypatch.setattr(
        "fyp.annotation.backends.settings.second_machine_annotates", lambda: state["on"]
    )
    monkeypatch.setattr(
        "web_interface.services.annotation_backlog.worker_status",
        lambda: {"host": "mac", "last_chunk_at": "2026-11-25T12:00:00+00:00"},
    )
    monkeypatch.setattr(
        "web_interface.services.worker_status.workers_blocking_consolidate",
        lambda: state["blocking"],
    )
    monkeypatch.setattr("web_interface.services.refresh_pipeline.run_in_flight", lambda: False)
    monkeypatch.setattr("web_interface.tasks.process_manager.load_process_stats", lambda: None)
    monkeypatch.setattr(
        "web_interface.tasks.process_manager.process_stats",
        {"consolidate_enrichment": {"last_consolidation": state["consolidated"]}},
    )
    monkeypatch.setattr(sup, "_pipeline_in_flight", lambda: False)
    monkeypatch.setattr(
        sup.ce, "get_meta", lambda key, default=None: state["meta"].get(key, default)
    )
    monkeypatch.setattr(
        sup.ce, "set_meta", lambda key, value: state["meta"].__setitem__(key, value)
    )
    monkeypatch.setattr(sup.journal, "record", lambda *a, **k: None)
    monkeypatch.setattr(
        sup,
        "_start_consolidation",
        lambda task_args, **kw: (state["started"].append((task_args, kw)), (True, "started"))[1],
    )
    return state


# 16:30 UTC is 02:30 in Brisbane, inside the window; 02:00 UTC is midday there.
IN_WINDOW = datetime(2026, 11, 25, 16, 30, tzinfo=UTC)
MIDDAY = datetime(2026, 11, 25, 2, 0, tzinfo=UTC)


def test_starts_once_a_night_with_a_refresh(night):
    out = sup._second_machine_nightly(_Reporter(), now=IN_WINDOW)
    assert out and out["started"] is True
    task_args, kw = night["started"][0]
    assert task_args == {"auto_refresh": True} and kw["mode"] == "refresh"
    # The same night's later heartbeats do nothing more.
    assert sup._second_machine_nightly(_Reporter(), now=IN_WINDOW) is None
    assert len(night["started"]) == 1


def test_does_nothing_when_switched_off_or_outside_the_window(night):
    assert sup._second_machine_nightly(_Reporter(), now=MIDDAY) is None
    night["on"] = False
    assert sup._second_machine_nightly(_Reporter(), now=IN_WINDOW) is None
    assert night["started"] == []


def test_does_nothing_without_new_results(night, monkeypatch):
    monkeypatch.setattr(
        "web_interface.tasks.process_manager.process_stats",
        {"consolidate_enrichment": {"last_consolidation": "2026-11-26T01:00:00+10:00"}},
    )
    assert sup._second_machine_nightly(_Reporter(), now=IN_WINDOW) is None
    assert night["started"] == []


def test_waits_for_running_workers_and_tries_again(night):
    night["blocking"] = ["queue_annotator"]
    reporter = _Reporter()
    assert sup._second_machine_nightly(reporter, now=IN_WINDOW) is None
    assert night["started"] == [] and "waiting for queue_annotator" in reporter.lines[0]
    night["blocking"] = []
    assert sup._second_machine_nightly(_Reporter(), now=IN_WINDOW)["started"] is True
