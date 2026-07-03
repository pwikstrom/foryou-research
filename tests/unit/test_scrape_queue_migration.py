"""Verify the per-platform scrape queue helpers in fyp.scrape_queues.

Covers the one-time legacy to_scrape.json migration (rename, both-files
union, idempotency), order-preserving dedup, append, and prune. Runs the
cache location against a temporary directory so no real queue is touched.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import json
import os

import fyp.scrape_queues as scrape_queues


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

    return FakeIO






def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        fake = _fake_data_io(tmp)
        with patch.object(scrape_queues, "_data_io", return_value=fake):
            assert scrape_queues.default_platform() == "tiktok"
            assert scrape_queues.queue_filename("tiktok") == "to_scrape_tiktok.json"

            # --- Legacy migration: rename ---
            fake.save_json(data=["a", "b", "a"], filename="to_scrape.json")
            q = scrape_queues.load_scrape_queue("tiktok")
            assert q == ["a", "b"], f"order-preserving dedup expected, got {q}"
            assert not fake.exists(filename="to_scrape.json"), "legacy file should be gone"
            assert fake.exists(filename="to_scrape_tiktok.json")

            # --- Idempotent: second load unchanged ---
            assert scrape_queues.load_scrape_queue("tiktok") == ["a", "b"]

            # --- Both-files union (web/task-runner race) ---
            fake.save_json(data=["c", "b"], filename="to_scrape.json")
            q = scrape_queues.load_scrape_queue("tiktok")
            assert q == ["a", "b", "c"], f"union expected, got {q}"
            assert not fake.exists(filename="to_scrape.json")

            # --- Append (dedup, returns new length) ---
            n = scrape_queues.append_to_scrape_queue("tiktok", ["d", "a"])
            assert n == 4, f"expected 4 after append, got {n}"

            # --- Prune (removes finished, keeps transient) ---
            pruned, remaining = scrape_queues.prune_scrape_queue("tiktok", {"a", "c", "zz"})
            assert (pruned, remaining) == (2, 2), f"got {(pruned, remaining)}"
            assert scrape_queues.load_scrape_queue("tiktok") == ["b", "d"]

            # --- Non-default platform: no legacy migration, own file ---
            fake.save_json(data=["x"], filename="to_scrape.json")
            assert scrape_queues.load_scrape_queue("instagram") == []
            assert fake.exists(filename="to_scrape.json"), "non-default platform must not migrate"
            scrape_queues.append_to_scrape_queue("instagram", ["ig1"])
            assert scrape_queues.load_scrape_queue("instagram") == ["ig1"]
            assert scrape_queues.load_scrape_queue("tiktok") == ["b", "d", "x"]

            # --- queue_lengths covers registered platforms ---
            lengths = scrape_queues.queue_lengths()
            assert lengths.get("tiktok") == 3, f"got {lengths}"

            # --- remove_scrape_queue deletes the file ---
            scrape_queues.remove_scrape_queue("tiktok")
            assert scrape_queues.load_scrape_queue("tiktok") == []

    print("OK — scrape queue migration/append/prune behaviour verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
