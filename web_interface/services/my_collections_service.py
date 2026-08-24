"""Donated-data-only stats for the "My Collections" My-stuff page.

Everything here is computed from the participant's own rows of
``recoded/collections_recoded.parquet`` (plus donated-status enrichment-seed
rows and the corpus-wide ``('personas', …)`` columns of
``collections_metadata.parquet`` for population percentiles). No scrape data,
no annotations — a collection is fully explorable the moment it is ingested.

The personality bundle degrades gracefully: every section is independently
nullable and a ``capabilities`` object tells the frontend what the donation
actually contains (Instagram DDPs have plays+faves only, zeeschuimer captures
no play durations, watch-history-only donations have no engagement rows).
"""

import re
import time

import pandas as pd

import fyp.data_io as data_io
from fyp.organize_datasets import COLLECTIONS_LABEL

from ..collection_accounts import collections_for_user
from .study_data import get_collection_tags

RECODED_FILENAME = f"{COLLECTIONS_LABEL}_recoded.parquet"
METADATA_FILENAME = f"{COLLECTIONS_LABEL}_metadata.parquet"

_VIEW_TYPES = ["play", "observe"]
_LIKE_TYPES = ["fave", "like", "fave_item"]

_ACTIVITY_COLUMNS = [
    "collection_id", "activity_type", "item_id", "play_duration", "session_id",
    "local_timestamp", "local_date", "local_weekday", "local_week",
    "local_day_segment", "extra_data", "source_platform", "data_source",
]

# ('personas', …) columns used for the picker list and corpus percentiles.
_PERSONA_FIELDS = [
    "total_events", "active_days", "first_event_ts", "last_event_ts",
    "total_watch_time_s", "daily_watch_time_s", "videos_per_day",
    "num_watches", "num_comments", "num_likes", "likes_per_video",
]

_WEEKDAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday",
                  "saturday", "sunday"]

# local_day_segment value -> persona archetype (segments from
# fyp.ingest.base._day_segment_from_hour: night 0-5, morning 6-11,
# afternoon 12-17, evening 18-23).
_ARCHETYPES = {
    "morning": "Morning Person",
    "afternoon": "Afternoon Ace",
    "evening": "Night Owl",
    "night": "Overnighter",
}

# Eight-word ladders per axis, indexed by floor(score / 12.5). Second person,
# kind at both extremes — nobody gets shamed for how they scroll.
_LADDERS = {
    "patience": ["Lightning-Scrolling", "Quick-Flicking", "Speed-Sampling",
                 "Curiously Skimming", "Steady-Viewing", "Attentive",
                 "Deep-Diving", "Deep-Soaking"],
    "enthusiasm": ["Cool-Handed", "Reserved", "Selective", "Warm-Hearted",
                   "Cheerful", "Generous", "Wholehearted", "Double-Tap-Devoted"],
    "consistency": ["Free-Range", "Spontaneous", "Wandering", "Flexible",
                    "Rhythmic", "Habitual", "Clockwork", "Metronomic"],
    "binge": ["Snack-Size", "Quick-Dipping", "Casual", "Session-Sipping",
              "Steady-Streaming", "Marathon-Curious", "Binge-Artist",
              "Full-Season"],
    "chattiness": ["Silently Observing", "Quietly Lurking", "Occasionally Commenting",
                   "Measured", "Conversational", "Chatty", "Talkative",
                   "Comment-Section-Regular"],
}

# Preferred axis per statement slot, with fallbacks so the sentence never has
# holes when a donation lacks durations or engagement rows.
_STATEMENT_SLOTS = [
    ["patience", "binge"],
    ["enthusiasm", "chattiness", "binge"],
    ["consistency", "binge", "chattiness"],
]

# Pragmatic emoji matcher: pictographs, symbols and flags — deliberately NOT
# \p{Emoji}, which also matches plain digits and #/* (an old-app bug that made
# "3" many people's favourite emoji).
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # pictographs, emoticons, transport, supplemental
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
    "\U00002B00-\U00002BFF"   # arrows/stars block (⭐ etc.)
    "\U00002700-\U000027BF"
    "]"
)

_CACHE_TTL_S = 600
_bundle_cache: dict[tuple, tuple[float, dict]] = {}
_corpus_cache: dict[str, tuple[float, dict]] = {}


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def _load_metadata_personas(collection_ids: list[str] | None = None) -> pd.DataFrame | None:
    """Load the ('personas', …) columns of collections_metadata.parquet.

    Requests both the stringified-tuple and plain column names (the on-disk
    form depends on how the frame was last written — same defensive pattern
    as ``stats_service._load_collection_event_windows``).
    """
    if not data_io.exists(storage_location="recoded", filename=METADATA_FILENAME):
        return None
    columns = []
    for f in _PERSONA_FIELDS:
        columns.append(f"('personas', '{f}')")
        columns.append(f)
    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=METADATA_FILENAME,
            columns=columns,
            set_index="collection_id",
        )
    except Exception as e:
        print(f"[my_collections] failed to load collections_metadata: {e}")
        return None
    if df is None or df.empty:
        return None
    # Normalize to flat single-level column names.
    flat = {}
    for f in _PERSONA_FIELDS:
        for candidate in (("personas", f), f):
            if candidate in df.columns:
                flat[f] = df[candidate]
                break
    if not flat:
        return None
    out = pd.DataFrame(flat)
    out.index = out.index.map(str)
    if collection_ids is not None:
        out = out[out.index.isin([str(c) for c in collection_ids])]
    return out


def _load_activities(collection_ids: list[str]) -> pd.DataFrame | None:
    """Load the donated activity rows for ``collection_ids`` (base columns only)."""
    if not collection_ids:
        return None
    if not data_io.exists(storage_location="recoded", filename=RECODED_FILENAME):
        return None
    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=RECODED_FILENAME,
            columns=_ACTIVITY_COLUMNS,
            filters=[("collection_id", "in", [str(c) for c in collection_ids])],
        )
    except Exception as e:
        print(f"[my_collections] failed to load activities: {e}")
        return None
    if df is None or df.empty:
        return None
    df["local_timestamp"] = pd.to_datetime(df["local_timestamp"], errors="coerce")
    # local_hour is contract-derived but not persisted in the master parquet.
    df["local_hour"] = df["local_timestamp"].dt.hour
    return df


def _load_donated_seed_row(platform: str, data_source: str, item_id: str) -> dict | None:
    """Look up a donated (never scraped) enrichment-seed row for one item.

    YouTube DDPs carry titles/channels and Instagram DDPs carry captions/owners
    without any scraping; TikTok supplies nothing. Rows with any other
    ``scrape_status`` are ignored — this page must never surface scrape data.
    """
    filename = f"{platform}_{data_source}_enrichment_seed.parquet"
    if not data_io.exists(storage_location="recoded", filename=filename):
        return None
    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=filename,
            columns=["item_id", "desc", "author_name", "scrape_status"],
            filters=[("item_id", "==", str(item_id))],
        )
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df[df["scrape_status"] == "donated"]
    if df.empty:
        return None
    row = df.iloc[0]
    desc = row.get("desc")
    author = row.get("author_name")
    return {
        "desc": None if pd.isna(desc) else str(desc),
        "author_name": None if pd.isna(author) else str(author),
    }


# ---------------------------------------------------------------------------
# Corpus percentiles
# ---------------------------------------------------------------------------

def corpus_percentile_frame(force: bool = False) -> pd.DataFrame | None:
    """The full corpus personas frame, cached for ``_CACHE_TTL_S`` seconds."""
    now = time.time()
    hit = _corpus_cache.get("frame")
    if not force and hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    df = _load_metadata_personas(None)
    _corpus_cache["frame"] = (now, df)
    return df


def _percentile(series: pd.Series | None, value: float | None) -> float | None:
    """Share of corpus collections with a strictly smaller value, as 0-100."""
    if value is None or series is None:
        return None
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 3:  # a percentile against 1-2 peers is noise, not honesty
        return None
    return round(float((s < value).mean()) * 100, 1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_owned_collections(username: str) -> list[dict]:
    """Light per-collection metadata for the picker cards."""
    cids = collections_for_user(username)
    if not cids:
        return []
    tags = get_collection_tags() or {}
    meta = _load_metadata_personas(cids)

    # One cheap two-column scan for platform/source (metadata doesn't carry it).
    platforms: dict[str, dict] = {}
    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=RECODED_FILENAME,
            columns=["collection_id", "source_platform", "data_source"],
            filters=[("collection_id", "in", cids)],
        )
        if df is not None and not df.empty:
            for cid, grp in df.groupby("collection_id"):
                platforms[str(cid)] = {
                    "source_platform": str(grp["source_platform"].mode().iloc[0]),
                    "data_source": str(grp["data_source"].mode().iloc[0]),
                }
    except Exception as e:
        print(f"[my_collections] platform lookup failed: {e}")

    out = []
    for cid in cids:
        entry = tags.get(cid) if isinstance(tags.get(cid), dict) else {}
        item = {
            "collection_id": cid,
            "display_id": entry.get("display_collection_id") or cid,
            "source_platform": platforms.get(cid, {}).get("source_platform"),
            "data_source": platforms.get(cid, {}).get("data_source"),
            "total_events": None,
            "active_days": None,
            "first_event_ts": None,
            "last_event_ts": None,
            "total_watch_time_s": None,
        }
        if meta is not None and cid in meta.index:
            row = meta.loc[cid]
            for f in ("total_events", "active_days", "total_watch_time_s"):
                v = row.get(f)
                item[f] = None if pd.isna(v) else float(v)
            for f in ("first_event_ts", "last_event_ts"):
                v = row.get(f)
                item[f] = None if (v is None or pd.isna(v)) else str(v)
        out.append(item)
    return out


def build_personality(collection_ids: list[str]) -> dict | None:
    """The full "My Short-Video Personality" bundle for one or more collections.

    Returns None when no donated activity rows exist for the ids.
    """
    key = tuple(sorted(str(c) for c in collection_ids))
    now = time.time()
    hit = _bundle_cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]

    df = _load_activities(list(key))
    if df is None or df.empty:
        return None

    bundle = _compute_bundle(df, list(key))
    _bundle_cache[key] = (now, bundle)
    return bundle


def invalidate_cache() -> None:
    _bundle_cache.clear()
    _corpus_cache.clear()


# ---------------------------------------------------------------------------
# Bundle computation
# ---------------------------------------------------------------------------

def _compute_bundle(df: pd.DataFrame, collection_ids: list[str]) -> dict:
    plays = df[df["activity_type"].isin(_VIEW_TYPES)]
    likes = df[df["activity_type"].isin(_LIKE_TYPES)]
    comments = df[df["activity_type"] == "comment"]
    posts = df[df["activity_type"] == "post"]
    searches = df[df["activity_type"] == "search"]

    durations = pd.to_numeric(plays["play_duration"], errors="coerce").dropna() \
        if "play_duration" in plays.columns else pd.Series(dtype=float)
    has_durations = len(plays) > 0 and (len(durations) / len(plays)) >= 0.05

    capabilities = {
        "has_watch_durations": bool(has_durations),
        "has_likes": len(likes) > 0,
        "has_comments": len(comments) > 0,
        "has_posts": len(posts) > 0,
        "has_searches": len(searches) > 0,
        "has_sessions": bool("session_id" in df.columns and df["session_id"].notna().any()),
    }

    sessions = _session_stats(df) if capabilities["has_sessions"] else None
    corpus = corpus_percentile_frame()
    n_corpus = int(len(corpus)) if corpus is not None else 0

    axes = _persona_axes(plays, likes, comments, durations, sessions, corpus)
    persona = _persona_statement(axes, plays)
    persona["n_corpus"] = n_corpus

    platforms = sorted(str(p) for p in df["source_platform"].dropna().unique())

    bundle = {
        "collection_ids": collection_ids,
        "platforms": platforms,
        "capabilities": capabilities,
        "persona": persona,
        "hour_of_day": _hour_of_day(plays),
        "weekday": _weekday(plays),
        "weekly": _weekly(plays),
        "calendar": _calendar(plays),
        "doomscroll": _doomscroll(durations) if has_durations else None,
        "rewatch": _rewatch(plays),
        "searches": _search_terms(searches) if capabilities["has_searches"] else None,
        "emoji": _favourite_emoji(comments) if capabilities["has_comments"] else None,
        "stats": _stat_strip(df, plays, likes, comments, posts, durations,
                             sessions, has_durations, corpus),
    }
    return bundle


def _session_stats(df: pd.DataFrame) -> dict:
    grp = df.dropna(subset=["session_id"]).groupby("session_id")["local_timestamp"]
    spans = (grp.max() - grp.min()).dt.total_seconds()
    if spans.empty:
        return {"n_sessions": 0, "longest_s": 0.0, "binge_share": None}
    return {
        "n_sessions": int(len(spans)),
        "longest_s": float(spans.max()),
        "binge_share": float((spans > 1200).mean()),
    }


def _persona_axes(plays, likes, comments, durations, sessions, corpus) -> dict:
    """The five axes: score 0-100 (fractions as-is, ratios as corpus percentiles)."""
    n_plays = max(1, len(plays))
    axes: dict[str, dict] = {}

    # Patience — share of watches held for 30s or more (fraction, no norm).
    if len(durations) > 0:
        axes["patience"] = {"score": round(float((durations >= 30).mean()) * 100, 1),
                            "percentile": None}
    else:
        axes["patience"] = {"score": None, "percentile": None}

    # Binge — share of sessions longer than 20 minutes (fraction).
    if sessions and sessions["n_sessions"] > 0 and sessions["binge_share"] is not None:
        axes["binge"] = {"score": round(sessions["binge_share"] * 100, 1),
                         "percentile": None}
    else:
        axes["binge"] = {"score": None, "percentile": None}

    # Consistency — share of views inside the two favourite hours (fraction).
    if len(plays) > 0:
        hour_counts = plays["local_hour"].value_counts()
        axes["consistency"] = {
            "score": round(float(hour_counts.nlargest(2).sum() / len(plays)) * 100, 1),
            "percentile": None,
        }
    else:
        axes["consistency"] = {"score": None, "percentile": None}

    # Chattiness — comments per play, ranked against the corpus.
    if len(comments) > 0:
        own = len(comments) / n_plays
        corpus_series = None
        if corpus is not None and {"num_comments", "num_watches"} <= set(corpus.columns):
            watches = pd.to_numeric(corpus["num_watches"], errors="coerce")
            corpus_series = pd.to_numeric(corpus["num_comments"], errors="coerce") / watches.clip(lower=1)
        pct = _percentile(corpus_series, own)
        axes["chattiness"] = {"score": pct if pct is not None else min(100.0, round(own * 100, 1)),
                              "percentile": pct}
    else:
        axes["chattiness"] = {"score": None, "percentile": None}

    # Enthusiasm — likes per play, ranked against the corpus likes_per_video.
    if len(likes) > 0:
        own = len(likes) / n_plays
        corpus_series = corpus["likes_per_video"] if corpus is not None and "likes_per_video" in corpus.columns else None
        pct = _percentile(corpus_series, own)
        axes["enthusiasm"] = {"score": pct if pct is not None else min(100.0, round(own * 100, 1)),
                              "percentile": pct}
    else:
        axes["enthusiasm"] = {"score": None, "percentile": None}

    return axes


def _persona_statement(axes: dict, plays: pd.DataFrame) -> dict:
    """Pick three ladder words + the time-of-day archetype, backfilling gaps."""
    used: set[str] = set()
    words = []
    for slot in _STATEMENT_SLOTS:
        picked = None
        for axis in slot:
            if axis in used:
                continue
            score = axes.get(axis, {}).get("score")
            if score is not None:
                idx = min(7, int(score // 12.5))
                picked = {"axis": axis, "word": _LADDERS[axis][idx]}
                used.add(axis)
                break
        words.append(picked)
    words = [w for w in words if w]

    # Archetype from the day-segment shares of the viewing events.
    archetype = None
    segment_shares = {}
    if len(plays) > 0 and "local_day_segment" in plays.columns:
        shares = plays["local_day_segment"].value_counts(normalize=True)
        segment_shares = {str(k): round(float(v), 4) for k, v in shares.items()}
        top = shares.idxmax()
        archetype = _ARCHETYPES.get(str(top))

    statement = None
    if words and archetype:
        adjectives = [w["word"] for w in words]
        article = "an" if adjectives[0][0].upper() in "AEIOU" else "a"
        if len(adjectives) == 1:
            joined = adjectives[0]
        else:
            joined = ", ".join(adjectives[:-1]) + f", {adjectives[-1]}"
        statement = f"You are {article} {joined} {archetype}!"

    return {
        "statement": statement,
        "words": words,
        "archetype": archetype,
        "segment_shares": segment_shares,
        "axes": axes,
    }


def _hour_of_day(plays: pd.DataFrame) -> dict | None:
    if plays.empty:
        return None
    counts = plays["local_hour"].value_counts()
    full = [int(counts.get(h, 0)) for h in range(24)]
    peak = int(counts.idxmax())
    return {"counts": full, "peak_hour": peak, "peak_label": _friendly_hour(peak)}


def _friendly_hour(hour: int) -> str:
    if hour == 0:
        return "midnight"
    if hour == 12:
        return "noon"
    if hour < 12:
        return f"{hour} AM"
    return f"{hour - 12} PM"


def _weekday(plays: pd.DataFrame) -> dict | None:
    if plays.empty:
        return None
    counts = plays["local_weekday"].astype(str).str.lower().value_counts()
    ordered = {d: int(counts.get(d, 0)) for d in _WEEKDAY_ORDER}
    top = max(ordered, key=ordered.get)
    return {"counts": ordered, "top": top}


def _weekly(plays: pd.DataFrame) -> dict | None:
    if plays.empty:
        return None
    counts = plays.groupby("local_week").size()

    def _week_key(w: str):
        try:
            y, wk = str(w).split("-")
            return (int(y), int(wk))
        except Exception:
            return (0, 0)

    weeks = sorted((str(w) for w in counts.index), key=_week_key)
    series = [{"week": w, "count": int(counts[w])} for w in weeks]
    top = max(series, key=lambda d: d["count"])
    return {"series": series, "top_week": top}


def _calendar(plays: pd.DataFrame) -> dict | None:
    if plays.empty:
        return None
    dates = pd.to_datetime(plays["local_date"].astype(str), errors="coerce").dropna()
    if dates.empty:
        return None
    counts = dates.dt.date.value_counts().sort_index()
    days = [{"date": d.isoformat(), "count": int(c)} for d, c in counts.items()]

    # Longest run of consecutive active days.
    longest = streak = 1
    prev = None
    for d in counts.index:
        if prev is not None and (d - prev).days == 1:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 1
        prev = d
    return {"days": days, "longest_streak": int(longest)}


def _doomscroll(durations: pd.Series) -> dict | None:
    if durations.empty:
        return None
    buckets = {
        "under_3s": int((durations < 3).sum()),
        "3_10s": int(((durations >= 3) & (durations < 10)).sum()),
        "10_30s": int(((durations >= 10) & (durations < 30)).sum()),
        "30_60s": int(((durations >= 30) & (durations < 60)).sum()),
        "over_60s": int((durations >= 60).sum()),
    }
    return {
        "buckets": buckets,
        "n_watches": int(len(durations)),
        "median_s": float(durations.median()),
    }


def _rewatch(plays: pd.DataFrame) -> dict | None:
    """The most-rewatched item, if anything was watched 10+ times."""
    ids = plays["item_id"].dropna()
    ids = ids[ids.astype(str).str.len() > 0]
    if ids.empty:
        return None
    counts = ids.value_counts()
    top_id = str(counts.index[0])
    n = int(counts.iloc[0])
    if n < 10:
        return None
    row = plays[plays["item_id"] == counts.index[0]].iloc[0]
    platform = str(row.get("source_platform") or "")
    data_source = str(row.get("data_source") or "")

    url = None
    if platform == "youtube":
        url = f"https://www.youtube.com/watch?v={top_id}"
    elif platform == "tiktok" and top_id.isdigit():
        url = f"https://www.tiktok.com/@_/video/{top_id}"
    # Instagram item ids are internal media pks — no public URL can be built.

    seed = _load_donated_seed_row(platform, data_source, top_id) or {}
    return {
        "item_id": top_id,
        "count": n,
        "platform": platform,
        "url": url,
        "desc": seed.get("desc"),
        "author_name": seed.get("author_name"),
    }


def _search_terms(searches: pd.DataFrame) -> dict | None:
    terms = searches["extra_data"].dropna().astype(str).str.strip().str.lower()
    terms = terms[terms.str.len() > 0]
    if terms.empty:
        return None
    top = terms.value_counts().head(10)
    return {
        "n_searches": int(len(terms)),
        "top_terms": [{"term": str(t), "count": int(c)} for t, c in top.items()],
    }


def _favourite_emoji(comments: pd.DataFrame) -> dict | None:
    text = " ".join(comments["extra_data"].dropna().astype(str).tolist())
    found = _EMOJI_RE.findall(text)
    if not found:
        return None
    counts = pd.Series(found).value_counts()
    return {"top": str(counts.index[0]), "count": int(counts.iloc[0])}


def _stat_strip(df, plays, likes, comments, posts, durations, sessions,
                has_durations, corpus) -> dict:
    total_watch_s = float(durations.sum()) if has_durations else None
    watch_pct = None
    if total_watch_s is not None and corpus is not None and "total_watch_time_s" in corpus.columns:
        watch_pct = _percentile(corpus["total_watch_time_s"], total_watch_s)

    # Date range and active days follow the VIEWING events, matching the
    # charts — a TikTok like-list can reach years further back than the
    # donated watch history, and "you spent X watching between A and B"
    # must not stretch over that gap.
    ts = plays["local_timestamp"].dropna()
    if ts.empty:
        ts = df["local_timestamp"].dropna()
    return {
        "n_videos": int(len(plays)),
        "n_likes": int(len(likes)),
        "n_comments": int(len(comments)),
        "n_posts": int(len(posts)),
        "under_3s": int((durations < 3).sum()) if has_durations else None,
        "over_60s": int((durations >= 60).sum()) if has_durations else None,
        "total_watch_time_s": total_watch_s,
        "watch_time_percentile": watch_pct,
        "n_sessions": sessions["n_sessions"] if sessions else None,
        "longest_session_s": sessions["longest_s"] if sessions else None,
        "active_days": int(ts.dt.date.nunique()) if not ts.empty else 0,
        "first_date": ts.min().date().isoformat() if not ts.empty else None,
        "last_date": ts.max().date().isoformat() if not ts.empty else None,
    }
