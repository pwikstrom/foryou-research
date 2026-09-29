"""A collection delete must not refresh a study it has just removed.

2026-09-14: a participant's only collection was deleted; the worker dispatched
a ``study_refresh`` for their Just Me study, then the participant-study
reconciliation removed that study. The refresh retried four times against a
missing definition and dead-lettered. Three guards now cover it: the worker
reconciles first and dispatches only for studies that still exist, the
refresh worker treats a vanished study as a no-op, and ``forget_process_stats``
removes the stale worker-board entry.
"""

import pytest


def test_refresh_targets_skip_removed_and_composed_studies():
    from web_interface.workers.run_collection_delete import _refresh_targets

    defs = {
        "kept": {"SELECTED_COLLECTIONS": []},
        "__me_plus__p@example.org": {"COMPOSE": True, "SYSTEM": True},
    }
    affected = ["kept", "__me__p@example.org", "__me_plus__p@example.org"]
    assert _refresh_targets(affected, defs) == ["kept"]
    assert _refresh_targets([], defs) == []


def test_reconciliation_runs_before_the_refresh_dispatch():
    """Order in the worker source: the participant sync precedes step 10."""
    import inspect

    from web_interface.workers import run_collection_delete as mod

    src = inspect.getsource(mod.run_collection_delete)
    assert src.index("sync_for_cids(") < src.index("_refresh_targets(affected_studies")


def test_study_refresh_of_a_vanished_study_is_a_noop(monkeypatch):
    import fyp.analysis.studies as studies
    from fyp.core.fyp_config import fyp_cf
    from web_interface.workers.run_study_refresh import run_study_refresh

    monkeypatch.setattr(studies, "init_study_defs", lambda: None)
    monkeypatch.setitem(fyp_cf, "study_defs", {"other": {}})

    class _Reporter:
        def __init__(self):
            self.lines = []

        def log(self, msg):
            self.lines.append(msg)

        def update_progress(self, *_a, **_k):
            pass

    reporter = _Reporter()
    assert run_study_refresh(reporter=reporter, task_args={"study_name": "gone"}) is None
    assert any("no longer exists" in line for line in reporter.lines)


def test_forget_process_stats_removes_only_that_key(monkeypatch):
    import web_interface.tasks.process_manager as pm

    store = {
        "study_refresh__gone": {"last_run_outcome": "Fail"},
        "pca_refresh": {"last_run_outcome": "Success"},
    }

    def _load():
        pm.process_stats.clear()
        pm.process_stats.update(store)
        pm._snapshot_process_stats()

    saved = {}

    def _save():
        saved.clear()
        saved.update(pm.process_stats)

    monkeypatch.setattr(pm, "load_process_stats", _load)
    monkeypatch.setattr(pm, "save_process_stats", _save)

    assert pm.forget_process_stats("study_refresh__gone") is True
    assert saved == {"pca_refresh": {"last_run_outcome": "Success"}}
    assert pm.forget_process_stats("never_there") is False


def test_moviepy_audio_reader_destructor_is_quiet():
    pytest.importorskip("moviepy")
    from moviepy.audio.io.readers import FFMPEG_AudioReader

    from fyp.scrape.slideshow import _patch_moviepy_audio_reader_del

    _patch_moviepy_audio_reader_del()
    # An instance whose __init__ raised before assigning ``proc``.
    half_built = object.__new__(FFMPEG_AudioReader)
    half_built.close()  # would raise AttributeError without the shim


def test_worker_dispatches_name_their_module():
    """A worker that starts another worker must pass its module.

    ``start_process`` spawns ``python -m <module>`` locally; ``None`` is only
    valid for callers that dispatch on Cloud Run alone. collection_delete once
    passed ``None`` unguarded, so every local delete failed its study refreshes.
    """
    import ast
    from pathlib import Path

    workers = Path(__file__).resolve().parents[2] / "web_interface" / "workers"
    calls = []
    for path in sorted(workers.glob("run_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None)) == "start_process"
            ):
                calls.append((path.name, node))
    assert calls, "no start_process calls found; the guard is looking in the wrong place"
    for name, call in calls:
        module = call.args[1] if len(call.args) > 1 else None
        assert module is not None, f"{name}:{call.lineno} start_process without a module"
        assert not (isinstance(module, ast.Constant) and module.value is None), (
            f"{name}:{call.lineno} start_process(..., None, ...) from a worker"
        )
