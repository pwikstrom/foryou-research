"""Corpus-wide scraped/annotated coverage for the Edit Collections column.

The same figure a participant reads in My Collections, computed for every
collection at once. Two things must hold: the denominator is VIEW activities
(a like or a search has no scrapeable item), and "no coverage known" must stay
distinguishable from 0% — the column shows an em-dash for the first and a real
zero for the second.
"""

import pandas as pd
import pytest

import web_interface.services.collection_coverage as cov
import web_interface.services.preview_cache as preview_cache


def _activity_frame():
    rows = [
        # c1: 4 view rows (2 scraped, 1 annotated) + 2 faves that must not count
        ("c1", "play", "i1"), ("c1", "play", "i2"),
        ("c1", "observe", "i3"), ("c1", "play", "i4"),
        ("c1", "fave", "i1"), ("c1", "fave", "i9"),
        # c2: 2 view rows, neither scraped nor annotated — a real 0%, not blank
        ("c2", "play", "i5"), ("c2", "play", "i6"),
        # c3: likes only, so it has no coverage at all
        ("c3", "fave", "i7"),
    ]
    df = pd.DataFrame(rows, columns=["collection_id", "activity_type", "item_id"])
    df["collection_id"] = pd.Categorical(df["collection_id"],
                                         categories=["c1", "c2", "c3", "ghost"])
    return df


def _status_frame():
    return pd.DataFrame({
        "item_id": ["i1", "i2", "i8"],
        "scraped_ok": [True, True, True],
        "annotated_ok": [True, False, True],
    })


@pytest.fixture(autouse=True)
def clean_cache():
    cov._corpus_cache.clear()
    yield
    cov._corpus_cache.clear()


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(cov.data_io, "load_parquet_selective",
                        lambda **kw: _activity_frame())
    monkeypatch.setattr(preview_cache, "get_enrichment_status_cached",
                        lambda: _status_frame())
    return monkeypatch


def test_view_denominator_and_real_zero(patched):
    out = cov.corpus_coverage()
    assert out["c1"]["pct_scraped"] == pytest.approx(0.5)     # 2 of 4 views
    assert out["c1"]["pct_annotated"] == pytest.approx(0.25)  # 1 of 4 views
    # No enrichment yet is 0%, and says so — an em-dash would read as "unknown".
    assert out["c2"] == {"pct_scraped": 0.0, "pct_annotated": 0.0}
    # No view rows at all: no entry, so the column stays em-dashed.
    assert "c3" not in out and "ghost" not in out


def test_missing_status_table_yields_nothing(patched, monkeypatch):
    monkeypatch.setattr(preview_cache, "get_enrichment_status_cached", lambda: None)
    assert cov.corpus_coverage() == {}


def test_unreadable_corpus_is_not_cached(patched, monkeypatch):
    def _boom(**kw):
        raise FileNotFoundError("collections_recoded.parquet")

    monkeypatch.setattr(cov.data_io, "load_parquet_selective", _boom)
    assert cov.corpus_coverage() == {}
    # A failure must not poison the TTL: the next call re-reads.
    monkeypatch.setattr(cov.data_io, "load_parquet_selective", lambda **kw: _activity_frame())
    assert "c1" in cov.corpus_coverage()


def test_cache_and_force(patched):
    first = cov.corpus_coverage()
    patched.setattr(cov.data_io, "load_parquet_selective",
                    lambda **kw: _activity_frame().iloc[:0])
    assert cov.corpus_coverage() == first          # warm cache
    assert cov.corpus_coverage(force=True) == {}   # ?fresh=1 re-scans


def test_matches_my_collections(patched):
    """The two tables report one number, so they share one implementation."""
    import web_interface.services.my_collections_service as svc

    svc.invalidate_cache()
    frame = _activity_frame()
    frame["source_platform"] = "tiktok"
    frame["data_source"] = "ddp"
    patched.setattr(svc.data_io, "load_parquet_selective", lambda **kw: frame)
    _, mine = svc._platforms_and_coverage("user", ["c1", "c2", "c3"])
    svc.invalidate_cache()
    assert mine == cov.corpus_coverage()
