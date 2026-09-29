"""Shared pytest configuration for tests/unit.

Files listed in ``collect_ignore`` are self-runner integration scripts: they
have their own ``main()`` harness, expect a ``client`` argument that is not a
pytest fixture, and snapshot/restore the live var-schema and presentation
stores, so they must not run in a shared gate. Run them directly
(``python tests/unit/<file>.py``). When one is converted to pytest style,
delete its entry here.
"""

import sys

import pytest

collect_ignore = [
    "test_annotation_contract_api.py",
    "test_annotation_contract_editor.py",
    "test_var_schema_api.py",
]


@pytest.fixture(autouse=True)
def _reset_perf_caches():
    """Reset the web layer's module-level read caches after every test.

    The sessions routes and admin settings hold TTL/fingerprint caches so hot
    request paths stop re-reading storage. Tests monkeypatch the underlying
    reads, so a value cached in one test must never leak into the next.
    """
    yield
    admin = sys.modules.get("web_interface.services.admin_settings")
    if admin is not None:
        admin._SETTINGS_CACHE.update({"ts": 0.0, "data": None})
    routes = sys.modules.get("web_interface.routes.api_sessions_routes")
    if routes is not None:
        routes._DETAIL_RESPONSE_CACHE.clear()
    data = sys.modules.get("web_interface.services.sessions_data")
    if data is not None:
        data._STAT_CACHE.clear()
        data._RANGES_CACHE.clear()
        data._INDEX_CACHE.update({"fingerprint": None, "df": None, "search": None})
        data._META_CACHE.update({"fingerprint": None, "meta": None})
        data._EPISODES_CACHE.update({"fingerprint": None, "df": None})
        data._WINDOWS_CACHE.update({"fingerprint": None, "df": None})
        data._DIRECTED_CACHE.update({"fingerprint": None, "cut": None, "counts": None})
        data._FLAGS_CACHE.update({"key": None, "model": None, "flags": None, "emb_index": None})
        data._FEAT_CACHE.update({"key": None, "df": None, "trend_cols": None})
        data._COLLECTION_PLAYS_CACHE.clear()
        data._EPVMAX_CACHE.update({"key": None, "df": None})
        data._MEAN_CACHE.update({"ts": 0.0, "model": None, "mean": None})


@pytest.fixture(autouse=True)
def _scrape_batches_see_the_network_up(monkeypatch):
    """Report the machine online to every scrape batch under test.

    ``download_video_threads`` probes the real network after any failed item
    (``fyp.scrape.connectivity``). Tests fake failures on purpose; without
    this a sandbox with no network would read them as an outage and wait it
    out. Tests of the gate itself inject their own probe.
    """
    from fyp.scrape import connectivity

    monkeypatch.setattr(connectivity, "probe_online", lambda *a, **k: True)
