"""New annotations and new vectors must mark the Sessions artifacts stale.

The regression this file exists for (prod, 2026-08-15/16): ``stale_only``
runs finished in ~11 s reporting "all collections up to date" while two
consolidation chains landed 6,000 newly annotated items and 5,891 new
vectors. The per-collection staleness fingerprint was computed from
``collections_recoded.parquet`` — the activity file, written only by ingest —
and probed it for an ``annotated_ok`` column it has never had, so the
annotated count was silently 0 everywhere and nothing could ever look stale.

The planner's own unit tests passed throughout, because they feed
``compute_refresh_plan`` hand-written ``(cid, n_plays, n_annotated)`` tuples
and never exercise the path that produces them. So these tests drive the
worker's setup link end to end: given per-collection records that match
exactly, a moved embedding store or annotation corpus must still rebuild.
"""

import fyp.data_io as data_io
from fyp.analysis import embedding_store, embeddings, session_explorer
from web_interface import run_sessions_refresh as rsr

WIDE = [["1970-01-01", "2100-01-01"]]




class _Reporter:
    def __init__(self):
        self.messages = []
        self.data = {}

    def log(self, msg):
        self.messages.append(str(msg))

    def update_progress(self, percent, message):
        pass

    def emit_data(self, data):
        # The worker reports what it changed here; the refresh pipeline reads it
        # to decide whether anything downstream needs rebuilding.
        self.data.update(data)

    def check_cancelled(self):
        return False




def _fake_update_json(store: dict):
    def fake(storage_location: str = "", filename: str = "", mutate=None,
             default=None, **kwargs):
        doc = mutate(store.get(filename, default))
        store[filename] = doc
        return doc
    return fake




def _install_stubs(monkeypatch, meta: dict, store_fp: str, annotations_fp: str):
    """Stub the setup link's whole I/O surface around one published build.

    Artifacts all exist and the plays schema matches, so the only thing that
    can move the plan is the fingerprint comparison under test.
    """
    class _Backend:
        def model_id(self):
            return "test-model"

        def dim(self):
            return 8

    monkeypatch.setattr(embeddings, "active_embedding_backend", lambda: _Backend())
    monkeypatch.setattr(embedding_store, "get_corpus_mean",
                        lambda model, reporter=None: ("mean", 100, store_fp))
    monkeypatch.setattr(session_explorer, "compute_coverage_spec",
                        lambda *a, **k: {"c1": WIDE})
    monkeypatch.setattr(session_explorer, "discover_covered_collections",
                        lambda coverage, collections=None: [("c1", 10)])
    monkeypatch.setattr(session_explorer, "trend_numeric_columns",
                        lambda: ["log_plays"])
    monkeypatch.setattr(session_explorer, "annotation_corpus_fingerprint",
                        lambda: annotations_fp)
    monkeypatch.setattr(session_explorer, "sweep_stale_run_files",
                        lambda run_id: None)
    monkeypatch.setattr(data_io, "exists", lambda **kwargs: True)
    monkeypatch.setattr(data_io, "load_json", lambda **kwargs: meta)
    monkeypatch.setattr(
        data_io, "get_parquet_columns",
        lambda **kwargs: list(session_explorer.plays_table(None).schema.names))
    monkeypatch.setattr(data_io, "update_json", _fake_update_json({}))




def _published_meta(store_fp: str = "fp1", annotations_fp: str = "afp1") -> dict:
    return {
        "embedding_model": "test-model",
        "params": session_explorer.default_params(),
        "trend_vars": ["log_plays"],
        "store_fingerprint": store_fp,
        "annotations_fingerprint": annotations_fp,
        "collections": {"c1": {"windows": WIDE, "n_plays": 10,
                               "built_at": "2026-08-01T00:00:00Z"}},
    }




def test_new_vectors_rebuild_even_when_play_counts_match(monkeypatch):
    """The prod failure, reproduced: activity identical, store moved."""
    _install_stubs(monkeypatch, _published_meta(store_fp="OLD-fp"),
                   store_fp="NEW-fp", annotations_fp="afp1")

    result = rsr.run_sessions_refresh(_Reporter(), task_args={"stale_only": True})

    assert result is not None, "a moved embedding store must not report noop"
    assert result["next_task_args"]["mode"] == "full"




def test_new_annotations_rebuild_even_when_play_counts_match(monkeypatch):
    """Annotations can land without the store moving (local-only backend)."""
    _install_stubs(monkeypatch, _published_meta(annotations_fp="OLD-afp"),
                   store_fp="fp1", annotations_fp="NEW-afp")

    result = rsr.run_sessions_refresh(_Reporter(), task_args={"stale_only": True})

    assert result is not None, "a rewritten annotation corpus must not report noop"
    assert result["next_task_args"]["mode"] == "full"




def test_nothing_moved_is_still_a_noop(monkeypatch):
    """The invalidators must not fire unconditionally — that is the whole
    point of keeping stale_only cheap when the corpus really is unchanged."""
    _install_stubs(monkeypatch, _published_meta(),
                   store_fp="fp1", annotations_fp="afp1")
    reporter = _Reporter()

    result = rsr.run_sessions_refresh(reporter, task_args={"stale_only": True})

    assert result is None
    assert any("up to date" in m for m in reporter.messages)




def test_setup_pins_the_annotation_fingerprint_into_the_chain(monkeypatch):
    """The final link stamps the meta from chain args, so it must travel.

    Without this the published meta would record an empty fingerprint and the
    very next run would rebuild again, forever.
    """
    _install_stubs(monkeypatch, _published_meta(store_fp="OLD-fp"),
                   store_fp="NEW-fp", annotations_fp="NEW-afp")

    result = rsr.run_sessions_refresh(_Reporter(), task_args={"stale_only": True})

    assert result["next_task_args"]["annotations_fp"] == "NEW-afp"
    assert result["next_task_args"]["corpus_mean_fp"] == "NEW-fp"




def test_annotation_fingerprint_is_empty_without_a_corpus(monkeypatch):
    """No annotations parquet is a fresh install, not a change to react to."""
    monkeypatch.setattr(data_io, "stat", lambda **kwargs: None)
    assert session_explorer.annotation_corpus_fingerprint() == ""




def test_annotation_fingerprint_tracks_size_and_mtime(monkeypatch):
    """A rewritten corpus must produce a different fingerprint.

    Row count alone would miss a re-annotation that replaces rows without
    adding any, so the fingerprint is size+mtime.
    """
    monkeypatch.setattr(data_io, "stat",
                        lambda **kwargs: {"size": 10, "mtime": 1.0})
    first = session_explorer.annotation_corpus_fingerprint()

    monkeypatch.setattr(data_io, "stat",
                        lambda **kwargs: {"size": 10, "mtime": 2.0})
    assert session_explorer.annotation_corpus_fingerprint() != first

    monkeypatch.setattr(data_io, "stat",
                        lambda **kwargs: {"size": 11, "mtime": 1.0})
    assert session_explorer.annotation_corpus_fingerprint() != first




def test_discovery_reports_no_enrichment_counts():
    """Guard the removal: the activity file cannot support one.

    Reintroducing a per-collection enrichment count here without joining a
    file that actually holds one is exactly how the original bug shipped.
    """
    import inspect

    src = inspect.getsource(session_explorer.discover_covered_collections)
    assert "annotated_ok" not in src.split('"""')[2], \
        "discover_covered_collections must not read annotated_ok from the activity file"
