"""Regression tests for the fresh-install bugs found in the 2026-07 field log.

Covers:
    * _workers_blocking_consolidate detects per-platform scraper processes
      (the pre-fix code checked the retired flat name ``queue_scraper``, so a
      running scraper never blocked/armed a consolidate).
    * _consolidate_blockers treats an active drain lease as a blocker.
    * add_local_time_features tolerates an empty, columnless frame (a refresh
      that ingested nothing used to die with KeyError 'tz_offset').
    * TikTokDDPCollection.load_single_raw raises a clear error for a non-JSON
      upload (used to die with UnboundLocalError on the .zip export).
    * accepted_upload_suffixes declarations used by the upload validation.
    * The AIO AWS auto-fetch gate ([features].aio_aws_fetch, default
      Cloud-Run-only).
"""

import types

import pandas as pd






def _fake_running_proc():
    """A stand-in for a subprocess handle that reports 'still running'."""
    proc = types.SimpleNamespace()
    proc.poll = lambda: None
    return proc






def test_blocking_consolidate_detects_per_platform_scraper(monkeypatch):
    from web_interface import process_manager
    from web_interface.services import worker_status

    assert process_manager.SCRAPER_PROCESS_NAMES, "contract must register platforms"
    scraper_name = process_manager.SCRAPER_PROCESS_NAMES[0]

    monkeypatch.setitem(worker_status.processes, scraper_name, {"proc": _fake_running_proc()})
    try:
        assert scraper_name in worker_status._workers_blocking_consolidate()
    finally:
        worker_status.processes.pop(scraper_name, None)






def test_blocking_consolidate_detects_annotator(monkeypatch):
    from web_interface.services import worker_status

    monkeypatch.setitem(worker_status.processes, "queue_annotator", {"proc": _fake_running_proc()})
    try:
        assert "queue_annotator" in worker_status._workers_blocking_consolidate()
    finally:
        worker_status.processes.pop("queue_annotator", None)






def test_consolidate_blockers_include_drain_lease(monkeypatch):
    from web_interface.routes.management import enrichment

    monkeypatch.setattr(enrichment, "_workers_blocking_consolidate", lambda: [])
    monkeypatch.setattr(enrichment, "_active_drain_leases", lambda: {"tiktok": {"host": "x"}})
    assert enrichment._consolidate_blockers() == ["local drain (tiktok)"]

    monkeypatch.setattr(enrichment, "_active_drain_leases", lambda: {})
    assert enrichment._consolidate_blockers() == []






def test_add_local_time_features_empty_frame():
    from fyp.ingest.base import ForYouCollection

    stub = types.SimpleNamespace(data=pd.DataFrame())
    # Must not raise (used to KeyError on 'tz_offset') and must not invent columns.
    ForYouCollection.add_local_time_features(stub)
    assert len(stub.data) == 0






def test_tiktok_ddp_load_single_raw_clear_error_on_non_json(monkeypatch):
    import pytest

    from fyp.ingest import tiktok as tiktok_mod

    # data_io.load_json swallows decode errors and returns None (e.g. for a
    # raw .zip upload) — the loader must turn that into a clear ValueError.
    monkeypatch.setattr(tiktok_mod.data_io, "load_json", lambda **kw: None)
    stub = types.SimpleNamespace(raw_path="ddp_raw", verbose=False)
    with pytest.raises(ValueError, match="not readable as a JSON document"):
        tiktok_mod.TikTokDDPCollection.load_single_raw(stub, "TikTok_Data_123.zip")






def test_accepted_upload_suffixes_declarations():
    from fyp.ingest.base import ForYouBaseCollection
    from fyp.ingest.instagram import InstagramDDPCollection
    from fyp.ingest.tiktok import TikTokDDPCollection, TikTokZeeschuimerCollection
    from fyp.ingest.youtube import YouTubeDDPCollection

    assert TikTokDDPCollection.accepted_upload_suffixes() == [".json"]
    assert TikTokZeeschuimerCollection.accepted_upload_suffixes() == [".ndjson"]
    assert InstagramDDPCollection.accepted_upload_suffixes() == [".zip"]
    assert YouTubeDDPCollection.accepted_upload_suffixes() == [".zip"]
    assert ForYouBaseCollection.accepted_upload_suffixes() == []






def test_aio_aws_fetch_gate(monkeypatch):
    from fyp.fyp_config import fyp_cf
    from fyp.ingest.tiktok import TikTokAIOCollection

    features = fyp_cf.setdefault("features", {})

    # Key absent → Cloud Run only (K_SERVICE decides).
    monkeypatch.delitem(features, "aio_aws_fetch", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert TikTokAIOCollection._aws_fetch_enabled() is False
    monkeypatch.setenv("K_SERVICE", "fyp-task-runner")
    assert TikTokAIOCollection._aws_fetch_enabled() is True

    # Explicit config wins over the environment in both directions.
    monkeypatch.setitem(features, "aio_aws_fetch", False)
    assert TikTokAIOCollection._aws_fetch_enabled() is False
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setitem(features, "aio_aws_fetch", True)
    assert TikTokAIOCollection._aws_fetch_enabled() is True
