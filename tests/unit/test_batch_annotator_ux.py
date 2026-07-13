"""Batch-annotator async UX: ingest-key regression + worker state-machine.

Guards the KeyError:'prompt' regression (the batch ingest read a config key that
no longer exists) and the async-communication behaviour added alongside it:
claim/restore, job-state clearing, and the launcher email milestones.
"""

import pandas as pd
import pytest

import fyp.annotation.machine_annotation_batch as batch
import web_interface.run_queue_annotator_batch as worker


# --------------------------------------------------------------------------- #
# Part 0 regression: download_and_ingest must not read [machine].prompt.
# --------------------------------------------------------------------------- #
def test_download_and_ingest_does_not_need_machine_prompt_key(monkeypatch):
    # Config deliberately WITHOUT [machine].prompt — the removed key whose stale
    # reference raised KeyError: 'prompt' and stranded a billed batch in prod.
    cfg = {"machine": {"model": "gemini-x"}, "data_io": {"GCS_bucket_name": "b"}}
    monkeypatch.setattr(batch, "_cf", lambda: cfg)

    class _Bucket:
        def list_blobs(self, prefix=None):
            return []

    monkeypatch.setattr(batch, "_gcs_bucket", lambda: _Bucket())
    monkeypatch.setattr(batch, "platform_map_for", lambda ids: {})
    monkeypatch.setattr(batch, "_machine_annotations_label", lambda: "machine_annotations")
    monkeypatch.setattr(batch.annotation_versioning,
                        "current_annotation_version", lambda: "av_test")
    saved = {}
    monkeypatch.setattr(
        batch.data_io, "save_json",
        lambda data, storage_location, filename, **kw: saved.update(
            {"filename": filename, "data": data}),
    )

    # The prompt label comes from active_prompt_label() (a constant), not config.
    fn = batch.download_and_ingest("gs://b/data/out/", [])
    assert isinstance(fn, str) and fn == saved["filename"]


def test_active_prompt_label_is_config_free():
    # active_prompt_label() is the value download_and_ingest now uses; it must be
    # a stable label that needs no [machine].prompt config.
    assert batch.annotation_versioning.active_prompt_label() == "annotation_contract.toml"


# --------------------------------------------------------------------------- #
# Worker state-machine fakes.
# --------------------------------------------------------------------------- #
class FakeReporter:
    def __init__(self):
        self.logs: list[str] = []
        self.data: dict = {}
        self.cancelled = False

    def log(self, m):
        self.logs.append(str(m))

    def emit_data(self, d):
        self.data.update(d)

    def check_cancelled(self):
        return self.cancelled


class FakeDataIO:
    """In-memory stand-in for fyp.data_io keyed by filename."""

    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def load_json(self, storage_location, filename):
        return self.store.get(filename)

    def save_json(self, data, storage_location, filename, **kw):
        self.store[filename] = data

    def exists(self, storage_location, filename):
        return filename in self.store

    def remove(self, storage_location, filename):
        self.store.pop(filename, None)

    def update_json(self, storage_location="cache", filename="", mutate=None,
                    default=None, max_retries=6, verbose=False):
        import json as _json
        current = self.store.get(filename)
        if current is None:
            current = _json.loads(_json.dumps(default)) if default is not None else None
        new_value = mutate(current)
        if new_value is None:
            return None
        self.store[filename] = new_value
        return new_value


class FakeBatch:
    _TERMINAL_FAIL = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}

    def __init__(self, state="JOB_STATE_SUCCEEDED", ingest="ok"):
        self.state = state
        self.ingest = ingest

    def build_and_upload_jsonl(self, slice_ids, ts):
        return ("gs://b/in.jsonl", list(slice_ids))

    def submit_batch_job(self, uri, ts):
        return ("job/1", "gs://b/out/")

    def poll_batch_job(self, name):
        return self.state

    def download_and_ingest(self, uri, ids):
        if self.ingest == "raise":
            raise RuntimeError("boom")
        return "raw.json"


@pytest.fixture
def notices(monkeypatch):
    """Capture (to_email, kind, details) tuples the worker would email."""
    calls: list[tuple] = []
    monkeypatch.setattr(worker, "send_batch_annotation_email_async",
                        lambda to, kind, **d: calls.append((to, kind, d)))
    return calls


def _refine_returns(monkeypatch, df):
    import fyp.machine_annotation as ma
    monkeypatch.setattr(ma, "refine_one_raw_annotation_batch",
                        lambda raw_json_filename, verbose=False: df)


# --------------------------------------------------------------------------- #
# Submit phase.
# --------------------------------------------------------------------------- #
def test_submit_claims_queue_and_emails_submitted(notices):
    dio = FakeDataIO({"to_annotate.json": ["a", "b", "c"]})
    rep = FakeReporter()
    ta = {"phase": "submit", "batch_size": 2000, "chunk_index": 0, "launched_by": "u@x.com"}

    result = worker._submit_phase(rep, ta, FakeBatch(), dio)

    assert dio.store["to_annotate.json"] == []            # slice claimed out
    js = dio.store["annotate_batch_job.json"]
    assert js["phase"] == "poll" and js["launched_by"] == "u@x.com"
    assert sorted(js["submitted_ids"]) == ["a", "b", "c"]
    assert result["chain"] is True and result["next_task_args"]["phase"] == "poll"
    assert rep.data.get("annotate_claimed_len") == 3      # live in-flight indicator
    assert rep.data.get("annotate_queue_len") == 0        # pending drops immediately on claim
    assert any("Starting async annotation" in m for m in rep.logs)  # start summary
    assert any("Batch 1 of 1" in m for m in rep.logs)     # batch numbering
    assert ("u@x.com", "submitted", {"n_items": 3}) in notices


def test_submit_does_not_email_after_first_chunk(notices):
    dio = FakeDataIO({"to_annotate.json": ["a", "b"]})
    ta = {"phase": "submit", "chunk_index": 1, "launched_by": "u@x.com"}
    worker._submit_phase(FakeReporter(), ta, FakeBatch(), dio)
    assert not any(kind == "submitted" for _, kind, _ in notices)


# --------------------------------------------------------------------------- #
# Poll phase.
# --------------------------------------------------------------------------- #
def test_poll_success_single_chunk_emails_completed_only(monkeypatch, notices):
    _refine_returns(monkeypatch, pd.DataFrame({
        "item_id": ["a", "b", "c"],
        "annotated_ok": [True, True, False],
        "annotated_fail": [False, False, True],
    }))
    dio = FakeDataIO({"to_annotate.json": [],
                      "annotate_batch_job.json": {"submitted_ids": ["a", "b", "c"]}})
    rep = FakeReporter()
    ta = {"phase": "poll", "job_name": "job/1", "output_uri": "gs://b/out/",
          "submitted_ids": ["a", "b", "c"], "chunk_index": 0,
          "launched_by": "u@x.com", "total_ok": 0, "total_fail": 0}

    result = worker._poll_phase(rep, ta, FakeBatch(), dio)

    assert result is None                                     # terminal
    assert "annotate_batch_job.json" not in dio.store         # job state cleared
    kinds = [kind for _, kind, _ in notices]
    assert "completed" in kinds and "batch_done" not in kinds  # no redundant email
    completed = next(d for _, k, d in notices if k == "completed")
    assert completed == {"total_ok": 2, "total_fail": 1}
    assert any("All done" in m and "Consolidate & Refresh" in m for m in rep.logs)


def test_poll_multichunk_emails_batch_done_and_chains(monkeypatch, notices):
    _refine_returns(monkeypatch, pd.DataFrame({
        "item_id": ["a", "b"],
        "annotated_ok": [True, True],
        "annotated_fail": [False, False],
    }))
    dio = FakeDataIO({"to_annotate.json": ["x", "y"],   # leftover -> more chunks
                      "annotate_batch_job.json": {"submitted_ids": ["a", "b"]}})
    rep = FakeReporter()
    ta = {"phase": "poll", "job_name": "job/1", "output_uri": "gs://b/out/",
          "submitted_ids": ["a", "b"], "chunk_index": 0,
          "launched_by": "u@x.com", "total_ok": 0, "total_fail": 0}

    result = worker._poll_phase(rep, ta, FakeBatch(), dio)

    na = result["next_task_args"]
    assert result["chain"] is True and na["phase"] == "submit"
    assert na["chunk_index"] == 1 and na["launched_by"] == "u@x.com"
    assert na["total_ok"] == 2 and na["total_fail"] == 0     # running totals carried
    kinds = [kind for _, kind, _ in notices]
    assert "batch_done" in kinds and "completed" not in kinds
    assert "annotate_batch_job.json" in dio.store            # not cleared mid-run
    assert rep.data.get("annotate_claimed_len") == 0         # slice ingested


def test_poll_ingest_exception_restores_queue_and_emails_failed(notices):
    dio = FakeDataIO({"to_annotate.json": [],
                      "annotate_batch_job.json": {"submitted_ids": ["a", "b", "c"]}})
    ta = {"phase": "poll", "job_name": "job/1", "output_uri": "gs://b/out/",
          "submitted_ids": ["a", "b", "c"], "launched_by": "u@x.com"}

    result = worker._poll_phase(FakeReporter(), ta,
                                FakeBatch(ingest="raise"), dio)

    assert result is None
    assert sorted(dio.store["to_annotate.json"]) == ["a", "b", "c"]  # restored, not stranded
    assert "annotate_batch_job.json" not in dio.store                # cleared
    assert any(k == "failed" and "boom" in str(d.get("error", ""))
               for _, k, d in notices)


def test_poll_terminal_fail_restores_queue_and_emails_failed(notices):
    dio = FakeDataIO({"to_annotate.json": [],
                      "annotate_batch_job.json": {"submitted_ids": ["a", "b"]}})
    ta = {"phase": "poll", "job_name": "job/1", "output_uri": "gs://b/out/",
          "submitted_ids": ["a", "b"], "launched_by": "u@x.com"}

    result = worker._poll_phase(FakeReporter(), ta,
                                FakeBatch(state="JOB_STATE_FAILED"), dio)

    assert result is None
    assert sorted(dio.store["to_annotate.json"]) == ["a", "b"]
    assert "annotate_batch_job.json" not in dio.store
    assert any(k == "failed" for _, k, _ in notices)


def test_notify_is_noop_without_launcher(notices):
    worker._notify({"launched_by": None}, "submitted", n_items=1)
    worker._notify({}, "completed", total_ok=1, total_fail=0)
    assert notices == []


def test_total_batches_estimate():
    assert worker._total_batches(350, 2000, None) == 1     # fits one batch
    assert worker._total_batches(5000, 2000, None) == 3    # 2000+2000+1000
    assert worker._total_batches(5000, 2000, 1) == 1       # capped by max_batches
    assert worker._total_batches(0, 2000, None) == 1       # floor at 1
    assert worker._total_batches(100, 0, None) == 1        # guard div-by-zero


def test_log_prepends_timestamp():
    rep = FakeReporter()
    worker._log(rep, "hello world")
    assert len(rep.logs) == 1
    assert rep.logs[0].endswith("hello world")
    assert rep.logs[0][0] == "[" and "] " in rep.logs[0]   # [HH:MM:SS] prefix
