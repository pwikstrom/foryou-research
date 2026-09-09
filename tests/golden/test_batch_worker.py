"""Tests for the batch-annotation worker's queue semantics (claim / restore /
reschedule), with a fake batch backend and an isolated cache — no GCS, no API.

One ``run`` phase drives a TABLE of concurrent jobs. Covered here:
  * a fresh link submits from the queue's head and CLAIMS the slice out of it;
  * it keeps filling free slots up to ``MAX_CONCURRENT_JOBS``;
  * a still-running job re-chains and leaves the queue alone;
  * a failed job restores its whole claimed slice and stops the run;
  * a succeeded job keeps ok/fail items claimed and re-queues only the
    unprocessed (DNF / missing) ones;
  * the slot it frees is refilled from the queue on the same link;
  * the legacy ``submit`` / ``poll`` args still map onto the table shape.

Usage:
    python tests/golden/test_batch_worker.py
"""

from __future__ import annotations

import contextlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

import fyp.data_io as data_io
import fyp.machine_annotation as ma
import web_interface.run_queue_annotator_batch as w
from fyp.fyp_config import fyp_cf

QUEUE = w.QUEUE_FILE


@contextlib.contextmanager
def _isolated_cache():
    orig = fyp_cf["paths"].get("cache")
    tmp = tempfile.mkdtemp(prefix="fyp_batchwork_")
    fyp_cf["paths"]["cache"] = tmp
    try:
        yield tmp
    finally:
        fyp_cf["paths"]["cache"] = orig
        shutil.rmtree(tmp, ignore_errors=True)


class _Reporter:
    def __init__(self):
        self.logs = []
        self.data = {}

    def log(self, msg):
        self.logs.append(msg)

    def emit_data(self, d):
        self.data.update(d)

    def check_cancelled(self):
        return False


class _FakeBatch:
    _TERMINAL_FAIL = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}

    def __init__(self, poll_state="JOB_STATE_SUCCEEDED"):
        self.poll_state = poll_state
        self.submitted = []

    def build_and_upload_jsonl(self, ids, ts):
        return ("gs://b/in.jsonl", [str(i) for i in ids])

    def submit_batch_job(self, uri, ts):
        self.submitted.append(uri)
        return (f"projects/x/locations/y/batchJobs/{len(self.submitted)}", "gs://b/out/")

    def poll_batch_job(self, name):
        return self.poll_state

    def download_and_ingest(self, out, submitted):
        return "machine_annotations_fake.json"


def _seed(queue):
    data_io.save_json(data=queue, storage_location="cache", filename=QUEUE)


def _queue():
    return data_io.load_json(storage_location="cache", filename=QUEUE) or []


def _job(submitted_ids, batch_no=1):
    """One in-flight entry of the job table."""
    return {
        "job_name": "j",
        "output_uri": "gs://b/out/",
        "jsonl_uri": "gs://b/in.jsonl",
        "submitted_ids": list(submitted_ids),
        "ts_label": "",
        "batch_no": batch_no,
        "submitted_at": "",
    }


@contextlib.contextmanager
def _refine_returns(ok_ids):
    """Stub refinement: every id in ``ok_ids`` comes back annotated."""

    def _fake_refine(raw_json_filename, verbose=False):
        return pd.DataFrame({
            "item_id": list(ok_ids),
            "annotated_ok": [True] * len(ok_ids),
            "annotated_fail": [False] * len(ok_ids),
        })

    orig = ma.refine_one_raw_annotation_batch
    ma.refine_one_raw_annotation_batch = _fake_refine
    try:
        yield
    finally:
        ma.refine_one_raw_annotation_batch = orig


# ---------------------------------------------------------------------------
# claim / restore helpers
# ---------------------------------------------------------------------------

def test_claim_removes_ids() -> None:
    with _isolated_cache():
        _seed(["i1", "i2", "i3"])
        removed = w._claim_from_queue(data_io, ["i1", "i3"])
        assert removed == 2
        assert _queue() == ["i2"]


def test_restore_adds_without_duplicates() -> None:
    with _isolated_cache():
        _seed(["i2"])
        added = w._restore_to_queue(data_io, ["i1", "i2", "i4"])  # i2 already present
        assert added == 2
        assert set(_queue()) == {"i1", "i2", "i4"}


# ---------------------------------------------------------------------------
# submitting into the job table
# ---------------------------------------------------------------------------

def test_submit_claims_slice_and_chains() -> None:
    with _isolated_cache():
        _seed(["i1", "i2", "i3", "i4"])
        args = {"batch_size": 2, "max_concurrent_jobs": 1}
        out = w._run_phase(_Reporter(), args, _FakeBatch(), data_io)
        q = _queue()
    assert out["chain"] is True
    assert out["next_task_args"]["phase"] == "run"
    jobs = out["next_task_args"]["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["submitted_ids"] == ["i1", "i2"]
    assert out["next_dispatch_delay_seconds"] == w._POLL_DELAY_S
    # The two claimed ids were removed from the queue.
    assert q == ["i3", "i4"]


def test_submit_fills_every_free_slot() -> None:
    ids = [f"i{n}" for n in range(1, 11)]
    with _isolated_cache():
        _seed(list(ids))
        out = w._run_phase(_Reporter(), {"batch_size": 2}, _FakeBatch(), data_io)
        q = _queue()
    jobs = out["next_task_args"]["jobs"]
    assert len(jobs) == w.MAX_CONCURRENT_JOBS          # cap respected
    claimed = [i for j in jobs for i in j["submitted_ids"]]
    assert claimed == ids[:2 * w.MAX_CONCURRENT_JOBS]  # taken from the queue's head
    assert q == ids[2 * w.MAX_CONCURRENT_JOBS:]        # the rest still queued


# ---------------------------------------------------------------------------
# polling the job table
# ---------------------------------------------------------------------------

def test_poll_running_reschedules_and_leaves_queue() -> None:
    with _isolated_cache():
        _seed(["i3", "i4"])
        args = {"phase": "run", "batch_size": 2, "chunk_index": 1,
                "max_concurrent_jobs": 1, "jobs": [_job(["i1", "i2"])]}
        out = w._run_phase(_Reporter(), args, _FakeBatch("JOB_STATE_RUNNING"), data_io)
        q = _queue()
    assert out["chain"] is True
    assert out["next_task_args"]["phase"] == "run"
    assert [j["submitted_ids"] for j in out["next_task_args"]["jobs"]] == [["i1", "i2"]]
    assert out["next_dispatch_delay_seconds"] == w._POLL_DELAY_S
    assert q == ["i3", "i4"]      # untouched


def test_poll_failed_restores_claimed_items() -> None:
    with _isolated_cache():
        _seed(["i3", "i4"])              # i1,i2 are claimed (in-flight)
        args = {"phase": "run", "batch_size": 2, "chunk_index": 1,
                "max_concurrent_jobs": 1, "jobs": [_job(["i1", "i2"])]}
        out = w._run_phase(_Reporter(), args, _FakeBatch("JOB_STATE_FAILED"), data_io)
        q = _queue()
    assert out is None                   # stops, no chain
    assert set(q) == {"i1", "i2", "i3", "i4"}   # claimed items restored


def test_poll_success_requeues_only_unprocessed() -> None:
    # i1 came back ok; i2 was submitted but never returned (DNF) -> re-queue i2.
    # max_batches=1 is already spent, so this link submits nothing further and
    # the queue shows exactly what the finished job left behind.
    with _refine_returns(["i1"]):
        with _isolated_cache():
            _seed(["i3", "i4"])          # i1,i2 claimed
            args = {"phase": "run", "batch_size": 2, "chunk_index": 1,
                    "max_batches": 1, "max_concurrent_jobs": 1,
                    "jobs": [_job(["i1", "i2"])]}
            out = w._run_phase(_Reporter(), args, _FakeBatch("JOB_STATE_SUCCEEDED"), data_io)
            q = _queue()
    assert out is None                                     # max-batches reached
    assert "i1" not in q                                   # ok item stays claimed
    assert set(q) == {"i2", "i3", "i4"}                    # unprocessed re-queued


def test_finished_job_frees_a_slot_for_the_next_batch() -> None:
    with _refine_returns(["i1", "i2"]):
        with _isolated_cache():
            _seed(["i3", "i4"])
            args = {"phase": "run", "batch_size": 2, "chunk_index": 1,
                    "max_batches": 2, "max_concurrent_jobs": 1,
                    "jobs": [_job(["i1", "i2"])]}
            out = w._run_phase(_Reporter(), args, _FakeBatch("JOB_STATE_SUCCEEDED"), data_io)
            q = _queue()
    assert out["chain"] is True
    jobs = out["next_task_args"]["jobs"]
    assert [j["submitted_ids"] for j in jobs] == [["i3", "i4"]]   # slot refilled
    assert out["next_task_args"]["chunk_index"] == 2
    assert q == []                                                # newly claimed


# ---------------------------------------------------------------------------
# legacy chain links, live across the deploy
# ---------------------------------------------------------------------------

def test_legacy_poll_args_become_a_one_job_table() -> None:
    run = w._legacy_args_to_run({
        "phase": "poll", "job_name": "j", "output_uri": "gs://b/out/",
        "submitted_ids": ["i1", "i2"], "chunk_index": 0, "batch_size": 2,
    })
    assert run["phase"] == "run"
    assert [j["submitted_ids"] for j in run["jobs"]] == [["i1", "i2"]]
    assert run["chunk_index"] == 1          # the table counts submitted jobs
    assert run["notified_submitted"] is True


def test_legacy_submit_args_start_with_an_empty_table() -> None:
    run = w._legacy_args_to_run({"phase": "submit", "batch_size": 2, "chunk_index": 3})
    assert run["phase"] == "run"
    assert run["jobs"] == []
    assert run["chunk_index"] == 3


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception:
            failures += 1
            import traceback

            print(f"ERROR {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
