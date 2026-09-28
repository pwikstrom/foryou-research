#!/usr/bin/env python3
"""Tests for the network-outage gate in the scrape batch (fyp.scrape.connectivity).

Covers the 2026-09-27 local YouTube drain: the laptop's network dropped for
~6 minutes, every item failed with ``[Errno 8] nodename nor servname
provided`` (transient:network), and after 25 in a row the transient-storm
guard stopped the run at batch 6 of 20 and tried to raise a scraper alert — an
alert that holds the platform's enrichment plans until dismissed. An outage
is the machine's fault, not the platform's: the batch now waits it out,
re-runs the items that failed meanwhile, and keeps the outage out of every
guard, budget and alert.

Usage:
    python tests/unit/test_scrape_offline_gate.py
    pytest tests/unit/test_scrape_offline_gate.py
"""

import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from fyp.scrape import connectivity, scrape
from fyp.scrape.youtube_dl import YouTubeScraper

STORM_THRESHOLD = 5  # small, so an unguarded outage would certainly trip it




def _failure(category: str) -> pd.DataFrame:
    """An empty fetch result carrying a failure category, like a real miss."""
    empty = pd.DataFrame()
    empty.attrs["error_type"] = category
    return empty




def _metadata_row(item_id: str) -> pd.DataFrame:
    """A >10-column single-row frame like a real fetch result."""
    return pd.DataFrame([{
        "item_id": item_id, "desc": "x", "create_time_raw": pd.Timestamp("2026-01-01"),
        "duration_raw": 30, "author_id": "a", "author_handle": "@a",
        "author_name_raw": "A", "play_count_raw": 1, "fave_count_raw": 0,
        "comment_count_raw": 0, "share_count_raw": 0, "video_downloaded": True,
    }])




class _Network:
    """A switchable network: fetches fail and probes answer False while down.

    ``up_after_probes`` brings it back after that many offline probes — the
    outage ends while the gate waits, as a Wi-Fi drop does.
    """

    def __init__(self, up_after_probes=None):
        self.up = True
        self.up_after_probes = up_after_probes
        self.offline_probes = 0
        self.lock = threading.Lock()

    def probe(self, *args, **kwargs):
        with self.lock:
            if not self.up:
                self.offline_probes += 1
                if (self.up_after_probes is not None
                        and self.offline_probes >= self.up_after_probes):
                    self.up = True
            return self.up




def _run_batch(ids, fake_dl, net, max_wait=5.0, dry_run=False, extra=()):
    """Run download_video_threads against a fake fetch layer and network.

    The bucket-facing steps are stubbed (media probe, failed record, save),
    alerts are captured, and the gate polls every 10 ms.
    """
    alerts, cleared = [], []
    patches = [
        patch.object(scrape, "download_single_video", side_effect=fake_dl),
        patch.object(scrape, "_transient_storm_threshold", return_value=STORM_THRESHOLD),
        patch.object(scrape.scrape_versioning, "ensure_active_version_registered",
                     lambda: None),
        patch.object(YouTubeScraper, "inter_request_delay", return_value=0.0),
        patch.object(scrape, "check_existing_media", return_value={}),
        patch.object(scrape.data_io, "save_json", side_effect=lambda **kw: None),
        patch.object(scrape, "_canonicalize_recode_save",
                     side_effect=lambda results, *a, **k: results),
        patch.object(scrape.scraper_alerts, "raise_alert",
                     side_effect=lambda **kw: alerts.append(kw)),
        patch.object(scrape.scraper_alerts, "clear_alert",
                     side_effect=lambda *a, **kw: cleared.append(a)),
        patch.object(connectivity, "probe_online", side_effect=net.probe),
        patch.object(connectivity, "offline_max_wait", return_value=max_wait),
        patch.object(connectivity, "_POLL_SECONDS", 0.01),
        *extra,
    ]
    for p in patches:
        p.start()
    try:
        results, perm, trans = scrape.download_video_threads(
            interesting_videos=ids, max_workers=2, dry_run=dry_run, platform="youtube")
    finally:
        for p in reversed(patches):
            p.stop()
    return results, perm, trans, alerts, cleared




def test_gate_recovers_after_outage():
    """Offline probes, then online: the gate reports RECOVERED once."""
    net = _Network(up_after_probes=4)
    net.up = False
    gate = connectivity.ConnectivityGate(hosts=("h",), max_wait=5, poll=0.01, probe=net.probe)

    assert gate.check() == connectivity.RECOVERED
    assert gate.outages == 1 and not gate.gave_up
    assert gate.check() == connectivity.ONLINE
    print("PASS: gate recovers after an outage")




def test_gate_gives_up_past_max_wait():
    """An outage longer than the wait gives up, and stays given up."""
    net = _Network()
    net.up = False
    gate = connectivity.ConnectivityGate(hosts=("h",), max_wait=0.05, poll=0.01, probe=net.probe)

    assert gate.check() == connectivity.GAVE_UP
    assert gate.gave_up
    net.up = True
    assert gate.check() == connectivity.GAVE_UP, "a gate that gave up stays given up"
    print("PASS: gate gives up past the max wait")




def test_gate_releases_on_stop_event_without_giving_up():
    """Another guard stopping the batch releases a waiting gate, not marks it gave-up."""
    net = _Network()
    net.up = False
    gate = connectivity.ConnectivityGate(hosts=("h",), max_wait=5, poll=0.01, probe=net.probe)
    stop = threading.Event()
    stop.set()

    assert gate.check(stop) == connectivity.GAVE_UP
    assert not gate.gave_up
    print("PASS: stop event releases the gate")




def test_outage_mid_batch_is_waited_out():
    """The 2026-09-27 shape: the network drops mid-batch and comes back.

    Every item that failed while offline is re-run and succeeds; no storm, no
    alert, nothing left transient — although far more than STORM_THRESHOLD
    consecutive network failures happened.
    """
    ids = [f"v{i}" for i in range(40)]
    net = _Network(up_after_probes=3)
    calls = {"n": 0}
    lock = threading.Lock()

    def fake_dl(video_id=None, **kwargs):
        with lock:
            calls["n"] += 1
            if calls["n"] == 10:
                net.up = False  # the Wi-Fi drops
            up = net.up
        return _metadata_row(video_id) if up else _failure("network")

    results, perm, trans, alerts, _ = _run_batch(ids, fake_dl, net)

    assert results.attrs.get("transient_storm_tripped") is False
    assert results.attrs.get("offline") is False
    assert alerts == [], f"an outage must not raise a scraper alert: {alerts}"
    assert perm == [] and trans == [], (perm, trans)
    assert sorted(results["item_id"]) == sorted(ids)
    print("PASS: an outage mid-batch is waited out")




def test_media_leg_failure_during_outage_is_rerun():
    """Metadata landed, then the media download (or its upload) hit the outage."""
    ids = ["v0", "v1"]
    net = _Network(up_after_probes=2)
    seen = {}

    def fake_dl(video_id=None, **kwargs):
        seen[video_id] = seen.get(video_id, 0) + 1
        row = _metadata_row(video_id)
        if video_id == "v0" and seen[video_id] == 1:
            net.up = False
            row.attrs["media_error_type"] = "unknown"  # a failed bucket upload
        return row

    results, perm, trans, _, _ = _run_batch(ids, fake_dl, net)

    assert seen["v0"] == 2, "the item must be re-run once the network is back"
    assert results.attrs.get("media_retry_ids") == []
    assert trans == [] and perm == []
    print("PASS: a media-leg failure during an outage is re-run")




def test_ordinary_failures_online_are_untouched():
    """Online, a failure stands: the storm guard still trips as before."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD * 2)]
    net = _Network()

    def fake_dl(video_id=None, **kwargs):
        return _failure("unknown")

    results, _, _, alerts, _ = _run_batch(ids, fake_dl, net)

    assert results.attrs.get("transient_storm_tripped") is True
    assert results.attrs.get("offline") is False
    assert len(alerts) == 1 and alerts[0]["kind"] == scrape.scraper_alerts.KIND_TRANSIENT_STORM
    print("PASS: ordinary failures while online are untouched")




def test_outage_past_the_wait_stops_cleanly():
    """Offline past the wait: the batch stops, everything stays queued, no alert."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD * 3)]
    net = _Network()
    lock = threading.Lock()
    calls = {"n": 0}

    def fake_dl(video_id=None, **kwargs):
        with lock:
            calls["n"] += 1
            if calls["n"] == 3:
                net.up = False
            up = net.up
        return _metadata_row(video_id) if up else _failure("network")

    held = []
    hold = patch.object(connectivity.ConnectivityGate, "hold_until_online",
                        lambda self, *a, **k: held.append(True))
    results, perm, trans, alerts, cleared = _run_batch(
        ids, fake_dl, net, max_wait=0.05, extra=(hold,))

    assert results.attrs.get("offline") is True
    assert results.attrs.get("transient_storm_tripped") is False
    assert alerts == [] and cleared == [], "an outage neither raises nor clears alerts"
    assert held == [True], "the batch must hold its results until the network returns"
    assert perm == []
    done = set(results["item_id"]) if not results.empty else set()
    assert done | set(trans) == set(ids) and not (done & set(trans))
    assert calls["n"] < len(ids) + 3, "the batch must stop fetching once it gave up"
    print("PASS: an outage past the wait stops cleanly")




def test_batch_loop_stops_on_offline_without_charging():
    """An offline batch stops the loop; nothing transient is pruned or charged."""
    ids = [f"v{i}" for i in range(4)]
    calls = {"n": 0}
    prunes, charges = [], []

    def fake_threads(interesting_videos=None, **kwargs):
        calls["n"] += 1
        empty = pd.DataFrame()
        empty.attrs["offline"] = True
        return empty, [], list(interesting_videos)

    with patch.object(scrape, "download_video_threads", side_effect=fake_threads), \
         patch.object(scrape.scrape_queues, "prune_scrape_queue",
                      side_effect=lambda p, i: prunes.append(set(i)) or (len(i), 0)), \
         patch.object(scrape.scrape_queues, "charge_zero_progress",
                      side_effect=lambda p, i: charges.append(list(i)) or []):
        _, _, trans = scrape.scraper_loop_from_list(
            video_list=ids, batch_size=2, platform="youtube")

    assert calls["n"] == 1, "the loop must stop after the offline batch"
    assert prunes == [] and charges == [], (prunes, charges)
    assert set(trans) == set(ids[:2])
    print("PASS: batch loop stops on offline without charging")




if __name__ == "__main__":
    test_gate_recovers_after_outage()
    test_gate_gives_up_past_max_wait()
    test_gate_releases_on_stop_event_without_giving_up()
    test_outage_mid_batch_is_waited_out()
    test_media_leg_failure_during_outage_is_rerun()
    test_ordinary_failures_online_are_untouched()
    test_outage_past_the_wait_stops_cleanly()
    test_batch_loop_stops_on_offline_without_charging()
    print("All offline-gate tests passed.")
