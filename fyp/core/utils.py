"""Small shared utilities used across the Hub.

Connectivity probing, list chunking, dropped-column bookkeeping, fuzzy name
matching, URL cleaning, mojibake repair and zip-member reading. The activity
vocabulary (:mod:`fyp.core.activity_vocabulary`) and the progress monitor
(:mod:`fyp.core.progress_monitor`) are re-exported here for old callers.
"""

import http.client
import zipfile
from collections.abc import Iterable
from difflib import SequenceMatcher
from urllib.parse import unquote

# The activity vocabulary and the progress monitor live in their own modules;
# re-exported for callers of this module's old surface.
from fyp.core.activity_vocabulary import (  # noqa: F401
    ACTIVITY_TYPE_MAP,
    ENGAGEMENT_LABELS,
    ENGAGEMENT_TYPES,
    KNOWN_ACTIVITY_TYPES,
    RECEIVED_ACTIVITY_TYPES,
    SHARE_COUNT_SEP,
    STANDALONE_ACTIVITY_TYPES,
    STANDALONE_ENGAGEMENT_LABELS,
    VIDEO_VIEW_TYPES,
    VIEWING_ACTIVITY_TYPES,
    engagement_label,
    parse_extra_data_tokens,
    share_method_base,
    share_method_with_count,
)
from fyp.core.logging_setup import get_logger
from fyp.core.progress_monitor import (  # noqa: F401
    start_monitor,
)

logger = get_logger(__name__)


# Well-known HEAD-tolerant host used for the default connectivity probe.
DEFAULT_PROBE_HOST = "connectivitycheck.gstatic.com"


# check internet connectivity
def online_ok(url=DEFAULT_PROBE_HOST, timeout=3):
    connection = http.client.HTTPConnection(url, timeout=timeout)
    try:
        # only header requested for fast operation
        connection.request("HEAD", "/")
        connection.close()  # connection closed
        return True
    except Exception as exep:
        logger.warning(exep)
        return False


def chunk_list(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


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
        logger.info(message)
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
        logger.warning(warning)
        if reporter is not None:
            try:
                reporter.log(warning)
            except Exception:
                pass

    return summary


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


def clean_url(the_url: str) -> dict:
    outout = {}
    if "?" not in the_url or "&" not in the_url:
        return outout
    for u in the_url.split("?")[1].split("&"):
        v = u.split("=")
        v[1] = unquote(v[1]).replace(",", "|")
        try:
            v[1] = int(v[1])
        except Exception:
            pass
        outout.update({"source_url." + v[0]: v[1]})
    return outout


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
