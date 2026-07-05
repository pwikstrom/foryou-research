import http.client
import json
import os
import shutil
import sys
import threading
import time
import zipfile
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




def record_dropped_columns(
    stage: str,
    dropped: Iterable[str],
    reason: str,
    *,
    reporter=None,
    allow_list: set[str] | None = None,
    guardrail: str = "off",
    verbose: bool = False,
) -> dict:
    """Record and surface columns dropped at one pipeline stage.

    Makes the otherwise-silent column drops in recoding/scraping observable. A
    structured payload is always routed to the task reporter (so the drops show
    up in ``process_stats`` / the UI regardless of ``verbose``); the full column
    list is printed to stdout only under ``verbose`` to avoid per-batch noise
    from routine, intentional drops. Genuinely *unexpected* unknown columns (the
    data-loss-risk case) can be escalated via ``guardrail``.

    Args:
        stage: A short stage label (e.g. ``"recode_prefilter"`` /
            ``"scrape_whitelist"``); used as the payload key so multiple stages
            coexist in one ``process_stats`` entry without clobbering.
        dropped: The column names dropped at this stage.
        reason: Why they were dropped — ``"unknown"`` (not in the variable
            schema; potential data loss), ``"skip"`` (intentional ``role=skip``),
            or ``"whitelist"`` (the scrape pre-recode whitelist).
        reporter: An optional object exposing ``emit_data(dict)`` and
            ``log(str)`` (a ``TaskStatusReporter``); duck-typed to avoid an
            import cycle.
        allow_list: Column names dropped deliberately that must not count as
            unexpected (only consulted when ``reason == "unknown"``).
        guardrail: Action on unexpected unknown columns — ``"raise"`` raises
            ``ValueError``, ``"warn"`` emits a prominent warning, ``"off"`` does
            nothing beyond the structured/verbose record.
        verbose: When True, also print the full dropped-column list to stdout.

    Returns:
        A summary dict ``{stage, reason, count, columns, unexpected}``.

    Raises:
        ValueError: when ``guardrail == "raise"`` and an unexpected unknown
            column is present.
    """
    columns = sorted(str(c) for c in dropped)
    allowed = allow_list or set()
    unexpected = [c for c in columns if c not in allowed] if reason == "unknown" else []
    summary = {
        "stage": stage,
        "reason": reason,
        "count": len(columns),
        "columns": columns,
        "unexpected": unexpected,
    }

    if not columns:
        return summary

    if reporter is not None:
        try:
            reporter.emit_data({"dropped_columns": {stage: summary}})
        except Exception:
            pass

    if verbose:
        message = f"[{stage}] dropped {len(columns)} column(s) ({reason}): {', '.join(columns)}"
        print(message)
        if reporter is not None:
            try:
                reporter.log(message)
            except Exception:
                pass

    if unexpected and guardrail in ("warn", "raise"):
        warning = (
            f"WARNING: {len(unexpected)} unexpected column(s) dropped at '{stage}' "
            f"(not in the variable schema and not allow-listed): {', '.join(unexpected)}"
        )
        if guardrail == "raise":
            raise ValueError(warning)
        print(warning)
        if reporter is not None:
            try:
                reporter.log(warning)
            except Exception:
                pass

    return summary




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




def best_similarity_match(reference: str, candidates: Iterable[str]) -> tuple[str | None, float]:
    """Return the most similar candidate to ``reference`` and its similarity ratio.

    Args:
        reference: The string to match against.
        candidates: Candidate strings to rank.

    Returns:
        A ``(candidate, ratio)`` tuple where ratio is the difflib.SequenceMatcher
        score (0.0–1.0). Returns ``(None, 0.0)`` when ``candidates`` is empty.
    """

    best_candidate = None
    best_ratio = 0.0
    for candidate in candidates:
        ratio = SequenceMatcher(None, reference, candidate).ratio()
        if best_candidate is None or ratio > best_ratio:
            best_candidate = candidate
            best_ratio = ratio
    return best_candidate, best_ratio






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





def repair_mojibake(text: str) -> str:
    """Repair text that was UTF-8 bytes mis-decoded as Latin-1 ("mojibake").

    Data-donation exports (notably Meta's) frequently serialise UTF-8 bytes as
    if they were Latin-1, so ``é`` arrives as ``Ã©`` and an em dash as ``â``.
    Re-encoding to Latin-1 and decoding as UTF-8 reverses that. The round-trip
    is attempted defensively: any string that is not mangled this way (or cannot
    be cleanly re-decoded) is returned unchanged.

    Args:
        text: The possibly-mangled string.

    Returns:
        The repaired string, or the original when no clean repair is possible.
    """
    if not isinstance(text, str) or not text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text





def read_zip_members(local_path: str, suffixes: list[str]) -> dict[str, bytes | None]:
    """Read several zip members by name suffix in a single archive pass.

    Matching on a path suffix rather than a full name makes lookups robust to the
    archive's top-level folder varying between exports (e.g. ``Takeout/…`` or
    ``your_instagram_activity/…``). Directory entries are ignored; the first
    match per suffix wins. The archive is opened and its directory scanned once
    regardless of how many suffixes are requested.

    Args:
        local_path: Path to a zip archive on the local filesystem.
        suffixes: Member-name suffixes to match (e.g.
            ``["history/watch-history.html"]``).

    Returns:
        ``{suffix: bytes | None}`` — ``None`` for suffixes with no match.

    Raises:
        zipfile.BadZipFile: if the file is not a readable zip archive.
        OSError: if the file cannot be opened.
    """
    out: dict[str, bytes | None] = {s: None for s in suffixes}
    remaining = set(suffixes)
    with zipfile.ZipFile(local_path) as zf:
        for name in zf.namelist():
            if not remaining:
                break
            if name.endswith("/"):
                continue
            for suffix in list(remaining):
                if name.endswith(suffix):
                    out[suffix] = zf.read(name)
                    remaining.discard(suffix)
    return out





def read_zip_member(local_path: str, suffix: str) -> bytes | None:
    """Return the bytes of the first zip member whose name ends with ``suffix``.

    Convenience wrapper around :func:`read_zip_members` that swallows archive
    errors — use the plural form when a broken archive should raise instead.

    Args:
        local_path: Path to a zip archive on the local filesystem.
        suffix: The member-name suffix to match.

    Returns:
        The member's raw bytes, or ``None`` when the archive has no match or is
        not a readable zip.
    """
    try:
        return read_zip_members(local_path, [suffix])[suffix]
    except (zipfile.BadZipFile, FileNotFoundError, OSError):
        return None
