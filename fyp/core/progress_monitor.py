"""A live progress monitor for a batch of concurrent futures.

``start_monitor`` starts a daemon thread that prints a progress bar for the
futures (done, success rate, elapsed and per-item timings) until they all
finish, also reporting it to the task status when given a reporter; it
returns the thread.
"""

import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable


def start_monitor(
    futures,
    submit_times,
    interval=5,
    label="monitor",
    bar_width=30,
    result_checker: Callable | None = None,
    batch_label: str | None = None,
    cumulative_done: int = 0,
    cumulative_total: int = 0,
    cumulative_ok: int = 0,
    cumulative_fail: int = 0,
    reporter=None,
):
    """
    Monitor progress of concurrent futures with a live progress bar.

    Args:
        futures: list of Future objects to monitor
        submit_times: dict mapping Future -> time.time() at submission
        interval: seconds between status updates
        label: label for the progress bar
        bar_width: width of the progress bar in characters
        result_checker: optional callable(future) -> bool. If provided,
            called on each completed future to compute a success rate.
            E.g. for scraping: lambda f: isinstance(f.result()[1], pd.DataFrame)
        reporter: optional TaskStatusReporter for GCS-based progress (Cloud Tasks mode).
    """

    def _fmt_secs(s):
        if s is None:
            return "n/a"
        s = int(s)
        h, r = divmod(s, 3600)
        m, s = divmod(r, 60)
        if h:
            return f"{h}h{m}m{s}s"
        if m:
            return f"{m}m{s}s"
        return f"{s}s"

    def _bar(done, total, width=30, fill="#", empty="-"):
        if total <= 0:
            return "[" + empty * width + "] 0%"
        frac = max(0.0, min(1.0, done / total))
        n_fill = int(round(frac * width))
        n_empty = max(0, width - n_fill)
        pct = int(round(frac * 100))
        return f"[{fill * n_fill}{empty * n_empty}] {pct:3d}%"

    def _run():
        start = min(submit_times.values()) if submit_times else time.time()
        seen_done = set()
        durations = []

        total = len(futures)
        while True:
            now = time.time()
            done_futs = [f for f in futures if f.done()]

            # Optional success-rate tracking
            n_good = None
            if result_checker is not None:
                n_good = sum(1 for fut in done_futs if result_checker(fut))

            running = sum(f.running() for f in futures)
            done = len(done_futs)
            pending = total - done - running

            # record turnaround times (submission to completion)
            for f in done_futs:
                if f not in seen_done:
                    seen_done.add(f)
                    durations.append(now - submit_times.get(f, start))

            elapsed = now - start
            throughput = (done / elapsed) if elapsed > 0 else 0.0
            remaining = total - done
            eta = (remaining / throughput) if throughput > 0 else None

            bar = _bar(done, total, width=bar_width)

            # Build status line
            success_part = ""
            if n_good is not None and done > 0:
                success_rate = n_good / done
                success_part = f"success {success_rate:.0%}  "

            line = (
                f"[{label}] {bar}  "
                f"done {done:,}/{total:,}  {success_part}pending {pending:,}  "
                f"rate {throughput:.2f}/s  ETA {_fmt_secs(eta)}     "
            )

            # trim to terminal width if needed
            try:
                term_width = shutil.get_terminal_size(fallback=(140, 20)).columns
            except Exception:
                term_width = 140
            if len(line) > term_width:
                line = line[: max(0, term_width - 1)]

            # single-line update (reporter vs web interface vs terminal)
            if reporter is not None:
                overall_done = cumulative_done + done if cumulative_total > 0 else done
                overall_total = cumulative_total if cumulative_total > 0 else total
                overall_eta = (overall_total - overall_done) / throughput if throughput > 0 else 0
                pct = int((overall_done / overall_total) * 100) if overall_total > 0 else 0
                # Job-wide totals: cumulative carries OK/fail finalised in prior
                # batches/chains; the current batch's live counts are added on top.
                # Note for the scraper: mid-batch a not-yet-succeeded item counts
                # as fail here (done - n_good); transient failures that will be
                # retried only get reconciled back into pending at the batch
                # boundary, where cumulative_fail carries permanent fails only.
                batch_ok = n_good if n_good is not None else 0
                batch_fail = (done - n_good) if n_good is not None else 0
                total_ok = cumulative_ok + batch_ok
                total_fail = cumulative_fail + batch_fail
                total_pending = max(0, overall_total - overall_done - running)
                batch_pct = int((done / total) * 100) if total > 0 else 0
                # batch_label is "n/max"; render "Batch n (dd%)/max".
                if batch_label and "/" in batch_label:
                    b_n, b_max = batch_label.split("/", 1)
                    batch_str = f"Batch {b_n} ({batch_pct}%)/{b_max} · "
                elif batch_label:
                    batch_str = f"Batch {batch_label} ({batch_pct}%) · "
                else:
                    batch_str = ""
                reporter.update_progress(
                    pct,
                    f"{batch_str}{total_ok} OK · {total_fail} fail · "
                    f"{running} processing · {total_pending} pending · "
                    f"ETA {_fmt_secs(overall_eta)}",
                )
            elif "WEB_INTERFACE" in os.environ:
                overall_done = cumulative_done + done if cumulative_total > 0 else done
                overall_total = cumulative_total if cumulative_total > 0 else total
                overall_remaining = overall_total - overall_done
                overall_eta = (overall_remaining / throughput) if throughput > 0 else 0

                progress_data = {
                    "done": overall_done,
                    "total": overall_total,
                    "batch_done": done,
                    "batch_total": total,
                    "rate": throughput,
                    "eta": overall_eta,
                }
                if batch_label:
                    progress_data["batch"] = batch_label
                # STDOUT PROTOCOL — MUST stay print(). process_manager.enqueue_output()
                # parses subprocess stdout for the ::PROGRESS:: marker; never convert to logging.
                print(f"::PROGRESS::{json.dumps(progress_data)}", flush=True)
            else:
                sys.stdout.write("\r" + line)
                sys.stdout.flush()

            if done == total:
                break
            time.sleep(interval)

        # finish with a newline so the next print does not overwrite the last status
        sys.stdout.write("\n")
        sys.stdout.flush()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t
