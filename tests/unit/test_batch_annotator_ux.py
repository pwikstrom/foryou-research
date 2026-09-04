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
    cfg = {"machine": {"gemini": {"model": "gemini-x"}}, "data_io": {"GCS_bucket_name": "b"}}
    monkeypatch.setattr(batch, "_cf", lambda: cfg)

    class _Bucket:
        def list_blobs(self, prefix=None):
            return []

    monkeypatch.setattr(batch, "_gcs_bucket", lambda: _Bucket())
    monkeypatch.setattr(batch, "platform_map_for", lambda ids: {})
    monkeypatch.setattr(batch, "_machine_annotations_label", lambda: "machine_annotations")
    monkeypatch.setattr(batch.annotation_versioning,
                        "active_annotation_version", lambda: "av_test")
    saved = {}
    monkeypatch.setattr(
        batch.data_io, "save_json",
        lambda data, storage_location, filename, **kw: saved.update(
            {"filename": filename, "data": data}),
    )

    # The prompt label comes from active_prompt_label() (a constant), not config.
    fn = batch.download_and_ingest("gs://b/data/out/", [])
    assert isinstance(fn, str) and fn == saved["filename"]


def test_finished_run_writes_its_totals_to_the_enrichment_history(monkeypatch):
    """The batch worker's one history line carries the job-wide totals and who
    started it — the numbers the Edit Collections History renders."""
    import web_interface.services.enrichment_journal as journal

    seen = []
    monkeypatch.setattr(journal, "record",
                        lambda kind, message, **kw: seen.append((kind, message, kw)))
    worker._journal_finished({"total_ok": 85, "total_fail": 5, "chunk_index": 1,
                              "started_by": "enrichment_supervisor"},
                             0, "Queue is now empty.")
    kind, message, kw = seen[0]
    assert kind == "annotate.finished"
    assert "85 annotated, 5 failed" in message and "queue empty" in message
    assert kw["actor"] == "enrichment_supervisor" and kw["ok"] == 85 and kw["fail"] == 5


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
    """Per-job-aware batch API stand-in for the job-table state machine."""

    _TERMINAL_FAIL = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}

    def __init__(self, default_state="JOB_STATE_SUCCEEDED", states=None,
                 submit_raise=False, raise_uris=()):
        self.default_state = default_state
        self.states = dict(states or {})          # job_name -> state override
        self.submit_raise = submit_raise
        self.raise_uris = set(raise_uris)         # output uris whose ingest raises
        self.submitted = []                       # job names in submission order
        self.last_ingested_ids = None

    def build_and_upload_jsonl(self, slice_ids, ts):
        if self.submit_raise:
            raise RuntimeError("subboom")
        return (f"gs://b/in/{ts}.jsonl", list(slice_ids))

    def submit_batch_job(self, uri, ts):
        name = f"job/{len(self.submitted) + 1}"
        self.submitted.append(name)
        return (name, f"gs://b/out/{name}/")

    def poll_batch_job(self, name):
        return self.states.get(name, self.default_state)

    def download_and_ingest(self, uri, ids):
        if uri in self.raise_uris:
            raise RuntimeError("boom")
        self.last_ingested_ids = [str(i) for i in ids]
        return "raw.json"


@pytest.fixture
def notices(monkeypatch):
    """Capture (to_email, kind, details) tuples the worker would email."""
    calls: list[tuple] = []
    monkeypatch.setattr(worker, "send_batch_annotation_email_async",
                        lambda to, kind, **d: calls.append((to, kind, d)))
    return calls


def _refine_echo_ingested(monkeypatch, fake_batch, fail_ids=()):
    """Refine returns exactly what the fake batch last ingested (ok unless listed)."""
    import fyp.machine_annotation as ma

    def _refine(raw_json_filename, verbose=False):
        ids = fake_batch.last_ingested_ids or []
        return pd.DataFrame({
            "item_id": ids,
            "annotated_ok": [i not in fail_ids for i in ids],
            "annotated_fail": [i in fail_ids for i in ids],
        })

    monkeypatch.setattr(ma, "refine_one_raw_annotation_batch", _refine)


# --------------------------------------------------------------------------- #
# Run phase: submitting fills concurrent slots.
# --------------------------------------------------------------------------- #
def test_run_fills_slots_claims_and_emails_submitted_once(notices):
    ids = [f"v{i}" for i in range(5000)]
    dio = FakeDataIO({"to_annotate.json": list(ids)})
    rep = FakeReporter()
    fb = FakeBatch(default_state="JOB_STATE_RUNNING")
    ta = {"phase": "run", "batch_size": 2000, "launched_by": "u@x.com"}

    result = worker._run_phase(rep, ta, fb, dio)

    assert fb.submitted == ["job/1", "job/2", "job/3"]     # 2000+2000+1000
    assert dio.store["to_annotate.json"] == []             # every slice claimed out
    js = dio.store["annotate_batch_job.json"]
    assert js["format"] == 2 and len(js["jobs"]) == 3
    assert sum(len(j["submitted_ids"]) for j in js["jobs"]) == 5000
    assert result["chain"] is True and result["next_task_args"]["phase"] == "run"
    assert rep.data.get("annotate_claimed_len") == 5000    # live in-flight indicator
    assert rep.data.get("annotate_queue_len") == 0
    assert any("Starting async annotation" in m for m in rep.logs)
    assert [k for _, k, _ in notices].count("submitted") == 1


def test_run_respects_the_concurrency_cap(notices):
    ids = [f"v{i}" for i in range(20000)]
    dio = FakeDataIO({"to_annotate.json": list(ids)})
    fb = FakeBatch(default_state="JOB_STATE_RUNNING")
    ta = {"phase": "run", "batch_size": 2000}

    result = worker._run_phase(FakeReporter(), ta, fb, dio)

    assert len(fb.submitted) == worker.MAX_CONCURRENT_JOBS
    assert len(dio.store["to_annotate.json"]) == 20000 - 2000 * worker.MAX_CONCURRENT_JOBS
    # Next link with everything still running submits nothing further.
    fb2_count = len(fb.submitted)
    result = worker._run_phase(FakeReporter(), result["next_task_args"], fb, dio)
    assert len(fb.submitted) == fb2_count
    assert result["chain"] is True


def test_run_refills_free_slots_when_the_queue_grows(notices):
    # One job in flight, slots free, new items appear (a handoff landed
    # mid-run): the same chain absorbs them as additional concurrent jobs.
    dio = FakeDataIO({"to_annotate.json": ["n1", "n2"]})
    fb = FakeBatch(default_state="JOB_STATE_RUNNING")
    ta = {"phase": "run", "batch_size": 2000, "chunk_index": 1,
          "jobs": [{"job_name": "job/0", "output_uri": "gs://b/out/job/0/",
                    "jsonl_uri": "", "submitted_ids": ["a", "b"],
                    "ts_label": "", "batch_no": 1, "submitted_at": ""}]}

    result = worker._run_phase(FakeReporter(), ta, fb, dio)

    js = dio.store["annotate_batch_job.json"]
    assert len(js["jobs"]) == 2 and fb.submitted == ["job/1"]
    assert dio.store["to_annotate.json"] == []
    assert result["chain"] is True


# --------------------------------------------------------------------------- #
# Run phase: completion, totals, and terminal conditions.
# --------------------------------------------------------------------------- #
def test_concurrent_jobs_complete_with_correct_totals(monkeypatch, notices):
    ids = [f"v{i}" for i in range(3000)]
    dio = FakeDataIO({"to_annotate.json": list(ids)})
    fb = FakeBatch(default_state="JOB_STATE_RUNNING")
    ta = {"phase": "run", "batch_size": 2000, "launched_by": "u@x.com"}
    result = worker._run_phase(FakeReporter(), ta, fb, dio)   # 2 jobs submitted

    fb.default_state = "JOB_STATE_SUCCEEDED"
    _refine_echo_ingested(monkeypatch, fb, fail_ids={"v0"})
    rep = FakeReporter()
    result = worker._run_phase(rep, result["next_task_args"], fb, dio)

    assert result is None                                   # terminal
    assert "annotate_batch_job.json" not in dio.store       # cleared
    kinds = [k for _, k, _ in notices]
    assert "completed" in kinds and "batch_done" not in kinds
    completed = next(d for _, k, d in notices if k == "completed")
    assert completed == {"total_ok": 2999, "total_fail": 1}
    assert any("All done" in m and "Consolidate & Refresh" in m for m in rep.logs)
    assert rep.data.get("annotate_claimed_len") == 0


def test_one_job_completing_while_another_runs_emails_batch_done(monkeypatch, notices):
    dio = FakeDataIO({"to_annotate.json": []})
    fb = FakeBatch(states={"job/1": "JOB_STATE_SUCCEEDED",
                           "job/2": "JOB_STATE_RUNNING"})
    _refine_echo_ingested(monkeypatch, fb)
    ta = {"phase": "run", "batch_size": 2000, "chunk_index": 2, "launched_by": "u@x.com",
          "jobs": [
              {"job_name": "job/1", "output_uri": "gs://b/out/job/1/", "jsonl_uri": "",
               "submitted_ids": ["a", "b"], "ts_label": "", "batch_no": 1, "submitted_at": ""},
              {"job_name": "job/2", "output_uri": "gs://b/out/job/2/", "jsonl_uri": "",
               "submitted_ids": ["c"], "ts_label": "", "batch_no": 2, "submitted_at": ""},
          ]}

    result = worker._run_phase(FakeReporter(), ta, fb, dio)

    assert result["chain"] is True
    na = result["next_task_args"]
    assert [j["job_name"] for j in na["jobs"]] == ["job/2"]
    assert na["total_ok"] == 2
    kinds = [k for _, k, _ in notices]
    assert "batch_done" in kinds and "completed" not in kinds


def test_one_failed_job_restores_only_its_ids_and_halts_submits(monkeypatch, notices):
    dio = FakeDataIO({"to_annotate.json": ["x1", "x2"]})   # would-be next slice
    fb = FakeBatch(states={"job/1": "JOB_STATE_FAILED",
                           "job/2": "JOB_STATE_SUCCEEDED"})
    _refine_echo_ingested(monkeypatch, fb)
    ta = {"phase": "run", "batch_size": 2000, "chunk_index": 2, "launched_by": "u@x.com",
          "jobs": [
              {"job_name": "job/1", "output_uri": "gs://b/out/job/1/", "jsonl_uri": "",
               "submitted_ids": ["a", "b"], "ts_label": "", "batch_no": 1, "submitted_at": ""},
              {"job_name": "job/2", "output_uri": "gs://b/out/job/2/", "jsonl_uri": "",
               "submitted_ids": ["c", "d"], "ts_label": "", "batch_no": 2, "submitted_at": ""},
          ]}

    result = worker._run_phase(FakeReporter(), ta, fb, dio)

    assert result is None                                   # halted + drained = done
    assert fb.submitted == []                               # no new submits after a failure
    assert sorted(dio.store["to_annotate.json"]) == ["a", "b", "x1", "x2"]  # only job/1 restored
    assert "annotate_batch_job.json" not in dio.store
    kinds = [k for _, k, _ in notices]
    assert "failed" in kinds
    completed = next(d for _, k, d in notices if k == "completed")
    assert completed == {"total_ok": 2, "total_fail": 0}    # job/2 still counted


def test_ingest_exception_is_isolated_to_its_job(monkeypatch, notices):
    dio = FakeDataIO({"to_annotate.json": []})
    fb = FakeBatch(raise_uris={"gs://b/out/job/1/"})
    _refine_echo_ingested(monkeypatch, fb)
    ta = {"phase": "run", "batch_size": 2000, "chunk_index": 2, "launched_by": "u@x.com",
          "jobs": [
              {"job_name": "job/1", "output_uri": "gs://b/out/job/1/", "jsonl_uri": "",
               "submitted_ids": ["a", "b"], "ts_label": "", "batch_no": 1, "submitted_at": ""},
              {"job_name": "job/2", "output_uri": "gs://b/out/job/2/", "jsonl_uri": "",
               "submitted_ids": ["c"], "ts_label": "", "batch_no": 2, "submitted_at": ""},
          ]}

    result = worker._run_phase(FakeReporter(), ta, fb, dio)

    assert result is None
    assert sorted(dio.store["to_annotate.json"]) == ["a", "b"]   # job/1 restored
    assert any(k == "failed" and "boom" in str(d.get("error", "")) for _, k, d in notices)
    completed = next(d for _, k, d in notices if k == "completed")
    assert completed["total_ok"] == 1                        # job/2 ingested fine


def test_submit_failure_with_no_jobs_in_flight_stops(notices):
    dio = FakeDataIO({"to_annotate.json": ["a", "b"]})
    fb = FakeBatch(submit_raise=True)
    ta = {"phase": "run", "batch_size": 2000, "launched_by": "u@x.com"}

    result = worker._run_phase(FakeReporter(), ta, fb, dio)

    assert result is None
    assert dio.store["to_annotate.json"] == ["a", "b"]      # queue untouched
    assert "annotate_batch_job.json" not in dio.store
    assert any(k == "failed" and "subboom" in str(d.get("error", ""))
               for _, k, d in notices)


def test_cancellation_leaves_jobs_and_claims_as_is(notices):
    dio = FakeDataIO({"to_annotate.json": ["x"]})
    rep = FakeReporter()
    rep.cancelled = True
    ta = {"phase": "run", "chunk_index": 1,
          "jobs": [{"job_name": "job/1", "output_uri": "gs://b/out/job/1/",
                    "jsonl_uri": "", "submitted_ids": ["a"], "ts_label": "",
                    "batch_no": 1, "submitted_at": ""}]}

    result = worker._run_phase(rep, ta, FakeBatch(), dio)

    assert result is None
    assert dio.store["to_annotate.json"] == ["x"]           # claimed ids NOT restored
    assert "annotate_batch_job.json" not in dio.store       # file cleared
    assert notices == []


# --------------------------------------------------------------------------- #
# Legacy phase adapters (a chain in flight across the deploy).
# --------------------------------------------------------------------------- #
def test_legacy_poll_args_wrap_into_a_one_job_table():
    run = worker._legacy_args_to_run({
        "phase": "poll", "job_name": "job/9", "output_uri": "gs://b/out/",
        "jsonl_uri": "gs://b/in.jsonl", "submitted_ids": ["a", "b"],
        "chunk_index": 1, "initial_total": 4000, "batch_size": 2000,
        "max_batches": None, "launched_by": "u@x.com",
        "total_ok": 5, "total_fail": 1,
    })
    assert run["phase"] == "run" and len(run["jobs"]) == 1
    job = run["jobs"][0]
    assert job["job_name"] == "job/9" and job["submitted_ids"] == ["a", "b"]
    assert run["chunk_index"] == 2          # this job counts as submitted
    assert run["notified_submitted"] is True
    assert run["total_ok"] == 5 and run["total_fail"] == 1


def test_legacy_submit_args_start_an_empty_table():
    run = worker._legacy_args_to_run({"phase": "submit", "batch_size": 500,
                                      "launched_by": "u@x.com"})
    assert run["phase"] == "run" and run["jobs"] == []
    assert run["batch_size"] == 500 and run["launched_by"] == "u@x.com"


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


def test_log_forwards_bare_leaving_stamping_to_the_run_log():
    # Timestamping happens once, in run_logs.append. This worker used to add
    # its own prefix from a config key that does not exist, so its stamps were
    # UTC on Cloud Run while every other line was in the project timezone.
    rep = FakeReporter()
    worker._log(rep, "hello world")
    assert rep.logs == ["hello world"]
