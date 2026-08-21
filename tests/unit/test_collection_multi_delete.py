"""Deleting several collections at once: request parsing, affected-study union,
and the single worker run the route dispatches.

A collection delete reloads and rewrites the whole activity parquet, so N
collections must go out as ONE task — N concurrent tasks would each write back
a frame that still holds the others' rows.
"""

import pytest

from web_interface.routes.management.collections import (
    _affected_studies_for_collections,
    _requested_collection_ids,
)


class _Args:
    """Minimal stand-in for a Flask request args MultiDict."""

    def __init__(self, pairs):
        self._pairs = list(pairs)

    def getlist(self, key):
        return [v for k, v in self._pairs if k == key]


def test_request_ids_accepts_the_single_legacy_shape():
    assert _requested_collection_ids({"collection_id": "c1"}) == ["c1"]


def test_request_ids_accepts_a_list():
    assert _requested_collection_ids({"collection_ids": ["c1", "c2"]}) == ["c1", "c2"]


def test_request_ids_dedupes_and_keeps_order():
    body = {"collection_ids": ["c2", "c1", "c2"], "collection_id": "c1"}
    assert _requested_collection_ids(body) == ["c2", "c1"]


def test_request_ids_drops_blanks():
    assert _requested_collection_ids({"collection_ids": ["", "  ", "c1"]}) == ["c1"]


def test_request_ids_reads_repeated_query_args():
    args = _Args([("collection_id", "c1"), ("collection_id", "c2")])
    assert _requested_collection_ids(args) == ["c1", "c2"]


def test_request_ids_empty_when_nothing_supplied():
    assert _requested_collection_ids({}) == []
    assert _requested_collection_ids(_Args([])) == []


@pytest.fixture
def study_defs(monkeypatch):
    from fyp.fyp_config import fyp_cf
    from web_interface.routes.management import collections as collections_mod

    defs = {
        "study_a": {"SELECTED_COLLECTIONS": ["c1", "c9"]},
        "study_b": {"SELECTED_COLLECTIONS": ["c2"]},
        "study_c": {"SELECTED_COLLECTIONS": ["c8"]},
    }
    monkeypatch.setattr(collections_mod, "init_study_defs", lambda: None)
    monkeypatch.setitem(fyp_cf, "study_defs", defs)
    return defs


def test_affected_studies_is_the_union_over_every_id(study_defs):
    # study_a and study_b each hold one of the deleted collections; study_c
    # holds neither and must not be refreshed.
    assert sorted(_affected_studies_for_collections(["c1", "c2"])) == ["study_a", "study_b"]


def test_affected_studies_lists_each_study_once(study_defs):
    # Both ids live in study_a — it is still refreshed exactly once.
    assert _affected_studies_for_collections(["c1", "c9"]) == ["study_a"]


def test_affected_studies_empty_for_unreferenced_ids(study_defs):
    assert _affected_studies_for_collections(["nope"]) == []


def test_worker_cli_args_repeat_the_flag_once_per_collection():
    from web_interface.process_manager import _task_args_to_cli

    args = _task_args_to_cli("collection_delete", {"collection_ids": ["c1", "c2"]})
    assert args == ["--collection-id", "c1", "--collection-id", "c2"]
