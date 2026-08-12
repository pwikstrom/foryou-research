"""Duplicate-chain protection in the sessions_refresh worker.

A link that outlives its Cloud Tasks dispatch deadline is retried by the
platform while the original execution keeps running; both executions
eventually try to chain. 2026-08-11/12 on prod this forked up to four
concurrent chains that re-segmented the whole corpus and then failed the
anti-partial-publish guard. Two defences:

* the initial dispatch is SETUP-ONLY (returns in seconds, so the platform
  deadline is trivially met and its retries stop forking), and
* every link must CAS-claim its successor before dispatching it, so a
  retried link's second execution stops instead of forking the chain.
"""

import fyp.data_io as data_io
from web_interface import run_sessions_refresh as rsr




def _fake_update_json(store: dict):
    def fake(storage_location: str = "", filename: str = "", mutate=None,
             default=None, **kwargs):
        doc = store.get(filename, default)
        doc = mutate(doc)
        store[filename] = doc
        return doc
    return fake




def test_first_execution_wins_the_chain_claim(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(data_io, "update_json", _fake_update_json(store))
    assert rsr._claim_chain_dispatch("runA", 3) is True




def test_duplicate_execution_of_the_same_link_loses(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(data_io, "update_json", _fake_update_json(store))
    assert rsr._claim_chain_dispatch("runA", 3) is True
    assert rsr._claim_chain_dispatch("runA", 3) is False, \
        "the platform-retried execution must not fork the chain"




def test_claims_are_per_link_and_per_run(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(data_io, "update_json", _fake_update_json(store))
    assert rsr._claim_chain_dispatch("runA", 3) is True
    assert rsr._claim_chain_dispatch("runA", 4) is True
    assert rsr._claim_chain_dispatch("runB", 3) is True




class _Reporter:
    def __init__(self):
        self.messages = []

    def log(self, msg):
        self.messages.append(msg)

    def update_progress(self, percent, message):
        pass

    def check_cancelled(self):
        return False




def test_initial_dispatch_is_setup_only(monkeypatch):
    """The task with no run_id must chain immediately without building."""
    from fyp.analysis import embedding_store, embeddings, session_explorer

    class _Backend:
        def model_id(self):
            return "test-model"

    monkeypatch.setattr(embeddings, "active_embedding_backend", lambda: _Backend())
    monkeypatch.setattr(embedding_store, "get_corpus_mean",
                        lambda model, reporter=None: ("mean", 42, "fp42"))
    monkeypatch.setattr(session_explorer, "discover_collections",
                        lambda collections: [("c1", 10), ("c2", 5)])
    monkeypatch.setattr(session_explorer, "trend_numeric_columns", lambda: ["log_plays"])
    monkeypatch.setattr(session_explorer, "sweep_stale_run_files", lambda run_id: None)

    def _boom(*args, **kwargs):
        raise AssertionError("setup link must not build a batch")

    monkeypatch.setattr(session_explorer, "build_batch", _boom)

    result = rsr.run_sessions_refresh(_Reporter(), task_args={})

    assert result is not None and result["chain"] is True
    nta = result["next_task_args"]
    assert nta["chunk_index"] == 0
    assert nta["run_id"]
    assert nta["remaining_collections"] == "c1\x1fc2"
    assert nta["total_collections"] == 2
    assert nta["corpus_mean_fp"] == "fp42"
