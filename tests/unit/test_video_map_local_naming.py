"""Term-based niche naming: the no-Gemini fallback path in video_map."""

import numpy as np
import pandas as pd
import pytest

import fyp.analysis.video_map as video_map






def _fixture_corpus():
    """Two 10-video niches with distinct, min_df/max_df-surviving vocab."""
    item_ids = [f"v{i}" for i in range(20)]
    labels = np.array([0] * 10 + [1] * 10)
    reduced = np.vstack([
        np.random.RandomState(0).rand(10, 4) + 0.0,
        np.random.RandomState(1).rand(10, 4) + 10.0,
    ]).astype(np.float32)
    stories = pd.Series(
        ["kitten mischief indoors" if i % 2 else "kitten mischief outdoors" for i in range(10)]
        + ["guitar practice session" if i % 2 else "guitar practice cover" for i in range(10)]
    )
    categories = pd.Series(["pets"] * 10 + ["music"] * 10)
    return item_ids, labels, reduced, stories, categories






def test_name_niches_terms_mode_is_deterministic(monkeypatch):
    monkeypatch.setattr(video_map, "_naming_available", lambda: False)
    item_ids, labels, reduced, stories, categories = _fixture_corpus()

    meta1 = video_map._name_niches(item_ids, labels, reduced, stories, categories)
    meta2 = video_map._name_niches(item_ids, labels, reduced, stories, categories)

    names1 = {n: m["name"] for n, m in meta1.items()}
    names2 = {n: m["name"] for n, m in meta2.items()}
    assert names1 == names2
    # Real term-derived labels, not the generic placeholder.
    assert all(not name.startswith("Niche ") for name in names1.values())
    # Unique across niches.
    assert len(set(names1.values())) == len(names1)






def test_name_niches_terms_mode_carried_names_win(monkeypatch):
    monkeypatch.setattr(video_map, "_naming_available", lambda: False)
    item_ids, labels, reduced, stories, categories = _fixture_corpus()

    meta = video_map._name_niches(
        item_ids, labels, reduced, stories, categories,
        carried_names={0: "Carried Cats"},
    )
    assert meta[0]["name"] == "Carried Cats"
    assert meta[1]["name"] != "Carried Cats"






def test_term_name_from_terms():
    meta = {7: {"terms": ["cat mischief", "funny pets", "zoomies"]}}
    assert video_map._term_name(meta, 7) == "Cat Mischief / Funny Pets"
    assert video_map._term_name({7: {"terms": []}}, 7) == "Niche 7"






def test_dedupe_with_noop_ask_fn_resolves_collisions():
    """With ask_fn returning None, collisions resolve via distinctive terms."""
    meta = {
        1: {"name": "Pets", "size": 100, "terms": ["dogs"]},
        2: {"name": "Pets", "size": 50, "terms": ["cats"]},
        3: {"name": "Pets", "size": 10, "terms": ["birds"]},
    }
    renamed = video_map._dedupe_niche_names(meta, lambda n: "", lambda p: None)
    assert renamed == 2
    names = [m["name"] for m in meta.values()]
    assert len(set(names)) == 3
    assert meta[1]["name"] == "Pets"  # largest keeps the label






def test_gemini_path_untouched_when_available(monkeypatch):
    """With naming available, the LLM path is used (client requested)."""
    monkeypatch.setattr(video_map, "_naming_available", lambda: True)
    sentinel = RuntimeError("naming client requested")

    def _boom():
        raise sentinel

    monkeypatch.setattr(video_map, "_get_naming_client", _boom)
    item_ids, labels, reduced, stories, categories = _fixture_corpus()
    with pytest.raises(RuntimeError, match="naming client requested"):
        video_map._name_niches(item_ids, labels, reduced, stories, categories)
