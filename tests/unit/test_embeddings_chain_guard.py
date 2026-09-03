"""Single-flight protection in the embeddings_refresh worker.

2026-08-14: a Cloud Tasks redelivery of a still-running embeddings batch ran
concurrently with the original, both computed the identical backlog head
slice, and both appended a uuid-named shard — 10,175 items landed twice in
the store and the duplicates crashed sessions_refresh downstream. The lease
makes every dispatch path single-flight: link 0 claims the whole run (losing
against a fresh lease held by another run, whatever dispatched it), and each
chain link claims its chunk so a redelivered link exits instead of embedding
the slice again.
"""

import time

import fyp.data_io as data_io
from web_interface import run_embeddings_refresh as rer




def _fake_update_json(store: dict):
    def fake(storage_location: str = "", filename: str = "", mutate=None,
             default=None, **kwargs):
        doc = store.get(filename, default)
        doc = mutate(doc)
        store[filename] = doc
        return doc
    return fake




def test_link0_claims_a_free_lease(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(data_io, "update_json", _fake_update_json(store))
    assert rer._claim_link("runA", 0) is True




def test_concurrent_run_loses_while_lease_is_fresh(monkeypatch):
    """A pipeline dispatch / redelivered initial task must not start a twin run."""
    store: dict = {}
    monkeypatch.setattr(data_io, "update_json", _fake_update_json(store))
    assert rer._claim_link("runA", 0) is True
    assert rer._claim_link("runB", 0) is False




def test_stale_lease_stops_blocking(monkeypatch):
    """A crashed run's lease expires instead of wedging refreshes forever."""
    store: dict = {}
    monkeypatch.setattr(data_io, "update_json", _fake_update_json(store))
    assert rer._claim_link("runA", 0) is True
    store[rer._LEASE_FILE]["updated_at"] = time.time() - rer._LEASE_STALE_S - 1
    assert rer._claim_link("runB", 0) is True




def test_redelivered_link_loses_its_chunk_claim(monkeypatch):
    """The duplicate-shard failure mode: same link executed twice."""
    store: dict = {}
    monkeypatch.setattr(data_io, "update_json", _fake_update_json(store))
    assert rer._claim_link("runA", 0) is True
    assert rer._claim_link("runA", 1) is True
    assert rer._claim_link("runA", 1) is False, \
        "the platform-retried execution must not embed the slice again"
    assert rer._claim_link("runA", 2) is True




def test_link_of_a_superseded_run_loses(monkeypatch):
    """A straggler link from an old chain cannot write into a new run."""
    store: dict = {}
    monkeypatch.setattr(data_io, "update_json", _fake_update_json(store))
    assert rer._claim_link("runA", 0) is True
    store[rer._LEASE_FILE]["updated_at"] = time.time() - rer._LEASE_STALE_S - 1
    assert rer._claim_link("runB", 0) is True
    assert rer._claim_link("runA", 5) is False




def test_release_frees_the_lease_for_the_next_run(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(data_io, "update_json", _fake_update_json(store))
    assert rer._claim_link("runA", 0) is True
    rer._release_lease("runA")
    assert rer._claim_link("runB", 0) is True




def test_release_never_clobbers_another_runs_lease(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(data_io, "update_json", _fake_update_json(store))
    assert rer._claim_link("runA", 0) is True
    rer._release_lease("runZ")
    assert rer._claim_link("runB", 0) is False




class _Reporter:
    def __init__(self):
        self.messages = []
        self.data = {}

    def log(self, msg):
        self.messages.append(msg)

    def update_progress(self, percent, message):
        pass

    def emit_data(self, data):
        # The worker reports what it changed here; the refresh pipeline reads it
        # to decide whether anything downstream needs rebuilding.
        self.data.update(data)

    def check_cancelled(self):
        return False




def test_worker_skips_when_another_run_holds_the_lease(monkeypatch):
    """The worker exits cleanly (no embed, no shard) when it loses the claim."""
    store: dict = {}
    monkeypatch.setattr(data_io, "update_json", _fake_update_json(store))
    assert rer._claim_link("live-run", 0) is True

    def _boom(**kw):
        raise AssertionError("embed_pending must not run on a lost claim")

    import fyp.embeddings as embeddings_mod
    monkeypatch.setattr(embeddings_mod, "embed_pending", _boom)
    reporter = _Reporter()
    result = rer.run_embeddings_refresh(reporter=reporter, task_args={})
    assert result is None
    assert any("skipping" in m.lower() for m in reporter.messages)
