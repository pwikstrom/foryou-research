"""The activity-type vocabulary every platform ingester maps onto, and its engagement helpers.

Viewing, engagement and standalone activity types, the UI labels for
engagement tokens, parsing the tokens folded into a play row's
``extra_data``, and the share-count suffix TikTok's repeated share records
become.
"""

import re

# The activity-type vocabulary. Every platform ingester maps its export's
# sections onto these values (and declares which ones it emits in its
# `emitted_activity_types`); a registry test keeps the two in step. The
# common terms and what each platform's export calls them:
#
#   fave     a like / heart        TikTok ItemFavoriteList, IG liked_posts, YT Liked videos
#   save     a bookmark            TikTok FavoriteVideoList, IG saved_posts, YT Favorites videos
#   comment  a comment the donor wrote (text in extra_data)
#   share    a share or repost     TikTok ShareHistoryList / RepostList (context = method)
#   follow   the donor followed an account (username in extra_data) — no item,
#            so it never folds onto a play; counted from standalone rows only
#
# Viewing rows (`play`, `observe`, `ad_play`) are what studies are built on.
# The remaining standalone types are kept for participant-facing stats and
# never enter a study.
VIEWING_ACTIVITY_TYPES = ("play", "observe", "ad_play")
# Organic video views: a `play` (platform exports) or an `observe`
# (Zeeschuimer, which never emits `play`). Excludes ad_play. Every view-based
# count and metric (sessions, timelines, coverage, collection stats) uses this.
VIDEO_VIEW_TYPES = ("play", "observe")
ENGAGEMENT_TYPES = ("fave", "save", "comment", "share")
STANDALONE_ACTIVITY_TYPES = ("follow", "followed_by", "search", "login", "post")
KNOWN_ACTIVITY_TYPES = frozenset(
    VIEWING_ACTIVITY_TYPES + ENGAGEMENT_TYPES + STANDALONE_ACTIVITY_TYPES
)
# Events that happen TO the account rather than BY the donor: another account
# following them. They are no part of a phone sitting, so session assignment
# leaves them out (a follower arriving mid-afternoon must not join the
# donor's lunchtime and evening scrolling into one session).
RECEIVED_ACTIVITY_TYPES = ("followed_by",)

# Human labels for the UI (Explorer facet, Timelines series, My Collections).
# The stored token stays `fave`; only the label says "Like".
ENGAGEMENT_LABELS = {"fave": "Like", "save": "Save", "comment": "Comment", "share": "Share"}
STANDALONE_ENGAGEMENT_LABELS = {**ENGAGEMENT_LABELS, "follow": "Follow"}

# Engagement tokens carried inside the folded `extra_data` column of a play
# row: a comma-separated list of "<atype>[:context]" tokens (e.g. "fave",
# "comment:hello", "save,share:copy_link") recorded when other activities
# share the same session run as the leading play, or — for engagement that
# is not adjacent to any play — folded into the nearest-in-time play of the
# same item (see ingest.derive_play_duration). Only ENGAGEMENT_TYPES fold:
# a follow has no item to fold onto.
ACTIVITY_TYPE_MAP = {t: t for t in ENGAGEMENT_TYPES}


# A TikTok share sent to several friends at once is exported as that many
# byte-identical ShareHistory records (the recipients are not in the export).
# Ingest keeps one row per send and appends the record count to the method:
# "chat_head ×3". A single record carries the bare method.
SHARE_COUNT_SEP = " ×"
_SHARE_COUNT_RE = re.compile(re.escape(SHARE_COUNT_SEP) + r"\d+$")


def share_method_with_count(method, copies: int):
    """The stored ``extra_data`` of a share row: the method, plus `` ×n`` when n > 1."""
    if not isinstance(method, str) or copies is None or int(copies) <= 1:
        return method
    return f"{method}{SHARE_COUNT_SEP}{int(copies)}"


def share_method_base(value):
    """A share row's method without the `` ×n`` record count (``None`` passes through)."""
    if not isinstance(value, str):
        return value
    return _SHARE_COUNT_RE.sub("", value)


def engagement_label(token: str) -> str:
    """The UI label for an engagement token (``'fave'`` → ``'Like'``)."""
    return STANDALONE_ENGAGEMENT_LABELS.get(token, str(token).title())


def parse_extra_data_tokens(s) -> set:
    """Parse a folded extra_data cell into its normalised engagement-type tokens."""
    if not isinstance(s, str) or not s:
        return set()
    out = set()
    for part in s.split(","):
        atype = part.split(":", 1)[0].strip().lower()
        mapped = ACTIVITY_TYPE_MAP.get(atype)
        if mapped:
            out.add(mapped)
    return out
