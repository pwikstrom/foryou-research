import http.client
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterable
from difflib import SequenceMatcher
from urllib.parse import unquote

import pandas as pd
import pyarrow as pa


# check internet connectivity
def online_ok(url="www.qut.edu.au",
                        timeout=3):
    connection = http.client.HTTPConnection(url,
                                        timeout=timeout)
    try:
        # only header requested for fast operation
        connection.request("HEAD", "/")
        connection.close()  # connection closed
        return True
    except Exception as exep:
        print(exep)
        return False








def chunk_list(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]




def is_list_like_col(s):
    # Check for the Arrow List type (your original code)
    is_arrow_list = (
        isinstance(s.dtype, pd.ArrowDtype) and 
        pa.types.is_list(s.dtype.pyarrow_dtype)
    )
    # Check for the "good old" object type
    is_object = s.dtype == "object"
    
    return is_arrow_list or is_object



def sort_by_similarity(reference: str, candidates: Iterable[str]) -> list[str]:
    """
    Return the candidates sorted from most to least similar to the reference string.
    Similarity is measured via difflib.SequenceMatcher ratio (0.0–1.0).
    """

    return sorted(
        candidates,
        key=lambda candidate: SequenceMatcher(None, reference, candidate).ratio(),
        reverse=True,
    )






def pretty_str_seconds(proc_time_seconds: float) -> str:
    minutes, seconds = divmod(proc_time_seconds, 60)
    out = ""
    if minutes > 0:
        out += f"{minutes:.0f}m"
    if seconds > 0:
        if minutes > 0:
            out += " and "
        out += f"{seconds:.0f}s"
    return out




def extract_and_join_subkeys(data, sub_keys: list):
    """
    Process a list of dictionaries or a single value, extracting and joining specified sub-keys.

    Args:
    data (list or any): The input data to process. If it's a list, each item is expected to be a dictionary.
    sub_keys (list): A list of keys to extract from each dictionary in the list.

    Returns:
    str or numpy.nan: A string of concatenated values from the specified sub-keys, 
                      or numpy.nan if the input is not a list or is empty.

    Description:
    This function extracts and concatenates values from specific keys in a list of dictionaries.
    If the input is not a list or is empty, it returns numpy.nan.
    For each dictionary in the list, it extracts the values of the specified sub-keys,
    joins them with "__", and then joins all these combined values with " | ".

    Example:
    >>> data = [
    ...     {"id": 1, "name": "John", "age": 30},
    ...     {"id": 2, "name": "Jane", "age": 25},
    ...     {"id": 3, "name": "Bob", "age": 35}
    ... ]
    >>> sub_keys = ["name", "age"]
    >>> result = extract_and_join_subkeys(data, sub_keys)
    >>> print(result)
    'John__30 | Jane__25 | Bob__35'
    """
    joined_values = []
    if isinstance(data, list) and len(sub_keys) > 0:
        for item in data:
            if isinstance(item, dict):
                subkey_values = []
                for sk in sub_keys:
                    if sk in item:
                        subkey_values.append(str(item[sk]))
                joined_values.append("__".join(subkey_values))
        return " | ".join(joined_values)
    else:
        return pd.NA




def clean_url(the_url: str) -> dict:
    outout = {}
    if "?" not in the_url or "&" not in the_url:
        return outout
    for u in the_url.split("?")[1].split("&"):
        v = u.split("=")
        v[1] = unquote(v[1]).replace(",","|")
        try:
            v[1] = int(v[1])
        except:
            pass
        outout.update({"source_url."+v[0]:v[1]})
    return outout



def flatten_list(nested_list):
    """
    Flattens a nested list into a single list.
    """
    return [item for sublist in nested_list for item in sublist]



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
        if h: return f"{h}h{m}m{s}s"
        if m: return f"{m}m{s}s"
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
                line = line[:max(0, term_width - 1)]

            # single-line update (reporter vs web interface vs terminal)
            if reporter is not None:
                 overall_done = cumulative_done + done if cumulative_total > 0 else done
                 overall_total = cumulative_total if cumulative_total > 0 else total
                 overall_eta = (overall_total - overall_done) / throughput if throughput > 0 else 0
                 pct = int((overall_done / overall_total) * 100) if overall_total > 0 else 0
                 batch_str = f"Batch {batch_label}: " if batch_label else ""
                 if n_good is not None and done > 0:
                     fail_count = done - n_good
                     counts_str = f"{n_good} OK, {fail_count} fail, {pending} pending"
                 else:
                     counts_str = f"{done}/{total} done, {pending} pending"
                 reporter.update_progress(
                     pct,
                     f"{batch_str}{counts_str} ({throughput:.1f}/s, ETA {_fmt_secs(overall_eta)})",
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
                     "eta": overall_eta
                 }
                 if batch_label:
                     progress_data["batch"] = batch_label
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





# Engagement activity tokens carried inside the folded `extra_data` column.
# A play row's `extra_data` is a comma-separated list of "<atype>[:context]"
# tokens (e.g. "fave", "comment:hello", "fave,follow:somebody") recorded
# when other activities share the same session run as the leading play.
ENGAGEMENT_TYPES = ('fave', 'share', 'comment', 'follow', 'save')
ACTIVITY_TYPE_MAP = {
    'fave': 'fave',
    'share': 'share',
    'comment': 'comment',
    'follow': 'follow',
    'following': 'follow',
    'save': 'save',
}





def parse_extra_data_tokens(s) -> set:
    """Parse a folded extra_data cell into its normalised engagement-type tokens."""
    if not isinstance(s, str) or not s:
        return set()
    out = set()
    for part in s.split(','):
        atype = part.split(':', 1)[0].strip().lower()
        mapped = ACTIVITY_TYPE_MAP.get(atype)
        if mapped:
            out.add(mapped)
    return out
