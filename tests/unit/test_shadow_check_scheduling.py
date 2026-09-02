"""The weekly shadow verification must run once per interval, not once per delivery.

2026-09-02 prod: one scheduled check ran FIVE times (772-816 s each, two of
them overlapping on separate instances) — ~66 minutes of an 8-vCPU runner.
Cause: consolidate_enrichment was missing from
``process_manager._LONG_RUNNING_DEADLINES``, so Cloud Tasks applied its 600 s
default; every attempt answered 200 after the queue had already given up and
re-delivered. The deadline is the fix (see test_dispatch_deadlines.py); this
file pins the second line of defence, so a re-delivery from ANY cause — a
dispatch failure, a queue replay — cannot repeat a check that just passed.

Both the scheduler (``_maybe_schedule_shadow_check``) and the worker itself
(``_run_shadow_verification``) read the age through ``_shadow_check_age_days``,
so the two can never disagree about what "due" means.

Usage:
    python -m pytest tests/unit/test_shadow_check_scheduling.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

import web_interface.run_consolidate_enrichment as rce


class _Reporter:
    """Captures the log lines the worker emits."""

    def __init__(self):
        self.lines: list[str] = []
        self.progress: list[tuple[int, str]] = []

    def log(self, msg):
        self.lines.append(str(msg))

    def update_progress(self, pct, msg, **kw):
        self.progress.append((pct, str(msg)))

    def emit_data(self, payload):
        pass


@pytest.fixture
def no_verification(monkeypatch):
    """Fail loudly if the expensive verification is entered."""
    calls: list[int] = []

    import fyp.organize_datasets as od

    def _boom(*a, **k):
        calls.append(1)
        return {"ok": True, "mismatches": {}}

    monkeypatch.setattr(od, "verify_consolidation_equivalence", _boom)
    return calls


def test_a_fresh_check_is_not_repeated(monkeypatch, no_verification):
    """A re-delivered task whose check passed an hour ago must do nothing."""
    monkeypatch.setattr(rce, "_shadow_check_age_days", lambda: 1.0 / 24.0)
    reporter = _Reporter()

    assert rce._run_shadow_verification(reporter) is None
    assert not no_verification, (
        "a re-delivery re-ran the full corpus rebuild; the age guard is not "
        "in front of verify_consolidation_equivalence")
    assert any("skipped" in line for line in reporter.lines), reporter.lines


def test_a_due_check_runs(monkeypatch, no_verification):
    """Past the interval, the check goes ahead."""
    monkeypatch.setattr(rce, "_shadow_check_age_days",
                        lambda: rce._SHADOW_CHECK_INTERVAL_DAYS + 0.5)
    reporter = _Reporter()

    rce._run_shadow_verification(reporter)
    assert no_verification, "a due check did not run"


def test_no_marker_means_due(monkeypatch, no_verification):
    """No check on record must run one, not skip it."""
    monkeypatch.setattr(rce, "_shadow_check_age_days", lambda: None)
    reporter = _Reporter()

    rce._run_shadow_verification(reporter)
    assert no_verification, "a first-ever check was skipped"


def test_unreadable_marker_reads_as_due(monkeypatch):
    """The age helper never raises; a broken marker must not skip the check."""
    import fyp.data_io as data_io

    def _boom(*a, **k):
        raise OSError("bucket unreachable")

    monkeypatch.setattr(data_io, "exists", _boom)
    assert rce._shadow_check_age_days() is None


def test_scheduler_and_worker_share_the_age_source():
    """One definition of 'due', so the two sides cannot drift apart."""
    import inspect

    src = inspect.getsource(rce._maybe_schedule_shadow_check)
    assert "_shadow_check_age_days" in src, (
        "the scheduler re-implements the age check; it must call the shared "
        "helper the worker uses")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
