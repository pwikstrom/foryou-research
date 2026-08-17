"""Per-card staleness badges on the Dataset Assembly page.

Every step of the refresh pipeline now carries a "(N ... need refresh)" badge,
not just the four study/collection-scoped ones. The corpus-global steps
(embeddings, semantic map, sessions) are display-only: they must report their
own staleness without gating the consolidation impact, because the pipeline can
legitimately skip them — a local-only embedding backend is not dispatchable
from Cloud Run, and gating on a step that will never run would pin the impact
forever.
"""

import pytest

from web_interface.services import stats_service as ss


IMPACT_TS = "2026-08-16T10:00:00+00:00"
BEFORE = "2026-08-16T09:00:00+00:00"
AFTER = "2026-08-16T11:00:00+00:00"

CORPUS_STEPS = ["embeddings_refresh", "video_map_refresh", "sessions_refresh"]


@pytest.fixture
def stats(monkeypatch):
    """Isolate process_stats and neuter its persistence."""
    store: dict = {}
    monkeypatch.setattr(ss, "process_stats", store)
    monkeypatch.setattr(ss, "processes", {})
    monkeypatch.setattr(ss, "load_process_stats", lambda: None)
    monkeypatch.setattr(ss, "save_process_stats", lambda: None)
    return store


def _seed(stats, *, studies, collections, new_annotations, last_success=None):
    stats["consolidate_enrichment"] = {
        "consolidation_impact": {
            "timestamp": IMPACT_TS,
            "affected_study_names": studies,
            "affected_collection_ids": collections,
            "new_annotation_item_count": new_annotations,
        }
    }
    for name, ts in (last_success or {}).items():
        stats[name] = {"last_success": ts}


def test_every_pipeline_step_reports_a_note(stats):
    _seed(stats, studies=["s1", "s2"], collections=["c1"], new_annotations=1500)

    out = ss._evaluate_consolidation_staleness()
    procs = out["processes"]

    assert out["has_impact"] is True
    assert procs["recode_refresh_studies"]["note"] == "(2 studies need refresh)"
    assert procs["timelines_refresh"]["note"] == "(1 collection needs refresh)"
    assert procs["embeddings_refresh"]["note"] == "(1,500 new annotations to embed)"
    assert procs["video_map_refresh"]["note"] == "(1,500 new embeddings to map)"
    assert procs["sessions_refresh"]["note"] == "(1 collection needs refresh)"


def test_sessions_falls_back_to_annotations_when_no_collection_moved(stats):
    _seed(stats, studies=[], collections=[], new_annotations=42)

    procs = ss._evaluate_consolidation_staleness()["processes"]

    assert procs["sessions_refresh"]["stale"] is True
    assert procs["sessions_refresh"]["note"] == "(new annotations — refresh needed)"


def test_corpus_steps_do_not_gate_impact_resolution(stats):
    """The four study/collection steps have run; the corpus ones have not."""
    _seed(
        stats, studies=["s1"], collections=["c1"], new_annotations=99,
        last_success={
            "recode_refresh_studies": AFTER,
            "meta_refresh_groups": AFTER,
            "pca_refresh": AFTER,
            "timelines_refresh": AFTER,
            # embeddings / video_map / sessions never ran for this impact.
        },
    )

    out = ss._evaluate_consolidation_staleness()

    assert out["has_impact"] is False, "corpus-only staleness must not pin the impact"
    assert all(out["processes"][s]["stale"] for s in CORPUS_STEPS)
    assert all(out["processes"][s]["gates"] is False for s in CORPUS_STEPS)


def test_no_new_annotations_means_no_corpus_badges(stats):
    _seed(stats, studies=["s1"], collections=[], new_annotations=0,
          last_success={"recode_refresh_studies": BEFORE})

    procs = ss._evaluate_consolidation_staleness()["processes"]

    assert procs["embeddings_refresh"]["stale"] is False
    assert procs["video_map_refresh"]["stale"] is False
    assert procs["sessions_refresh"]["stale"] is False
    assert all(procs[s]["note"] == "" for s in CORPUS_STEPS)
    # The gating side is untouched: the study refresh is still overdue.
    assert procs["recode_refresh_studies"]["stale"] is True


def test_a_step_that_ran_after_the_impact_is_fresh(stats):
    _seed(stats, studies=[], collections=[], new_annotations=10,
          last_success={"embeddings_refresh": AFTER, "video_map_refresh": BEFORE})

    procs = ss._evaluate_consolidation_staleness()["processes"]

    assert procs["embeddings_refresh"]["stale"] is False
    assert procs["video_map_refresh"]["stale"] is True
