"""Per-collection scraped/annotated coverage for the My Collections table.

Coverage semantics: the share of a collection's VIEW activities (play/observe)
whose item is scraped / annotated in enrichment_status — never total_events,
which counts likes/searches that have no scrapeable item. Missing status data
must surface as None (UI em-dash), not a false 0%.
"""

import pandas as pd
import pytest

import web_interface.services.my_collections_service as svc
import web_interface.services.preview_cache as preview_cache


def _activity_frame():
    rows = [
        # c1: 4 view rows (2 scraped, 1 annotated) + 2 faves that must not count
        ("c1", "play", "i1"), ("c1", "play", "i2"),
        ("c1", "observe", "i3"), ("c1", "play", "i4"),
        ("c1", "fave", "i1"), ("c1", "fave", "i9"),
        # c2: 2 view rows (1 scraped, 0 annotated)
        ("c2", "play", "i5"), ("c2", "play", "i6"),
    ]
    df = pd.DataFrame(rows, columns=["collection_id", "activity_type", "item_id"])
    df["source_platform"] = "tiktok"
    df["data_source"] = "ddp"
    # Categorical with an unused category: groupby must pass observed=True or
    # a phantom "ghost" collection appears with NaN coverage.
    df["collection_id"] = pd.Categorical(df["collection_id"], categories=["c1", "c2", "ghost"])
    return df


def _status_frame():
    return pd.DataFrame({
        "item_id": ["i1", "i2", "i5", "i7"],
        "scraped_ok": [True, True, True, True],
        "annotated_ok": [True, False, False, True],
    })


@pytest.fixture(autouse=True)
def clean_cache():
    svc.invalidate_cache()
    yield
    svc.invalidate_cache()


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(svc.data_io, "load_parquet_selective",
                        lambda **kw: _activity_frame())
    monkeypatch.setattr(preview_cache, "get_enrichment_status_cached",
                        lambda: _status_frame())
    return monkeypatch


def test_coverage_uses_view_denominator(patched):
    _, coverage = svc._platforms_and_coverage("user", ["c1", "c2"])
    assert coverage["c1"]["pct_scraped"] == pytest.approx(0.5)     # 2 of 4 views
    assert coverage["c1"]["pct_annotated"] == pytest.approx(0.25)  # 1 of 4 views
    assert coverage["c2"]["pct_scraped"] == pytest.approx(0.5)
    assert coverage["c2"]["pct_annotated"] == 0.0


def test_no_phantom_categories(patched):
    platforms, coverage = svc._platforms_and_coverage("user", ["c1", "c2"])
    assert "ghost" not in coverage
    assert "ghost" not in platforms


def test_missing_status_table_yields_no_coverage(patched, monkeypatch):
    monkeypatch.setattr(preview_cache, "get_enrichment_status_cached", lambda: None)
    _, coverage = svc._platforms_and_coverage("user", ["c1", "c2"])
    assert coverage == {}


def test_cache_ttl_and_invalidation(patched):
    _, first = svc._platforms_and_coverage("user", ["c1", "c2"])
    # Warm cache: a changed underlying frame is NOT reflected...
    patched.setattr(svc.data_io, "load_parquet_selective",
                    lambda **kw: _activity_frame().iloc[:0])
    _, warm = svc._platforms_and_coverage("user", ["c1", "c2"])
    assert warm == first
    # ...until invalidate_cache() clears it.
    svc.invalidate_cache()
    _, cold = svc._platforms_and_coverage("user", ["c1", "c2"])
    assert cold == {}


def test_bundle_cache_eviction_cap():
    import time
    svc._bundle_cache.clear()
    now = time.time()
    for i in range(svc._BUNDLE_CACHE_MAX + 10):
        svc._evict_bundle_cache(now)
        svc._bundle_cache[("subset", i)] = (now + i * 1e-3, {})
    assert len(svc._bundle_cache) <= svc._BUNDLE_CACHE_MAX
    # Expired entries are swept on write regardless of the cap.
    svc._bundle_cache[("stale",)] = (now - svc._CACHE_TTL_S - 1, {})
    svc._evict_bundle_cache(time.time())
    assert ("stale",) not in svc._bundle_cache
    svc._bundle_cache.clear()


def test_list_owned_collections_carries_coverage(patched, monkeypatch):
    monkeypatch.setattr(svc, "collections_for_user", lambda u, **kw: ["c1", "c2"])
    monkeypatch.setattr(svc, "load_withdrawals", lambda **kw: {})
    monkeypatch.setattr(svc, "get_collection_tags", lambda **kw: {})
    monkeypatch.setattr(svc, "_load_metadata_personas", lambda cids=None: None)
    monkeypatch.setattr(svc, "_pending_uploads_for_user", lambda u: {})
    out = {c["collection_id"]: c for c in svc.list_owned_collections("user")}
    assert out["c1"]["pct_scraped"] == pytest.approx(0.5)
    assert out["c1"]["pct_annotated"] == pytest.approx(0.25)
    assert out["c1"]["source_platform"] == "tiktok"
