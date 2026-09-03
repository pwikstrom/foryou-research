"""The shadow verification must not pose as the last consolidation.

It runs under the consolidate_enrichment status key so the enrichment
supervisor's gate serialises it against real consolidations — but on
2026-09-03 that made it look like consolidation had "fired twice": a 13-minute
run on the Consolidate card straight after a 42-second one, and its duration
and timestamp overwrote the card's "Last: … OK" line. Worse, a successful
verification refreshed ``last_success``, which the staleness checks read as
"data consolidated at this time".

``_merge_run_stats`` now writes a verify run to ``last_verify_*`` and leaves
every last-run field alone. Everything else is unchanged.

Usage:
    python -m pytest tests/unit/test_shadow_verify_stats.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from web_interface.routes import process_routes as pr

T0 = datetime(2026, 9, 3, 0, 23, 20, tzinfo=UTC)
T1 = datetime(2026, 9, 3, 0, 37, 0, tzinfo=UTC)

EXISTING = {
    "last_success": T0.isoformat(),
    "last_run_end_time": T0.isoformat(),
    "last_run_duration": 42.0,
    "last_run_outcome": "Success",
    "last_run_study": None,
    "auto_armed": False,
}


def test_a_real_consolidation_refreshes_the_last_run_fields():
    out = pr._merge_run_stats(EXISTING, {"impact": {"n": 28}}, name="consolidate_enrichment",
                              task_args={"auto_refresh": True}, outcome="Success",
                              end_time=T1, duration=41.8, study_name=None)
    assert out["last_run_end_time"] == T1.isoformat()
    assert out["last_run_duration"] == 41.8
    assert out["last_success"] == T1.isoformat()
    assert out["impact"] == {"n": 28}
    assert out["auto_armed"] is False, "unrelated stored fields survive"


def test_a_shadow_verification_leaves_the_last_run_alone():
    out = pr._merge_run_stats(EXISTING, {"shadow_check": {"ok": True}},
                              name="consolidate_enrichment",
                              task_args={"verify_consolidation": True}, outcome="Success",
                              end_time=T1, duration=811.0, study_name=None)
    for k in ("last_success", "last_run_end_time", "last_run_duration", "last_run_outcome"):
        assert out[k] == EXISTING[k], f"{k} was overwritten by the shadow verification"
    assert out["last_verify_end_time"] == T1.isoformat()
    assert out["last_verify_duration"] == 811.0
    assert out["last_verify_outcome"] == "Success"
    assert out["shadow_check"] == {"ok": True}, "its emitted result is still stored"


def test_a_failed_verification_does_not_dent_last_success_either_way():
    out = pr._merge_run_stats(EXISTING, {}, name="consolidate_enrichment",
                              task_args={"verify_consolidation": True}, outcome="Fail",
                              end_time=T1, duration=100.0, study_name=None)
    assert out["last_success"] == EXISTING["last_success"]
    assert out["last_run_outcome"] == "Success"
    assert out["last_verify_outcome"] == "Fail"


def test_a_failed_consolidation_keeps_the_previous_last_success():
    out = pr._merge_run_stats(EXISTING, {}, name="consolidate_enrichment",
                              task_args={}, outcome="Fail", end_time=T1, duration=5.0,
                              study_name=None)
    assert out["last_success"] == EXISTING["last_success"]
    assert out["last_run_outcome"] == "Fail"
    assert out["last_run_end_time"] == T1.isoformat()


def test_other_tasks_ignore_the_flag():
    """Only the consolidate key carries a verify mode."""
    out = pr._merge_run_stats({}, {}, name="pca_refresh", task_args={"verify_consolidation": True},
                              outcome="Success", end_time=T1, duration=9.0, study_name="s")
    assert out["last_run_outcome"] == "Success" and out["last_run_study"] == "s"
    assert "last_verify_outcome" not in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
