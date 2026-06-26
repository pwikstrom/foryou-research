"""Offline check of the unified start_monitor progress message format.

Drives start_monitor with synthetic futures and a capturing reporter, then
asserts the rendered line shows job-wide totals in the new format:

    Batch n (dd%)/max · X OK · Y fail · P processing · Q pending · ETA ...
"""

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from fyp.utils import start_monitor


class CapturingReporter:
    """Minimal reporter that records every progress message."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    def update_progress(self, percent, message, **kwargs) -> None:
        self.messages.append((percent, message))


def _task(value: str) -> str:
    time.sleep(0.4)
    return value


def main() -> None:
    reporter = CapturingReporter()

    # Current batch: 10 items, 7 "ok" + 3 "fail".
    outcomes = ["ok"] * 7 + ["fail"] * 3

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = []
        submit_times = {}
        for v in outcomes:
            fut = ex.submit(_task, v)
            futures.append(fut)
            submit_times[fut] = time.time()

        # Batch 2 of 5; 1 prior batch (10 items) already finalised, of which
        # 6 OK / 4 fail carried forward.
        t = start_monitor(
            futures, submit_times, interval=0.15, label="test", bar_width=20,
            result_checker=lambda f: f.result() == "ok",
            batch_label="2/5",
            cumulative_done=10,
            cumulative_total=50,
            cumulative_ok=6,
            cumulative_fail=4,
            reporter=reporter,
        )
        t.join(timeout=10)

    assert reporter.messages, "reporter captured no messages"
    final_pct, final_msg = reporter.messages[-1]
    print("Captured messages:")
    for pct, msg in reporter.messages:
        print(f"  ({pct:3d}%) {msg}")
    print(f"\nFinal: ({final_pct}%) {final_msg}")

    # Final tick: all 10 done (7 ok / 3 fail), 0 still processing.
    # Totals: OK = 6+7 = 13, fail = 4+3 = 7, pending = 50-20-0 = 30, pct = 20/50 = 40%.
    checks = {
        "batch label + internal pct": "Batch 2 (100%)/5" in final_msg,
        "total OK (13)": "13 OK" in final_msg,
        "total fail (7)": "7 fail" in final_msg,
        "processing (0)": "0 processing" in final_msg,
        "total pending (30)": "30 pending" in final_msg,
        "has ETA": "ETA" in final_msg,
        "overall percent (40)": final_pct == 40,
    }

    # At least one mid-flight tick should have shown items being processed.
    saw_processing = any(
        m.group(1) != "0"
        for _, msg in reporter.messages
        if (m := re.search(r"(\d+) processing", msg))
    )
    checks["processing > 0 at some tick"] = saw_processing

    print("\nChecks:")
    failed = []
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failed.append(label)

    if failed:
        raise SystemExit(f"\nFAILED: {failed}")
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
