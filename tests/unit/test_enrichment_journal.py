"""Enrichment history journal: the ring, the per-collection view, never raising.

The journal is the one durable, high-level record of what the enrichment
machinery did. Pins: events append newest-last and read newest-first; the ring
is bounded; a collection's view is its own events + the many-collection events
that name it + the platform-lane events its plan shares; a broken store never
turns a worker's write into an exception; every kind has a label and family.
"""

import pytest

import web_interface.services.enrichment_journal as journal


@pytest.fixture
def store(monkeypatch):
    files: dict[str, object] = {}

    def load_json(storage_location="cache", filename="", **kwargs):
        return files.get(filename)

    def update_json(storage_location="cache", filename="", mutate=None,
                    default=None, **kwargs):
        files[filename] = mutate(files.get(filename, default))
        return files[filename]

    monkeypatch.setattr(journal.data_io, "load_json", load_json)
    monkeypatch.setattr(journal.data_io, "update_json", update_json)
    return files


def _events(store):
    return (store.get(journal.JOURNAL_FILENAME) or {}).get("events") or []


def test_record_appends_and_read_returns_newest_first(store):
    journal.record("plan.armed", "Armed", collection_id="c1", target=100)
    journal.record("handoff", "3 handed", collection_id="c1", queued=3)
    assert [e["kind"] for e in _events(store)] == ["plan.armed", "handoff"]
    out = journal.read(limit=10)
    assert [e["kind"] for e in out] == ["handoff", "plan.armed"]
    assert out[0]["label"] == "Handed to annotation" and out[0]["family"] == journal.FAMILY_QUEUE
    assert out[0]["detail"] == {"queued": 3}
    assert out[0]["ts"].endswith("+00:00")       # an instant, rendered in the viewer's zone


def test_ring_is_bounded(store, monkeypatch):
    monkeypatch.setattr(journal, "MAX_EVENTS", 3)
    for n in range(5):
        journal.record("plan.tick", f"tick {n}")
    assert [e["message"] for e in _events(store)] == ["tick 2", "tick 3", "tick 4"]


def test_collection_view_keeps_own_tagged_and_platform_lane_events(store):
    journal.record("plan.armed", "own", collection_id="c1", platform="tiktok")
    journal.record("plan.armed", "other plan", collection_id="c2", platform="tiktok")
    journal.record("consolidate.finished", "tagged with c1", collection_ids=["c1", "c2"])
    journal.record("consolidate.finished", "tagged without c1", collection_ids=["c2"])
    journal.record("scrape.finished", "tiktok lane", platform="tiktok")
    journal.record("scrape.finished", "instagram lane", platform="instagram")
    journal.record("annotate.finished", "shared annotator")   # no collection, no platform
    seen = [e["message"] for e in journal.read(collection_id="c1", platform="tiktok")]
    assert seen == ["shared annotator", "tiktok lane", "tagged with c1", "own"]


def test_unfiltered_read_returns_everything(store):
    journal.record("plan.armed", "a", collection_id="c1")
    journal.record("scrape.finished", "b", platform="youtube")
    assert len(journal.read()) == 2


def test_record_never_raises(store, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("bucket down")
    monkeypatch.setattr(journal.data_io, "update_json", boom)
    journal.record("plan.armed", "Armed", collection_id="c1")   # must not raise
    assert journal.read() == []


def test_read_survives_a_corrupt_document(store):
    store[journal.JOURNAL_FILENAME] = {"events": "not a list"}
    assert journal.read() == []
    journal.record("plan.armed", "Armed")
    assert len(_events(store)) == 1       # the write recovered the document


def test_collection_ids_are_capped(store):
    journal.record("consolidate.finished", "big", collection_ids=[f"c{n}" for n in range(500)])
    assert len(_events(store)[0]["collection_ids"]) == journal.MAX_COLLECTION_IDS


def test_collection_ids_present_names_own_and_tagged(store):
    journal.record("plan.armed", "a", collection_id="c1")
    journal.record("consolidate.finished", "b", collection_ids=["c2", "c3"])
    journal.record("scrape.finished", "c", platform="tiktok")
    assert set(journal.collection_ids_present()) == {"c1", "c2", "c3"}


def test_every_kind_has_a_label_and_a_known_family():
    families = {journal.FAMILY_PLAN, journal.FAMILY_QUEUE, journal.FAMILY_WORKER,
                journal.FAMILY_CONSOLIDATE, journal.FAMILY_REFRESH, journal.FAMILY_ATTENTION}
    for kind, (label, family) in journal.KINDS.items():
        assert label and family in families, kind
    assert journal.label_for("not.a.kind") == "not.a.kind"     # unknown kinds still render


def test_finish_run_journals_a_refresh_once_even_when_reentered(store, monkeypatch):
    """The fan-out barrier can reach finish_run twice when leaves finish
    together; the history carried two "Analyses refreshed" lines one second
    apart (2026-09-05). Only the call that closes the run writes the line."""
    import web_interface.services.refresh_pipeline as rp

    state = {"record": {"run_id": "r1", "in_flight": True, "steps": {}, "fork": None,
                        "impact": {"affected_collection_ids": ["c1"],
                                   "affected_study_names": ["s1"]},
                        "origin": "consolidate_enrichment", "started_by": ""}}

    def fake_mutate(fn):
        rec = dict(state["record"])
        if fn(rec) is False:
            return None
        state["record"] = rec
        return rec

    monkeypatch.setattr(rp, "mutate_run", fake_mutate)
    monkeypatch.setattr(rp, "summarize", lambda record: "Refreshed everything.")
    rp.finish_run(run_id="r1")
    rp.finish_run(run_id="r1")            # the barrier's second arrival
    lines = [e for e in _events(store) if e["kind"] == "refresh.finished"]
    assert len(lines) == 1
    assert lines[0]["collection_ids"] == ["c1"] and lines[0]["detail"]["studies"] == 1
    assert lines[0]["message"].startswith("Analyses refreshed")


def test_actor_label_names_the_loop_and_the_system():
    assert journal.actor_label(None) == "the system"
    assert journal.actor_label("enrichment_supervisor") == "the enrichment loop"
    assert journal.actor_label("info@example.org") == "info@example.org"
