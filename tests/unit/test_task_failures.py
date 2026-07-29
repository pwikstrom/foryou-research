"""Background-task retry decisions + the failure ledger (S2 Phase 3).

The ledger is the dead-letter record for the Cloud Tasks queue (HTTP queues
have no native dead-letter topic), and the 503/200 decision is what makes
queue-level retry safe: only the verified-idempotent refreshes are retried.
"""

import pytest


@pytest.fixture
def ledger(monkeypatch):
    """Serve the ledger file from memory; every other file behaves normally."""
    import fyp.data_io as data_io
    from web_interface import task_failures

    store: dict = {"entries": None}
    target = task_failures.FAILURES_FILENAME
    orig_exists = data_io.exists
    orig_load_json = data_io.load_json
    orig_update_json = data_io.update_json

    def _exists(storage_location, filename, **kw):
        if filename == target:
            return store["entries"] is not None
        return orig_exists(storage_location=storage_location, filename=filename, **kw)

    def _load_json(storage_location, filename, **kw):
        if filename == target:
            return store["entries"]
        return orig_load_json(storage_location=storage_location, filename=filename, **kw)

    def _update_json(storage_location, filename, mutate, default=None, **kw):
        if filename != target:
            return orig_update_json(storage_location=storage_location, filename=filename,
                                    mutate=mutate, default=default, **kw)
        current = store["entries"] if store["entries"] is not None else default
        result = mutate(current)
        if result is not None:
            store["entries"] = result
        return store["entries"]

    monkeypatch.setattr(data_io, "exists", _exists)
    monkeypatch.setattr(data_io, "load_json", _load_json)
    monkeypatch.setattr(data_io, "update_json", _update_json)
    yield task_failures, store






def test_record_and_load(ledger):
    task_failures, store = ledger

    assert task_failures.load_failures() == []
    task_failures.record_failure(task="pca_refresh", error="boom",
                                 status_key="pca_refresh", retry_count=2,
                                 disposition=task_failures.DISPOSITION_RETRYING)

    entries = task_failures.load_failures()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["task"] == "pca_refresh"
    assert entry["retry_count"] == 2
    assert entry["disposition"] == "retrying"
    assert entry["acknowledged"] is False
    assert entry["id"]






def test_error_truncated_and_args_redacted(ledger):
    task_failures, _ = ledger

    task_failures.record_failure(
        task="ab_eval", error="x" * 5000,
        task_args={"study_name": "s1", "launched_by": "someone@example.com",
                   "arms_spec": [{"a": 1}], "batch_size": 50})

    entry = task_failures.load_failures()[0]
    assert len(entry["error"]) <= task_failures.MAX_ERROR_CHARS + 20
    assert entry["error"].endswith("(truncated)")
    assert entry["task_args"]["launched_by"] == "<redacted>"
    assert entry["task_args"]["arms_spec"] == "<redacted>"
    assert entry["task_args"]["study_name"] == "s1"
    assert entry["task_args"]["batch_size"] == 50






def test_ledger_is_capped(ledger):
    task_failures, _ = ledger

    for i in range(task_failures.MAX_ENTRIES + 25):
        task_failures.record_failure(task=f"t{i}", error="e")

    entries = task_failures.load_failures()
    assert len(entries) == task_failures.MAX_ENTRIES
    # Oldest trimmed, newest kept
    assert entries[-1]["task"] == f"t{task_failures.MAX_ENTRIES + 24}"






def test_acknowledge_single_and_all(ledger):
    task_failures, _ = ledger

    for i in range(3):
        task_failures.record_failure(task=f"t{i}", error="e")
    entries = task_failures.load_failures()

    assert task_failures.acknowledge(entries[1]["id"]) == 1
    assert len(task_failures.unacknowledged_dead()) == 2

    assert task_failures.acknowledge("") == 2
    assert task_failures.unacknowledged_dead() == []
    # Nothing left to change
    assert task_failures.acknowledge("") == 0






def test_unacknowledged_dead_excludes_retrying(ledger):
    task_failures, _ = ledger

    task_failures.record_failure(task="pca_refresh", error="e",
                                 disposition=task_failures.DISPOSITION_RETRYING)
    task_failures.record_failure(task="collection_delete", error="e",
                                 disposition=task_failures.DISPOSITION_DEAD)

    dead = task_failures.unacknowledged_dead()
    assert [d["task"] for d in dead] == ["collection_delete"]






def test_record_never_raises(monkeypatch):
    """Bookkeeping must never turn a task failure into a crash."""
    import fyp.data_io as data_io
    from web_interface import task_failures

    def _boom(*a, **kw):
        raise RuntimeError("storage down")

    monkeypatch.setattr(data_io, "update_json", _boom)
    monkeypatch.setattr(data_io, "exists", _boom)
    monkeypatch.setattr(data_io, "load_json", _boom)

    task_failures.record_failure(task="pca_refresh", error="e")  # must not raise
    assert task_failures.load_failures() == []
    assert task_failures.acknowledge("") == 0






def test_retry_safe_set_excludes_dangerous_tasks():
    """Non-idempotent workers must never be queue-retried."""
    from web_interface.routes.process_routes import QUEUE_RETRY_SAFE

    for unsafe in ("queue_annotator", "queue_annotator_batch",
                   "consolidate_enrichment", "collection_delete",
                   "ingest_refresh", "embeddings_refresh", "ab_eval",
                   "queue_scraper_tiktok", "queue_scraper_youtube"):
        assert unsafe not in QUEUE_RETRY_SAFE, unsafe

    for safe in ("pca_refresh", "meta_refresh_groups", "timelines_refresh",
                 "study_refresh", "recode_refresh_studies"):
        assert safe in QUEUE_RETRY_SAFE, safe






@pytest.fixture
def task_client(monkeypatch):
    """Test client for the internal task endpoint with a stubbed task fn."""
    import web_interface.fyp_data_hub as hub

    monkeypatch.setattr(hub, "_IS_TASK_RUNNER", True)
    app = hub.create_app()
    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        yield client






def _stub_task(monkeypatch, name, raises=True):
    from web_interface.routes import process_routes

    def _fn(reporter=None, task_args=None):
        if raises:
            raise RuntimeError("task blew up")
        return None

    process_routes._ensure_task_functions_loaded()
    monkeypatch.setitem(process_routes.TASK_FUNCTIONS, name, _fn)






def test_retry_safe_failure_returns_503(task_client, monkeypatch, ledger):
    """A retry-safe task asks Cloud Tasks for another attempt."""
    _stub_task(monkeypatch, "pca_refresh")
    task_failures, _ = ledger

    res = task_client.post("/internal/run-task/pca_refresh", json={},
                           headers={"X-CloudTasks-TaskRetryCount": "0"})
    assert res.status_code == 503

    entry = task_failures.load_failures()[-1]
    assert entry["task"] == "pca_refresh"
    assert entry["disposition"] == "retrying"






def test_retry_safe_exhausted_returns_200(task_client, monkeypatch, ledger):
    """Once the app-side attempt bound is hit, the failure is terminal."""
    from web_interface.routes.process_routes import MAX_APP_RETRIES

    _stub_task(monkeypatch, "pca_refresh")
    task_failures, _ = ledger

    res = task_client.post(
        "/internal/run-task/pca_refresh", json={},
        headers={"X-CloudTasks-TaskRetryCount": str(MAX_APP_RETRIES - 1)})
    assert res.status_code == 200

    entry = task_failures.load_failures()[-1]
    assert entry["disposition"] == "dead"
    assert entry["retry_count"] == MAX_APP_RETRIES - 1






def test_non_retry_safe_failure_returns_200(task_client, monkeypatch, ledger):
    """A non-idempotent worker is never re-delivered; it dead-letters at once."""
    _stub_task(monkeypatch, "queue_annotator")
    task_failures, _ = ledger

    res = task_client.post("/internal/run-task/queue_annotator", json={},
                           headers={"X-CloudTasks-TaskRetryCount": "0"})
    assert res.status_code == 200

    entry = task_failures.load_failures()[-1]
    assert entry["task"] == "queue_annotator"
    assert entry["disposition"] == "dead"






def test_success_returns_200_and_no_ledger_entry(task_client, monkeypatch, ledger):
    _stub_task(monkeypatch, "pca_refresh", raises=False)
    task_failures, _ = ledger

    res = task_client.post("/internal/run-task/pca_refresh", json={})
    assert res.status_code == 200
    assert task_failures.load_failures() == []
