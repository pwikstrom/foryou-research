"""timelines_refresh batches run on a forked process pool with compute-only children.

2026-09-03: the batch loop was a ThreadPoolExecutor over pandas object-column
work, which holds the GIL — 8 threads ran 8 collections in 27 s against 3.5 s
for one (1.1×), so "Using 10 parallel workers" was effectively serial, and a
15-collection batch took 786 s. Forked processes ran the same 8 in 4.6 s.

What this file pins:
  * children only compute (`compute_collection_timeline`); the parent does
    every storage write (`write_collection_timeline`) — a forked child must not
    use storage clients inherited from the parent;
  * the biggest collection is submitted first (it alone sets the batch floor);
  * a collection whose slice is missing takes the serial path;
  * a pool failure degrades to the serial path and still finishes the batch;
  * a real forked pool returns results and children see the parent's modules.

Usage:
    python -m pytest tests/unit/test_timelines_process_pool.py
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from concurrent.futures import Future

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import pytest

import web_interface.run_timelines_refresh as rtr


class _Reporter:
    def __init__(self):
        self.lines: list[str] = []
        self.progress: list[str] = []

    def log(self, msg):
        self.lines.append(str(msg))

    def update_progress(self, pct, msg, **kw):
        self.progress.append(str(msg))

    def check_cancelled(self):
        return False


def _slices():
    return {
        "small": pd.DataFrame({"x": range(10)}),
        "big": pd.DataFrame({"x": range(1000)}),
        "mid": pd.DataFrame({"x": range(100)}),
        "noslice": None,
    }


@pytest.fixture
def quiet(monkeypatch):
    """No storage: coverage lookup is identity, writes are recorded, warm-up is a no-op."""
    writes: list[tuple[str, int, list, dict | None]] = []
    monkeypatch.setattr(rtr, "_warm_worker_imports", lambda: None)
    monkeypatch.setattr(rtr, "_vars_with_prior_coverage", lambda cid, vv: list(vv))
    monkeypatch.setattr(rtr, "write_collection_timeline",
                        lambda cid, agg, vv, an: writes.append((cid, len(agg), vv, an)))
    monkeypatch.setattr(rtr, "compute_collection_timeline",
                        lambda cid, sl, vv, fe: (pd.DataFrame({"period": ["d"] * len(sl)}), {"cid": cid}))
    return writes


class _InlineExecutor:
    """Records submission order and runs the work inline — deterministic."""
    submitted: list[str] = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, cid):
        _InlineExecutor.submitted.append(cid)
        f: Future = Future()
        f.set_result(fn(cid))
        return f

    def shutdown(self, **kw):
        pass


def test_children_compute_and_the_parent_writes_biggest_first(monkeypatch, quiet):
    _InlineExecutor.submitted = []
    monkeypatch.setattr(rtr, "ProcessPoolExecutor", _InlineExecutor)
    monkeypatch.setattr(rtr.multiprocessing, "get_all_start_methods", lambda: ["fork"])
    monkeypatch.setattr(rtr.os, "cpu_count", lambda: 8)
    reporter = _Reporter()
    slices = _slices()

    # The no-slice collection would load from storage on the serial path;
    # give process_one_collection a stand-in so the test stays offline.
    serial_seen: list[str] = []
    monkeypatch.setattr(rtr, "process_one_collection",
                        lambda cid, sl, vv, fe: serial_seen.append(cid) or True)

    n = rtr._process_batch(reporter, list(slices), slices, ["v1"], {"big": "2025-01-01"}, 0, 4)

    assert _InlineExecutor.submitted == ["big", "mid", "small"], "biggest first"
    assert serial_seen == ["noslice"], "a missing slice takes the serial path"
    assert n == 4
    written = {w[0]: w for w in quiet}
    assert set(written) == {"big", "mid", "small"}
    assert written["big"][1] == 1000 and written["big"][3] == {"cid": "big"}
    assert any("worker processes" in ln for ln in reporter.lines)
    batch = [ln for ln in reporter.lines if "timelines_batch" in ln]
    assert batch and "slowest=" in batch[0] and "workers=3" in batch[0]
    assert sum("timelines_collection" in ln for ln in reporter.lines) == 4


def test_pool_failure_falls_back_to_serial(monkeypatch, quiet):
    class _Broken(_InlineExecutor):
        def submit(self, fn, cid):
            raise OSError("fork refused")

    monkeypatch.setattr(rtr, "ProcessPoolExecutor", _Broken)
    monkeypatch.setattr(rtr.multiprocessing, "get_all_start_methods", lambda: ["fork"])
    monkeypatch.setattr(rtr.os, "cpu_count", lambda: 8)
    reporter = _Reporter()
    slices = {k: v for k, v in _slices().items() if v is not None}

    n = rtr._process_batch(reporter, list(slices), slices, ["v1"], {}, 0, 3)

    assert n == 3
    assert any("worker pool failed" in ln and "serially" in ln for ln in reporter.lines)
    assert {w[0] for w in quiet} == set(slices)
    assert not rtr._FORK_CTX, "the fork context is cleared after the pool"


def test_single_worker_never_builds_a_pool(monkeypatch, quiet):
    monkeypatch.setattr(rtr, "ProcessPoolExecutor", _Broken_if_used := type(
        "X", (), {"__init__": lambda self, *a, **k: pytest.fail("pool built for one worker")}))
    monkeypatch.setattr(rtr.os, "cpu_count", lambda: 1)
    reporter = _Reporter()
    slices = {"only": pd.DataFrame({"x": range(5)})}
    assert rtr._process_batch(reporter, ["only"], slices, ["v1"], {}, 0, 1) == 1
    assert not any("worker processes" in ln for ln in reporter.lines)


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(),
                    reason="fork start method unavailable")
def test_real_forked_pool_round_trips_results(monkeypatch, quiet):
    """Children inherit the parent's (monkeypatched) modules and return frames."""
    monkeypatch.setattr(rtr.os, "cpu_count", lambda: 4)
    reporter = _Reporter()
    slices = {k: v for k, v in _slices().items() if v is not None}

    n = rtr._process_batch(reporter, list(slices), slices, ["v1"], {}, 0, 3)

    assert n == 3
    assert {w[0] for w in quiet} == set(slices)
    assert not any("worker pool failed" in ln for ln in reporter.lines)
    assert any("workers=3" in ln for ln in reporter.lines if "timelines_batch" in ln)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
