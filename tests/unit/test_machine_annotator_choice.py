"""A machine's own annotator choice, its file tag, and annotators that give way."""

import pandas as pd

import fyp.annotation.annotation_versioning as av
import fyp.annotation.backends as backends
from fyp.annotation import gemini_calls


def test_machine_choice_goes_before_the_shared_setting(monkeypatch):
    monkeypatch.setattr("fyp.annotation.backends.settings.get_annotation_backend", lambda: "gemini")
    monkeypatch.delenv(backends.MACHINE_BACKEND_ENV, raising=False)
    assert backends.machine_backend_override() is None
    assert backends.active_backend_name() == "gemini"

    monkeypatch.setenv(backends.MACHINE_BACKEND_ENV, "qwen_local")
    assert backends.active_backend_name() == "qwen_local"

    # A typo falls back to the shared setting.
    monkeypatch.setenv(backends.MACHINE_BACKEND_ENV, "qwen_locall")
    assert backends.machine_backend_override() is None
    assert backends.active_backend_name() == "gemini"


def test_machine_tag_on_raw_files(monkeypatch):
    monkeypatch.delenv(gemini_calls.MACHINE_TAG_ENV, raising=False)
    assert gemini_calls.machine_file_suffix() == ""
    monkeypatch.setenv(gemini_calls.MACHINE_TAG_ENV, "Mac Studio_01.local")
    assert gemini_calls.machine_file_suffix() == "_macstudio01local"


REGISTRY = {
    "preferred": "av_gem",
    "versions": {
        "av_gem": {"model": "gemini"},
        "av_old": {"model": "gemini"},
        "av_qwen": {"model": "qwen", "backend": "qwen_local"},
        "av_api": {"model": "qwen", "backend": "qwen_api"},
    },
}


def _frame():
    return pd.DataFrame(
        [
            # a: Qwen newest, older Gemini (not the preferred version) -> Gemini shown
            ("a", "av_old", 100, True),
            ("a", "av_qwen", 900, True),
            # b: only Qwen -> Qwen shown
            ("b", "av_qwen", 900, True),
            # c: Gemini failed, Qwen succeeded -> Qwen shown (a failure hides nothing)
            ("c", "av_gem", 950, False),
            ("c", "av_qwen", 900, True),
            # d: a hosted Qwen API row does not give way -> newest wins
            ("d", "av_old", 100, True),
            ("d", "av_api", 900, True),
        ],
        columns=["item_id", "annotation_version", "inference_ts", "annotated_ok"],
    )


def _second_machine(monkeypatch, on: bool):
    monkeypatch.setattr("fyp.annotation.backends.settings.second_machine_annotates", lambda: on)


def test_nothing_gives_way_while_the_second_machine_setting_is_off(monkeypatch):
    """A single-machine Hub with local Qwen keeps reading the newest annotation."""
    monkeypatch.setattr(av, "load_registry", lambda: REGISTRY)
    _second_machine(monkeypatch, False)
    assert av.yielding_versions() == frozenset()
    assert av.view_rule() == av.VIEW_RULE
    view = av.select_latest_view(_frame())
    assert dict(zip(view.item_id, view.annotation_version))["a"] == "av_qwen"


def test_a_yielding_annotator_never_hides_another(monkeypatch):
    monkeypatch.setattr(av, "load_registry", lambda: REGISTRY)
    _second_machine(monkeypatch, True)
    assert av.yielding_versions() == frozenset({"av_qwen"})

    for view in (av.select_latest_view(_frame()), av.select_preferred_view(_frame(), "av_gem")):
        shown = dict(zip(view.item_id, view.annotation_version))
        assert shown == {"a": "av_old", "b": "av_qwen", "c": "av_qwen", "d": "av_api"}

    # A study pinned to the Qwen version still reads Qwen rows.
    pinned = av.select_version_view(_frame(), "av_qwen")
    assert sorted(pinned.item_id) == ["a", "b", "c"]


def test_view_rule_names_the_yielding_versions(monkeypatch):
    monkeypatch.setattr(
        av, "load_registry", lambda: {"versions": {"av_gem": {}}, "preferred": None}
    )
    _second_machine(monkeypatch, True)
    assert av.view_rule() == av.VIEW_RULE
    monkeypatch.setattr(av, "load_registry", lambda: REGISTRY)
    assert av.view_rule() == av.VIEW_RULE + "|yielding:av_qwen"


def test_without_yielding_versions_the_views_are_unchanged(monkeypatch):
    monkeypatch.setattr(av, "load_registry", lambda: {"versions": {}, "preferred": None})
    _second_machine(monkeypatch, True)
    view = av.select_latest_view(_frame())
    assert dict(zip(view.item_id, view.annotation_version))["a"] == "av_qwen"
