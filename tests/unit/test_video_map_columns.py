"""video_map.parquet's column contract, exercised through a real build.

The map file is not just the Semantic Space tab's data source: the study
merge joins ``typicality_pct`` and ``niche_isolation_pct`` out of it as
numeric analysis variables. A refactor that quietly stopped writing either
column would not fail anywhere in this repo — it would surface as those
variables vanishing from the Correlations tab, after a map refresh and a
full study recode had already run in production. This pins them at the
point they are written.

Cost-free: the embedding store, annotation/scrape parquets, and all writes
are stubbed, and niche naming runs in the no-Gemini term mode.
"""

import numpy as np
import pandas as pd
import pytest

import fyp.analysis.video_map as video_map


_N_PER_NICHE = 10






def _corpus():
    """Two tight, well-separated clusters of 10 videos each."""
    rng = np.random.RandomState(0)
    item_ids = [f"v{i}" for i in range(2 * _N_PER_NICHE)]
    matrix = np.vstack([
        rng.rand(_N_PER_NICHE, 8) * 0.1,
        rng.rand(_N_PER_NICHE, 8) * 0.1 + 5.0,
    ]).astype(np.float32)
    return item_ids, matrix






@pytest.fixture
def built_map(monkeypatch):
    """Run build_niche_map against stubbed storage; return the saved frames."""
    item_ids, matrix = _corpus()
    saved: dict = {}

    annotations = pd.DataFrame({
        "item_id": item_ids,
        "video_story": (["kitten mischief indoors", "kitten mischief outdoors"] * 5
                        + ["guitar practice cover", "guitar practice session"] * 5),
        "content_category": [["pets"]] * _N_PER_NICHE + [["music"]] * _N_PER_NICHE,
        "political_score": np.linspace(0, 1, 2 * _N_PER_NICHE),
        "australian_relevance": ["yes"] * _N_PER_NICHE + ["no"] * _N_PER_NICHE,
    })
    scrapes = pd.DataFrame({
        "item_id": item_ids,
        "play_count": np.arange(2 * _N_PER_NICHE) * 1000,
        "source_platform": ["tiktok"] * 2 * _N_PER_NICHE,
    })

    def _load(storage_location, filename, columns=None, **kw):
        frame = annotations if filename == video_map.embeddings.ANNOTATIONS_FILE else scrapes
        return frame[[c for c in (columns or frame.columns) if c in frame.columns]].copy()

    monkeypatch.setattr(video_map, "_naming_available", lambda: False)
    monkeypatch.setattr(video_map.embeddings, "active_embedding_backend",
                        lambda: type("B", (), {"model_id": lambda self: "test-model"})())
    monkeypatch.setattr(video_map.embeddings, "load_embeddings",
                        lambda **kw: (item_ids, matrix))
    # No previous build on disk, so niche ids start fresh.
    monkeypatch.setattr(video_map.data_io, "exists", lambda **kw: False)
    monkeypatch.setattr(video_map.data_io, "get_parquet_columns",
                        lambda **kw: list(scrapes.columns))
    monkeypatch.setattr(video_map.data_io, "load_parquet_selective", _load)
    monkeypatch.setattr(video_map.data_io, "save_parquet",
                        lambda df, storage_location, filename, **kw: saved.update({filename: df}))
    monkeypatch.setattr(video_map.data_io, "save_json",
                        lambda data, storage_location, filename, **kw: saved.update({filename: data}))

    summary = video_map.build_niche_map(n_niches=2, map_sample=20, pca_dim=4)
    return saved, summary






def test_the_map_carries_both_analysis_measures(built_map):
    saved, summary = built_map
    map_df = saved[video_map.MAP_FILE]

    assert summary["videos"] == 2 * _N_PER_NICHE
    for col in ("typicality_pct", "niche_isolation_pct"):
        assert col in map_df.columns, col
        assert str(map_df[col].dtype) == "double[pyarrow]", col
        assert map_df[col].notna().all(), col






def test_typicality_percentile_spans_the_corpus(built_map):
    """Percentiles, not raw cosines — the join stores rank, so check the range."""
    map_df = built_map[0][video_map.MAP_FILE]

    pct = map_df["typicality_pct"].astype("float64")
    assert pct.max() == pytest.approx(100.0)
    assert 0.0 < pct.min() < 100.0
    # The raw cosine stays too — the Semantic Space overlay reads it.
    assert "typicality" in map_df.columns






def test_isolation_is_constant_within_a_niche(built_map):
    """Isolation is a property of the niche, spread to that niche's videos."""
    map_df = built_map[0][video_map.MAP_FILE]

    per_niche = map_df.groupby("niche")["niche_isolation_pct"].nunique()
    assert (per_niche == 1).all()
    # Two niches, each other's nearest: the pair ranks 0 and 100 between them.
    assert set(map_df["niche_isolation_pct"].astype("float64").unique()) == {0.0, 100.0}






def test_isolation_matches_the_niche_metadata(built_map):
    """The per-video copy must agree with the niches JSON it is spread from."""
    saved = built_map[0]
    map_df, niches = saved[video_map.MAP_FILE], saved[video_map.NICHES_FILE]

    for niche_id, meta in niches.items():
        rows = map_df[map_df["niche"] == int(niche_id)]
        assert rows["niche_isolation_pct"].astype("float64").iloc[0] == meta["isolation_pct"]
