"""Tests for the batch-annotation worker's queue semantics (claim / restore /
reschedule), with a fake batch backend and an isolated cache — no GCS, no API.

Covers the (a) claim-at-submit and (b) poll-once-then-reschedule behaviour:
  * submit claims its slice out of the queue and chains to poll with a delay;
  * a still-running poll re-chains poll with a delay and leaves the queue alone;
  * a failed job restores the whole claimed slice;
  * a succeeded job keeps ok/fail items claimed and re-queues only the
    unprocessed (DNF / missing) ones, then chains back to submit.

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

    def build_and_upload_jsonl(self, ids, ts):
        return ("gs://b/in.jsonl", [str(i) for i in ids])

    def submit_batch_job(self, uri, ts):
        return ("projects/x/locations/y/batchJobs/123", "gs://b/out/")

    def poll_batch_job(self, name):
        return self.poll_state

    def download_and_ingest(self, out, submitted):
        return "machine_annotations_fake.json"


def _seed(queue):
    data_io.save_json(data=queue, storage_location="cache", filename=QUEUE)


def _queue():
    return data_io.load_json(storage_location="cache", filename=QUEUE) or []


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
# submit phase
# ---------------------------------------------------------------------------

def test_submit_claims_slice_and_chains_to_poll() -> None:
    with _isolated_cache():
        _seed(["i1", "i2", "i3", "i4"])
        out = w._submit_phase(_Reporter(), {"batch_size": 2}, _FakeBatch(), data_io)
        q = _queue()
    assert out["chain"] is True
    assert out["next_task_args"]["phase"] == "poll"
    assert out["next_task_args"]["submitted_ids"] == ["i1", "i2"]
    assert out["next_dispatch_delay_seconds"] == w._POLL_DELAY_S
    # The two claimed ids were removed from the queue.
    assert q == ["i3", "i4"]


# ---------------------------------------------------------------------------
# poll phase
# ---------------------------------------------------------------------------

def test_poll_running_reschedules_and_leaves_queue() -> None:
    with _isolated_cache():
        _seed(["i3", "i4"])
        args = {"phase": "poll", "job_name": "j", "output_uri": "gs://b/out/",
                "submitted_ids": ["i1", "i2"]}
        out = w._poll_phase(_Reporter(), args, _FakeBatch("JOB_STATE_RUNNING"), data_io)
        q = _queue()
    assert out["chain"] is True
    assert out["next_task_args"]["phase"] == "poll"
    assert out["next_dispatch_delay_seconds"] == w._POLL_DELAY_S
    assert q == ["i3", "i4"]      # untouched


def test_poll_failed_restores_claimed_items() -> None:
    with _isolated_cache():
        _seed(["i3", "i4"])              # i1,i2 are claimed (in-flight)
        args = {"phase": "poll", "job_name": "j", "output_uri": "gs://b/out/",
                "submitted_ids": ["i1", "i2"]}
        out = w._poll_phase(_Reporter(), args, _FakeBatch("JOB_STATE_FAILED"), data_io)
        q = _queue()
    assert out is None                   # stops, no chain
    assert set(q) == {"i1", "i2", "i3", "i4"}   # claimed items restored


def test_poll_success_requeues_only_unprocessed() -> None:
    # i1 came back ok; i2 was submitted but never returned (DNF) -> re-queue i2.
    def _fake_refine(raw_json_filename, verbose=False):
        return pd.DataFrame({"item_id": ["i1"], "annotated_ok": [True], "annotated_fail": [False]})

    orig = ma.refine_one_raw_annotation_batch
    ma.refine_one_raw_annotation_batch = _fake_refine
    try:
        with _isolated_cache():
            _seed(["i3", "i4"])          # i1,i2 claimed
            args = {"phase": "poll", "job_name": "j", "output_uri": "gs://b/out/",
                    "submitted_ids": ["i1", "i2"], "chunk_index": 0}
            out = w._poll_phase(_Reporter(), args, _FakeBatch("JOB_STATE_SUCCEEDED"), data_io)
            q = _queue()
    finally:
        ma.refine_one_raw_annotation_batch = orig
    assert out["chain"] is True
    assert out["next_task_args"]["phase"] == "submit"      # chains to next slice
    assert "i1" not in q                                   # ok item stays claimed
    assert "i2" in q                                       # unprocessed re-queued
    assert set(q) >= {"i2", "i3", "i4"}


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
