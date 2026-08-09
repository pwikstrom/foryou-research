"""Verify the durable process-log store (web_interface/run_logs.py).

Covers: the single-timestamping rule (and its no-double-stamp guard), the
per-process run ring and its line caps, the incremental ``since`` cursor the
log modal polls with, path-traversal rejection on status keys, and the
non-raising contract — a bookkeeping failure must never propagate into a task.
"""

import json
import logging
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from web_interface import run_logs


def _fake_io(store: dict, fail: bool = False):
    """A data_io stand-in backed by an in-memory dict keyed on filename."""

    class FakeIO:
        @staticmethod
        def exists(storage_location="cache", filename="", verbose=False):
            if fail:
                raise OSError("storage down")
            return filename in store

        @staticmethod
        def load_json(storage_location="cache", filename="", verbose=False):
            if fail:
                raise OSError("storage down")
            return json.loads(store[filename])

        @staticmethod
        def remove(storage_location="cache", filename="", verbose=False):
            if fail:
                raise OSError("storage down")
            del store[filename]

        @staticmethod
        def update_json(storage_location="cache", filename="", mutate=None,
                        default=None, max_retries=6, verbose=False):
            if fail:
                raise OSError("storage down")
            current = json.loads(store[filename]) if filename in store else default
            result = mutate(current)
            if result is not None:
                store[filename] = json.dumps(result)
            return result

    return FakeIO


def _runs(store: dict, key: str = "demo_proc") -> list:
    return json.loads(store[f"proc_logs/{key}.json"])["runs"]


@pytest.fixture(autouse=True)
def _isolate_state():
    """Reset the module's per-key buffers between tests."""
    with run_logs._states_lock:
        run_logs._states.clear()
    yield
    with run_logs._states_lock:
        for state in run_logs._states.values():
            run_logs._stop_flusher(state)
        run_logs._states.clear()






def test_append_stamps_each_line_exactly_once():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc", started_by="patrik", mode="subprocess")
        run_logs.append("demo_proc", "first line")
        # A line that already carries a stamp must not get a second one — this
        # is what keeps subprocess output (reporter.log -> print -> parent)
        # from being stamped by both ends of the pipe.
        run_logs.append("demo_proc", "[09:15:00] pre-stamped line")
        run_logs.flush("demo_proc")

    lines = _runs(store)[0]["lines"]
    body = [ln for ln in lines if "Run started" not in ln and "Started by" not in ln]
    assert len(body) == 2
    assert run_logs._STAMP_RE.match(body[0])
    assert body[0].endswith("first line")
    assert body[1] == "[09:15:00] pre-stamped line"






def test_banner_records_who_started_the_run_and_its_args():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc", started_by="patrik@example.com",
                          task_args={"batch_size": 500, "platform": "youtube",
                                     "log_run_id": "hidden", "started_by": "hidden"},
                          mode="cloud")

    lines = _runs(store)[0]["lines"]
    assert any("Started by patrik@example.com" in ln for ln in lines)
    args_line = next(ln for ln in lines if "Args:" in ln)
    assert "batch_size=500" in args_line and "platform=youtube" in args_line
    # Plumbing keys are noise in a banner.
    assert "log_run_id" not in args_line and "started_by=hidden" not in args_line


def test_banner_falls_back_to_system_when_unattributed():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
    assert any("Started by system" in ln for ln in _runs(store)[0]["lines"])






def test_run_ring_caps_at_max_runs_and_archives_the_previous_run():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        for i in range(run_logs.MAX_RUNS + 3):
            run_logs.open_run("demo_proc", started_by=f"user{i}")
            run_logs.append("demo_proc", f"work {i}")
            run_logs.finalize("demo_proc", run_logs.STATE_COMPLETED)

    runs = _runs(store)
    assert len(runs) == run_logs.MAX_RUNS
    # Oldest surviving run is the (n+3 - MAX_RUNS)th, newest is last.
    assert runs[-1]["started_by"] == f"user{run_logs.MAX_RUNS + 2}"
    assert all(r["state"] == run_logs.STATE_COMPLETED for r in runs)


def test_opening_a_run_marks_an_abandoned_predecessor_interrupted():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        run_logs.open_run("demo_proc")

    runs = _runs(store)
    assert runs[0]["state"] == run_logs.STATE_INTERRUPTED
    assert runs[0]["ended_at"]
    assert runs[1]["state"] == run_logs.STATE_RUNNING


def test_archived_runs_are_trimmed_harder_than_the_live_run():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        for i in range(run_logs.MAX_LINES_ARCHIVED + 50):
            run_logs.append("demo_proc", f"line {i}")
        run_logs.flush("demo_proc")
        live_len = len(_runs(store)[0]["lines"])
        run_logs.open_run("demo_proc")

    archived = _runs(store)[0]
    assert live_len > run_logs.MAX_LINES_ARCHIVED
    assert len(archived["lines"]) == run_logs.MAX_LINES_ARCHIVED
    assert archived["dropped"] > 0


def test_live_run_lines_are_capped_and_count_what_was_dropped():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        for i in range(run_logs.MAX_LINES_CURRENT + 40):
            run_logs.append("demo_proc", f"line {i}")
        run_logs.flush("demo_proc")

    run = _runs(store)[0]
    assert len(run["lines"]) == run_logs.MAX_LINES_CURRENT
    assert run["dropped"] > 0
    assert run["lines"][-1].endswith(f"line {run_logs.MAX_LINES_CURRENT + 39}")


def test_overlong_lines_are_truncated():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        run_logs.append("demo_proc", "x" * (run_logs.MAX_LINE_CHARS + 500))
        run_logs.flush("demo_proc")

    assert _runs(store)[0]["lines"][-1].endswith("… (truncated)")






def test_attach_run_adopts_an_open_run_instead_of_starting_a_second_one():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_id = run_logs.open_run("demo_proc", started_by="patrik")
        run_logs.detach("demo_proc")          # chain hop: this link is done
        adopted = run_logs.attach_run("demo_proc", run_id=run_id)
        run_logs.append("demo_proc", "second batch")
        run_logs.flush("demo_proc")

    assert adopted == run_id
    runs = _runs(store)
    assert len(runs) == 1, "a self-chain must stay one continuous run"
    assert any("second batch" in ln for ln in runs[0]["lines"])


def test_attach_run_opens_a_new_run_when_the_id_is_stale():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        run_logs.finalize("demo_proc", run_logs.STATE_COMPLETED)
        run_logs.attach_run("demo_proc", run_id="20200101T000000-deadbe")

    runs = _runs(store)
    assert len(runs) == 2
    assert runs[-1]["state"] == run_logs.STATE_RUNNING


def test_detach_flushes_pending_lines():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        run_logs.append("demo_proc", "trailing line")
        run_logs.detach("demo_proc")

    # Before this fix a chain hop dropped everything logged since the last
    # throttled write.
    assert any("trailing line" in ln for ln in _runs(store)[0]["lines"])
    assert _runs(store)[0]["state"] == run_logs.STATE_RUNNING


def test_finalize_writes_a_footer_and_a_terminal_state():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        run_logs.finalize("demo_proc", run_logs.STATE_FAILED)

    run = _runs(store)[0]
    assert run["state"] == run_logs.STATE_FAILED
    assert run["ended_at"]
    assert any("Run failed" in ln for ln in run["lines"])






def test_read_since_cursor_returns_only_new_lines():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        run_logs.append("demo_proc", "one")
        run_logs.flush("demo_proc")

        first = run_logs.read("demo_proc")
        assert first["reset"] is True
        assert any("one" in ln for ln in first["lines"])

        # Nothing new yet.
        assert run_logs.read("demo_proc", since=first["next_since"])["lines"] == []

        run_logs.append("demo_proc", "two")
        run_logs.flush("demo_proc")
        second = run_logs.read("demo_proc", since=first["next_since"])

    assert len(second["lines"]) == 1
    assert second["lines"][0].endswith("two")
    assert second["reset"] is False


def test_read_resets_when_the_cursor_outran_the_trim_window():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        run_logs.append("demo_proc", "seed")
        run_logs.flush("demo_proc")
        cursor = run_logs.read("demo_proc")["next_since"]

        for i in range(run_logs.MAX_LINES_CURRENT + 100):
            run_logs.append("demo_proc", f"line {i}")
        run_logs.flush("demo_proc")
        out = run_logs.read("demo_proc", since=cursor)

    # The client's next line was trimmed away, so it must replace, not append.
    assert out["reset"] is True
    assert len(out["lines"]) == run_logs.MAX_LINES_CURRENT


def test_read_returns_a_named_previous_run_and_the_picker_list():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        first_id = run_logs.open_run("demo_proc", started_by="alice")
        run_logs.append("demo_proc", "old work")
        run_logs.finalize("demo_proc", run_logs.STATE_COMPLETED)
        run_logs.open_run("demo_proc", started_by="bob")
        run_logs.append("demo_proc", "new work")
        run_logs.flush("demo_proc")

        newest = run_logs.read("demo_proc")
        older = run_logs.read("demo_proc", run_id=first_id)

    assert any("new work" in ln for ln in newest["lines"])
    assert any("old work" in ln for ln in older["lines"])
    assert older["run"]["started_by"] == "alice"
    # Newest first, and never carrying the line bodies.
    assert [r["started_by"] for r in newest["runs"]] == ["bob", "alice"]
    assert "lines" not in newest["runs"][0]
    assert newest["runs"][0]["line_count"] > 0


def test_read_of_an_unknown_process_is_empty_not_an_error():
    with patch.object(run_logs, "data_io", _fake_io({})):
        out = run_logs.read("never_ran")
    assert out == {"lines": [], "next_since": 0, "reset": True, "run": None, "runs": []}






def test_unsafe_keys_are_rejected_everywhere():
    for bad in ("../secrets", "a/b", "with space", "", "key$"):
        assert not run_logs.valid_key(bad)
        with pytest.raises(ValueError):
            run_logs.log_filename(bad)

    for good in ("pca_refresh", "study_refresh__my-study", "queue_scraper_youtube"):
        assert run_logs.valid_key(good)
        assert run_logs.log_filename(good) == f"proc_logs/{good}.json"


def test_unsafe_key_never_reaches_storage():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        assert run_logs.open_run("../escape") == ""
        run_logs.append("../escape", "should not be stored")
        assert run_logs.read("../escape")["lines"] == []
        assert run_logs.clear("../escape") is False
    assert store == {}






def test_every_entry_point_survives_a_dead_storage_backend():
    # A bookkeeping failure must never turn a working task into a failed one.
    with patch.object(run_logs, "data_io", _fake_io({}, fail=True)):
        assert run_logs.open_run("demo_proc") == ""
        run_logs.append("demo_proc", "line")
        run_logs.flush("demo_proc")
        run_logs.detach("demo_proc")
        run_logs.finalize("demo_proc", run_logs.STATE_FAILED)
        assert run_logs.read("demo_proc")["lines"] == []
        assert run_logs.clear("demo_proc") is False


def test_clear_removes_the_history():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        run_logs.append("demo_proc", "line")
        run_logs.flush("demo_proc")
        assert store
        assert run_logs.clear("demo_proc") is True
    assert store == {}






def test_reporter_log_handler_forwards_records_without_recursing():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        handler = run_logs.ReporterLogHandler("demo_proc")
        record = logging.LogRecord("fyp.test", logging.INFO, __file__, 1,
                                   "worker narration", None, None)
        handler.emit(record)
        run_logs.flush("demo_proc")

    assert any("worker narration" in ln for ln in _runs(store)[0]["lines"])


def test_reporter_log_handler_drops_records_emitted_during_a_write():
    # The write path itself logs; forwarding those records would recurse until
    # the stack blew, so they are dropped rather than queued.
    store = {}
    handler = run_logs.ReporterLogHandler("demo_proc")
    record = logging.LogRecord("fyp.test", logging.INFO, __file__, 1,
                               "re-entrant", None, None)
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        run_logs._guard.handling = True
        try:
            handler.emit(record)
        finally:
            run_logs._guard.handling = False
        run_logs.flush("demo_proc")

    assert not any("re-entrant" in ln for ln in _runs(store)[0]["lines"])


def test_append_does_not_force_a_nested_flush_while_writing():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")
        run_logs._guard.writing = True
        try:
            for i in range(run_logs.MAX_PENDING_LINES + 10):
                run_logs.append("demo_proc", f"burst {i}")
        finally:
            run_logs._guard.writing = False
        # Nothing was written; the lines are still buffered.
        assert len(_runs(store)[0]["lines"]) < run_logs.MAX_PENDING_LINES
        run_logs.flush("demo_proc")

    assert any("burst 0" in ln for ln in _runs(store)[0]["lines"])


def test_concurrent_appends_all_survive():
    store = {}
    with patch.object(run_logs, "data_io", _fake_io(store)):
        run_logs.open_run("demo_proc")

        def _worker(n):
            for i in range(20):
                run_logs.append("demo_proc", f"t{n}-{i}")

        threads = [threading.Thread(target=_worker, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        run_logs.flush("demo_proc")

    lines = _runs(store)[0]["lines"]
    for n in range(5):
        assert sum(1 for ln in lines if f"t{n}-" in ln) == 20


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
