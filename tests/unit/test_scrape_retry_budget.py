"""The cross-run retry budget for transiently-failing scrape queue items.

An item whose failures are classified transient used to be retried forever;
when such items were the only thing left in a queue, no batch ever pruned
anything, the queue never drained, and the enrichment supervisor's no-drain
guard parked every armed plan (observed live 2026-09-01: five TikTok items
stuck on yt-dlp's "No video formats found"). The budget gives every item
MAX_ZERO_PROGRESS_STRIKES zero-progress runs; after that it is pruned and
recorded in the failed-scrapes ledger like any other permanent failure.

Covers the sidecar helpers in fyp.scrape.scrape_queues, the cloud batch path
in web_interface.run_queue_scraper, and the "No video formats found"
classification fix in fyp.scrape.tiktok_dl.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from fyp.scrape import scrape_queues


def _fake_data_io(tmp: str):
    """A minimal data_io stand-in writing JSON files under tmp."""

    class FakeIO:
        @staticmethod
        def _p(filename):
            return os.path.join(tmp, filename)

        @staticmethod
        def exists(storage_location="cache", filename="", verbose=False):
            return os.path.exists(FakeIO._p(filename))

        @staticmethod
        def load_json(storage_location="cache", filename="", verbose=False):
            with open(FakeIO._p(filename)) as f:
                return json.load(f)

        @staticmethod
        def save_json(data=None, storage_location="cache", filename="", verbose=False):
            with open(FakeIO._p(filename), "w") as f:
                json.dump(data, f)

        @staticmethod
        def remove(storage_location="cache", filename="", verbose=False):
            os.remove(FakeIO._p(filename))

        @staticmethod
        def update_json(storage_location="cache", filename="", mutate=None,
                        default=None, max_retries=6, verbose=False):
            path = FakeIO._p(filename)
            current = json.loads(json.dumps(default)) if default is not None else None
            if os.path.exists(path):
                with open(path) as f:
                    current = json.load(f)
            new_value = mutate(current)
            if new_value is None:
                return None
            with open(path, "w") as f:
                json.dump(new_value, f)
            return new_value

    return FakeIO


# --------------------------------------------------------------------------- #
# Sidecar helpers
# --------------------------------------------------------------------------- #

def test_charge_zero_progress_accumulates_then_exhausts():
    """Strike 1 keeps items queued; strike MAX returns them as exhausted."""
    with tempfile.TemporaryDirectory() as tmp:
        io = _fake_data_io(tmp)
        with patch.object(scrape_queues, "_data_io", return_value=io):
            first = scrape_queues.charge_zero_progress("tiktok", ["a", "b"])
            assert first == [], f"one strike must not exhaust anything: {first}"
            sidecar = io.load_json(filename=scrape_queues.strikes_filename("tiktok"))
            assert sidecar == {"a": 1, "b": 1}

            second = scrape_queues.charge_zero_progress("tiktok", ["a", "b", "c"])
            assert set(second) == {"a", "b"}, f"a and b exhausted their budget: {second}"
            sidecar = io.load_json(filename=scrape_queues.strikes_filename("tiktok"))
            assert sidecar == {"c": 1}, "exhausted ids leave the sidecar; c keeps counting"
    print("PASS: strikes accumulate and exhaust at the budget")




def test_clear_zero_progress_resets_the_sidecar():
    """A progressing batch wipes all strikes (and tolerates a missing file)."""
    with tempfile.TemporaryDirectory() as tmp:
        io = _fake_data_io(tmp)
        with patch.object(scrape_queues, "_data_io", return_value=io):
            scrape_queues.clear_zero_progress("tiktok")  # no file: no-op
            scrape_queues.charge_zero_progress("tiktok", ["a"])
            scrape_queues.clear_zero_progress("tiktok")
            assert not io.exists(filename=scrape_queues.strikes_filename("tiktok"))
            assert scrape_queues.charge_zero_progress("tiktok", ["a"]) == [], \
                "after a clear the count restarts from zero"
    print("PASS: clear_zero_progress resets the sidecar")




# --------------------------------------------------------------------------- #
# Cloud batch path (what the enrichment supervisor drives)
# --------------------------------------------------------------------------- #

class _Reporter:
    def __init__(self):
        self.lines = []
        self.data = []

    def log(self, msg):
        self.lines.append(str(msg))

    def update_progress(self, pct, msg=""):
        pass

    def emit_data(self, payload):
        self.data.append(payload)

    def check_cancelled(self):
        return False


class _HealthyScraper:
    @staticmethod
    def health_check():
        return None


def _all_transient_threads(**kwargs):
    """Every item fails transiently; no storm, no breaker, no memory stop."""
    empty = pd.DataFrame()
    for k in ("circuit_breaker_tripped", "permanent_storm_tripped",
              "transient_storm_tripped", "memory_stop"):
        empty.attrs[k] = False
    return empty, [], list(kwargs["interesting_videos"])


def _run_cloud_batch(io, threads_fn, recorded):
    """Run one run_queue_scraper batch against the fake queue store."""
    import fyp.scrape as fyp_scrape
    from web_interface.run_queue_scraper import run_queue_scraper

    reporter = _Reporter()
    with patch.object(scrape_queues, "_data_io", return_value=io), \
         patch.object(scrape_queues, "migrate_legacy_queue", lambda platform: None), \
         patch.object(fyp_scrape, "download_video_threads", threads_fn), \
         patch.object(fyp_scrape, "record_failed_scrapes",
                      lambda items, **kw: recorded.append(items)), \
         patch("fyp.platform_scraper.get_scraper",
               lambda platform: _HealthyScraper()):
        result = run_queue_scraper(reporter, {"platform": "tiktok"})
    return result, reporter


def test_cloud_zero_progress_runs_burn_the_stuck_tail():
    """Two zero-progress runs drop the stuck items and drain the queue."""
    stuck = ["v1", "v2", "v3"]
    with tempfile.TemporaryDirectory() as tmp:
        io = _fake_data_io(tmp)
        io.save_json(data=stuck, filename=scrape_queues.queue_filename("tiktok"))
        recorded = []

        result, _ = _run_cloud_batch(io, _all_transient_threads, recorded)
        assert result is None, "a zero-progress batch must not chain"
        assert recorded == [], "one strike must not record failures"
        assert io.load_json(filename=scrape_queues.queue_filename("tiktok")) == stuck

        result, reporter = _run_cloud_batch(io, _all_transient_threads, recorded)
        assert result is None
        assert len(recorded) == 1, "the second run gives up on the stuck tail"
        assert {r["item_id"] for r in recorded[0]} == set(stuck)
        assert all(r["category"] == "permanent:retry_exhausted" for r in recorded[0])
        assert io.load_json(filename=scrape_queues.queue_filename("tiktok")) == [], \
            "the queue must drain so the supervisor's no-drain guard never parks"
        assert io.load_json(filename=scrape_queues.strikes_filename("tiktok")) == {}, \
            "exhausted ids leave the sidecar"
        assert any("Gave up on 3 item(s)" in ln for ln in reporter.lines), reporter.lines
    print("PASS: cloud path burns the stuck tail on the second run")




def test_cloud_progressing_batch_clears_strikes():
    """A batch that prunes something wipes earlier strikes."""
    with tempfile.TemporaryDirectory() as tmp:
        io = _fake_data_io(tmp)
        io.save_json(data=["good", "flaky"], filename=scrape_queues.queue_filename("tiktok"))
        recorded = []

        def mixed(**kwargs):
            empty = pd.DataFrame({"item_id": ["good"]})
            for k in ("circuit_breaker_tripped", "permanent_storm_tripped",
                      "transient_storm_tripped", "memory_stop"):
                empty.attrs[k] = False
            return empty, [], ["flaky"]

        io.save_json(data={"flaky": 1}, filename=scrape_queues.strikes_filename("tiktok"))
        _run_cloud_batch(io, mixed, recorded)
        assert not io.exists(filename=scrape_queues.strikes_filename("tiktok")), \
            "queue progress must reset the strike counts"
        assert recorded == []
        assert io.load_json(filename=scrape_queues.queue_filename("tiktok")) == ["flaky"]
    print("PASS: a progressing batch clears strikes")




def test_cloud_storm_abort_does_not_charge():
    """A transient-storm abort implicates the scraper — no strikes charged."""
    with tempfile.TemporaryDirectory() as tmp:
        io = _fake_data_io(tmp)
        io.save_json(data=["v1", "v2"], filename=scrape_queues.queue_filename("tiktok"))
        recorded = []

        def storm(**kwargs):
            empty = pd.DataFrame()
            for k in ("circuit_breaker_tripped", "permanent_storm_tripped", "memory_stop"):
                empty.attrs[k] = False
            empty.attrs["transient_storm_tripped"] = True
            empty.attrs["transient_storm_category"] = "transient:unknown"
            return empty, [], list(kwargs["interesting_videos"])

        for _ in range(3):
            _run_cloud_batch(io, storm, recorded)
        assert not io.exists(filename=scrape_queues.strikes_filename("tiktok")), \
            "storm-aborted runs must not burn retry budget"
        assert recorded == []
        assert io.load_json(filename=scrape_queues.queue_filename("tiktok")) == ["v1", "v2"]
    print("PASS: storm aborts never charge strikes")




# --------------------------------------------------------------------------- #
# Classifier fix
# --------------------------------------------------------------------------- #

def test_no_video_formats_found_is_permanent():
    """yt-dlp's 'No video formats found!' must classify as a permanent failure."""
    from fyp.scrape.tiktok_dl import _PERMANENT, _classify_error

    category, _ = _classify_error(Exception(
        "ERROR: [TikTok] 7649168662586346774: No video formats found!; "
        "please report this issue on https://github.com/yt-dlp/yt-dlp/issues"))
    assert category == "extraction", category
    assert category in _PERMANENT, "the category must sit in the permanent bucket"
    print("PASS: 'No video formats found' classifies as permanent")




if __name__ == "__main__":
    test_charge_zero_progress_accumulates_then_exhausts()
    test_clear_zero_progress_resets_the_sidecar()
    test_cloud_zero_progress_runs_burn_the_stuck_tail()
    test_cloud_progressing_batch_clears_strikes()
    test_cloud_storm_abort_does_not_charge()
    test_no_video_formats_found_is_permanent()
    print("All scrape retry-budget tests passed.")
