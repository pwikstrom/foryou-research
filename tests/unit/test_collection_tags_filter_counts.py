"""The virtual 'Collection Tags' Explore filter counts VIDEOS, not collections.

Every other filter's ``count`` is the number of rows selecting that value
keeps. Counting collections made a tag holding three collections and 40,000
videos read as "3" — the smallest-looking option in the list.
"""

import pytest

from web_interface.routes import api_explorer_routes as routes


@pytest.fixture
def tags(monkeypatch):
    annotations = {
        "c1": {"annotation_tags": ["pilot", "wave2"]},
        "c2": {"annotation_tags": ["pilot"]},
        "c3": {"annotation_tags": ["wave2"]},
        "outside": {"annotation_tags": ["pilot"]},
    }
    monkeypatch.setattr(routes, "get_collection_tags", lambda: annotations)
    return annotations


def _counts(metadata):
    return {v["value"]: v["count"] for v in metadata["Collection Tags"]["values"]}


def test_counts_are_summed_row_counts(tags):
    meta = routes._inject_collection_tags(
        {}, ["c1", "c2", "c3"], {"c1": 1000, "c2": 40, "c3": 7},
    )
    assert _counts(meta) == {"pilot": 1040, "wave2": 1007}


def test_collections_outside_the_study_are_ignored(tags):
    # "outside" carries the pilot tag but is not in this study's frame.
    meta = routes._inject_collection_tags({}, ["c2"], {"c2": 40, "outside": 999})
    assert _counts(meta) == {"pilot": 40}


def test_values_are_ordered_by_row_count(tags):
    meta = routes._inject_collection_tags(
        {}, ["c1", "c2", "c3"], {"c1": 1, "c2": 5, "c3": 900},
    )
    assert [v["value"] for v in meta["Collection Tags"]["values"]] == ["wave2", "pilot"]


def test_a_collection_with_no_known_count_still_keeps_its_tag(tags):
    meta = routes._inject_collection_tags({}, ["c3"], {})
    assert _counts(meta) == {"wave2": 0}


def test_counts_come_from_the_baked_map_when_not_passed(tags):
    metadata = {"collection_row_counts": {"c1": 1000, "c2": 40}}
    meta = routes._inject_collection_tags(metadata, ["c1", "c2"])
    assert _counts(meta) == {"pilot": 1040, "wave2": 1000}


def test_falls_back_to_the_collection_id_filter_on_older_metadata(tags):
    # Metadata written before collection_row_counts was baked in: the
    # collection_id filter's own value counts are the only counts available.
    metadata = {
        "collection_id": {
            "type": "category",
            "values": [{"value": "c1", "count": 1000}, {"value": "c2", "count": 40}],
        },
    }
    meta = routes._inject_collection_tags(metadata, ["c1", "c2"])
    assert _counts(meta) == {"pilot": 1040, "wave2": 1000}


def test_no_tags_leaves_the_metadata_untouched(monkeypatch):
    monkeypatch.setattr(routes, "get_collection_tags", lambda: {})
    metadata = {"collection_row_counts": {"c1": 5}}
    assert routes._inject_collection_tags(metadata, ["c1"]) is metadata
    assert "Collection Tags" not in metadata
