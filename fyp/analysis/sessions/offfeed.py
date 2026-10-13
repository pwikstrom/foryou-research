"""Off-feed signals: plays that were probably not served by the For You feed.

A watch history lists every video the donor played, whether the feed served
it or the donor opened it from a creator's profile, a search result or a
shared link, and it does not say which. Three signals mark a play as likely
off-feed:

* **search / follow** — a search or a follow by the donor in the
  ``window_s`` seconds before the play (inclusive at both ends);
* **same creator** — the play is by the same creator as the previous play in
  the session (TikTok states the For You feed "generally won't show two
  videos in a row" from one creator);
* **posting-order run** — the play belongs to a run of at least ``min_run``
  consecutive plays by one creator whose posting times are strictly
  increasing or strictly decreasing, the order of a creator's profile page.

The result is a per-play bitmask (:data:`SIG_SEARCH`, :data:`SIG_FOLLOW`,
:data:`SIG_SAME_CREATOR`, :data:`SIG_POSTING_RUN`); a play is off-feed when
any bit is set. The signals annotate plays — nothing is filtered here.

Creator identity and posting time come from scraped metadata, so the second
and third signals can only fire between scraped videos, and an unknown
creator or posting time never counts as a match: the signals undercount.

Pure numpy/pandas, no storage or config access, so analysis scripts outside
the Hub can import it (see docs/decisions/0028-offfeed-signals.md).
"""

import numpy as np
import pandas as pd

# The donor's own event types for the first signal. "followed_by" (another
# account following the donor) is deliberately absent: it is not the donor's
# action and carries no session.
EVENT_TYPES = ("search", "follow")
WINDOW_S = 120
MIN_RUN = 3

SIG_SEARCH = 1
SIG_FOLLOW = 2
SIG_SAME_CREATOR = 4
SIG_POSTING_RUN = 8

SIGNAL_LABELS = {
    SIG_SEARCH: "search in the 2 minutes before",
    SIG_FOLLOW: "follow in the 2 minutes before",
    SIG_SAME_CREATOR: "same creator as the previous play",
    SIG_POSTING_RUN: "part of a creator's posting-order run",
}


def _ns(values) -> np.ndarray:
    """Datetime-like values as int64 nanoseconds (NaT → int64 min).

    Timezone-aware values are taken in UTC; naive values as they are (the
    Hub's play and event timestamps are both naive local time, so their
    differences are exact).
    """
    s = pd.to_datetime(pd.Series(values), errors="coerce", utc=True).dt.tz_convert(None)
    return s.to_numpy(dtype="datetime64[ns]").view("int64")


def _in_window(play_ns: np.ndarray, event_ns: np.ndarray, window_ns: int) -> np.ndarray:
    """True where some event lies in ``[play - window, play]``."""
    if len(event_ns) == 0 or len(play_ns) == 0:
        return np.zeros(len(play_ns), dtype=bool)
    e = np.sort(event_ns)
    lo = np.searchsorted(e, play_ns - window_ns, side="left")
    hi = np.searchsorted(e, play_ns, side="right")
    return hi > lo


def flag_plays(
    plays: pd.DataFrame,
    events: pd.DataFrame | None,
    window_s: float = WINDOW_S,
    min_run: int = MIN_RUN,
    event_types: tuple[str, ...] = EVENT_TYPES,
    narrow_follow: bool = False,
) -> np.ndarray:
    """Per-play off-feed signal bitmask.

    Args:
        plays: One row per play with ``collection_id``, ``session_id``,
            ``_ts`` (datetime), ``author`` (creator handle, null when
            unknown) and ``created`` (the video's posting time, datetime or
            null). Any row order.
        events: The donor's event rows with ``collection_id``,
            ``activity_type``, ``_ts`` and, for follows, ``extra_data`` (the
            followed handle). None or empty disables the first signal.
        window_s: Seconds before a play in which a search or follow counts.
        min_run: Minimum length of a posting-order run.
        event_types: Event types the first signal looks for.
        narrow_follow: Count a follow only when the followed handle is the
            play's own creator (a sensitivity variant; searches unchanged).

    Returns:
        An int8 array aligned to ``plays``' row order: the OR of the
        ``SIG_*`` bits that fired for each play (0 = no signal).
    """
    n = len(plays)
    out = np.zeros(n, dtype=np.int8)
    if n == 0:
        return out

    # Session order: sessions never cross a collection, and within a session
    # plays are time-ordered (stable, so equal timestamps keep input order).
    order = np.lexsort(
        (
            _ns(plays["_ts"]),
            plays["session_id"].astype("string").fillna("").to_numpy(dtype=object),
            plays["collection_id"].astype("string").fillna("").to_numpy(dtype=object),
        )
    )
    cid = plays["collection_id"].astype("string").fillna("").to_numpy(dtype=object)[order]
    sess = plays["session_id"].astype("string").fillna("").to_numpy(dtype=object)[order]
    ts = _ns(plays["_ts"])[order]
    # Unknown creators become "" so element-wise comparisons stay boolean.
    author = plays["author"].astype("string").fillna("").to_numpy(dtype=object)[order]
    created = _ns(plays["created"])[order].astype(np.float64)
    created[created == np.iinfo(np.int64).min] = np.nan
    flags = np.zeros(n, dtype=np.int8)

    # 1. A search or follow in the window before the play.
    if events is not None and len(events):
        window_ns = int(window_s * 1e9)
        ev = events[events["activity_type"].isin(event_types)]
        ev_cid = ev["collection_id"].astype("string").fillna("").to_numpy(dtype=object)
        ev_ts = _ns(ev["_ts"])
        ev_type = ev["activity_type"].astype("string").to_numpy(dtype=object)
        ev_handle = (
            ev["extra_data"].astype("string").str.lower().fillna("").to_numpy(dtype=object)
            if "extra_data" in ev.columns
            else np.full(len(ev), None, dtype=object)
        )
        author_low = np.array([a.lower() for a in author], dtype=object)
        bounds = np.flatnonzero(np.r_[True, cid[1:] != cid[:-1], True])
        for a, b in zip(bounds[:-1], bounds[1:]):
            here = ev_cid == cid[a]
            if not here.any():
                continue
            t = ts[a:b]
            searches = ev_ts[here & (ev_type == "search")]
            flags[a:b] |= np.where(_in_window(t, searches, window_ns), SIG_SEARCH, 0).astype(
                np.int8
            )
            follow_rows = here & (ev_type == "follow")
            if not follow_rows.any():
                continue
            if not narrow_follow:
                hit = _in_window(t, ev_ts[follow_rows], window_ns)
            else:
                by_handle: dict[str, list[int]] = {}
                for h, x in zip(ev_handle[follow_rows], ev_ts[follow_rows]):
                    if isinstance(h, str) and h:
                        by_handle.setdefault(h, []).append(int(x))
                hit = np.zeros(b - a, dtype=bool)
                for k, (h, x) in enumerate(zip(author_low[a:b], t)):
                    if h and h in by_handle:
                        hit[k] = _in_window(np.array([x]), np.asarray(by_handle[h]), window_ns)[0]
            flags[a:b] |= np.where(hit, SIG_FOLLOW, 0).astype(np.int8)

    # 2. Same creator as the previous play in the same session; both known.
    known = author != ""
    prev_same = np.zeros(n, dtype=bool)
    if n > 1:
        prev_same[1:] = (
            (author[1:] == author[:-1])
            & (sess[1:] == sess[:-1])
            & (sess[1:] != "")
            & (cid[1:] == cid[:-1])
            & known[1:]
            & known[:-1]
        )
    flags |= np.where(prev_same, SIG_SAME_CREATOR, 0).astype(np.int8)

    # 3. Runs of >= min_run same-creator plays in strictly monotone posting
    # order. A run with any unknown posting time (or a repeated video, whose
    # posting time does not move) is not flagged.
    i = 0
    while i < n:
        j = i
        while j + 1 < n and prev_same[j + 1]:
            j += 1
        if j - i + 1 >= min_run:
            c = created[i : j + 1]
            if not np.isnan(c).any():
                d = np.diff(c)
                if (d > 0).all() or (d < 0).all():
                    flags[i : j + 1] |= SIG_POSTING_RUN
        i = j + 1

    out[order] = flags
    return out


def signal_counts(signals) -> dict[str, int]:
    """How many of ``signals`` carry each bit, plus ``any``."""
    s = np.asarray(signals, dtype=np.int8)
    return {
        "search": int((s & SIG_SEARCH).astype(bool).sum()),
        "follow": int((s & SIG_FOLLOW).astype(bool).sum()),
        "same_creator": int((s & SIG_SAME_CREATOR).astype(bool).sum()),
        "posting_run": int((s & SIG_POSTING_RUN).astype(bool).sum()),
        "any": int((s != 0).sum()),
    }
