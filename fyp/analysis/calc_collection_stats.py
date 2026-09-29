"""Per-collection summary statistics ("personas").

Computes activity, engagement and timing statistics for each collection in an
events frame.
"""

import pandas as pd

from fyp.analysis.activity_analysis import analyze_activity_peak
from fyp.core.logging_setup import get_logger
from fyp.core.utils import VIDEO_VIEW_TYPES

logger = get_logger(__name__)


def process_single_collection(df_raw: pd.DataFrame) -> dict:
    """
    Calculates statistics for a single collection.
    """
    if df_raw.empty:
        return {}

    # 1. Prepare data
    df = df_raw.copy()
    if df.empty:
        return {}

    # 2. Filter: Start from first 'play' or 'observe' event (whichever is earliest)
    # Only events after (or including) that first event are considered relevant
    viewing_events = df[df["activity_type"].isin(VIDEO_VIEW_TYPES)]
    if not viewing_events.empty:
        first_viewing_ts = viewing_events["local_timestamp"].min()
        df = df[df["local_timestamp"] >= first_viewing_ts]
    else:
        # No play or observe events — no valid stats
        return {}

    if df.empty:
        return {}

    # 3. Basic activity stats — count only play and observe events
    total_events = len(df[df["activity_type"].isin(VIDEO_VIEW_TYPES)])
    first_date = df["local_timestamp"].min()
    last_date = df["local_timestamp"].max()
    active_days = df["local_timestamp"].dt.date.nunique()
    lifespan_days = (last_date - first_date).days + 1
    events_per_day = total_events / max(1, active_days)

    # 4. Video consumption (Play + Observe Events)
    # 'observe' activities are treated as equivalent to 'play' for stats purposes
    play_df = df[df["activity_type"].isin(VIDEO_VIEW_TYPES)].copy()

    # play_duration is only populated by collection ingesters that capture watch time
    # (DDP has it; zeeschuimer doesn't). Absent that column, watch-time stats are 0.
    if "play_duration" in play_df.columns:
        valid_watches = play_df.dropna(subset=["play_duration"])
        total_watch_time = valid_watches["play_duration"].sum()
        median_watch_time = (
            valid_watches["play_duration"].median() if not valid_watches.empty else 0
        )
    else:
        total_watch_time = 0
        median_watch_time = 0

    # 5. Engagement
    # Comments
    comments_df = df[df["activity_type"] == "comment"]
    num_comments = len(comments_df)

    # Likes: `fave` is the like/heart on every platform (TikTok ItemFavoriteList,
    # Instagram liked posts, YouTube Liked videos). Bookmarks are `save`.
    likes_df = df[df["activity_type"] == "fave"]
    num_likes = len(likes_df)

    # Posts
    posts_df = df[df["activity_type"] == "post"]
    num_posts = len(posts_df)

    # 6. Time patterns (local time)
    # The weekday name (e.g. "monday") with the most events; None when no row
    # has a weekday. Ties go to the weekday value_counts lists first.
    weekday_counts = df["local_weekday"].value_counts()
    most_active_day = str(weekday_counts.idxmax()) if not weekday_counts.empty else None

    # Activity Peak Analysis
    # We need a DF with index=timestamp, col='event_count'
    # Resample to hourly counts for analysis
    hourly_ts = df.set_index("local_timestamp").resample("h").size().to_frame(name="event_count")
    # Convert index to DatetimeIndex if it's PyArrow-backed (to access .hour)
    hourly_ts.index = pd.DatetimeIndex(hourly_ts.index)
    # Add hour column for the function
    hourly_ts["hour"] = hourly_ts.index.hour

    # Find consistent peak (3-hour window)
    peak_stats = analyze_activity_peak(hourly_ts, period_hours=3)

    # 7. Per-active-day rates
    videos_per_day = len(play_df) / max(1, active_days)
    comments_per_day = num_comments / max(1, active_days)
    likes_per_day = num_likes / max(1, active_days)

    # Compile Result
    result = {
        "collection_id": df["collection_id"].iloc[0],
        "inferred_tz_offset": float(df["tz_offset"].iloc[0]),
        "active_days": int(active_days),
        "lifespan_days": int(lifespan_days),
        "total_events": int(total_events),
        "events_per_active_day": float(events_per_day),
        "videos_per_day": float(videos_per_day),
        "comments_per_day": float(comments_per_day),
        "likes_per_day": float(likes_per_day),
        "likes_per_video": float(num_likes / max(1, len(play_df))),
        "daily_watch_time_s": float(total_watch_time / max(1, active_days)),
        "num_watches": len(play_df),
        "total_watch_time_s": float(total_watch_time),
        "median_watch_time_s": float(median_watch_time),
        "num_comments": int(num_comments),
        "num_likes": int(num_likes),
        "num_posts": int(num_posts),
        "peak_activity_hour_local": peak_stats["peak_starting_hour"],
        "most_active_weekday": most_active_day,
        # Timestamps for first/last event
        "first_event_ts": first_date.isoformat() if pd.notna(first_date) else None,
        "last_event_ts": last_date.isoformat() if pd.notna(last_date) else None,
    }

    return result


def generate_personas(events_df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculates statistics for all collections in the input DataFrame.
    """
    if events_df.empty:
        return pd.DataFrame()

    results = []

    # Group by collection and process
    # Iterating groups (rather than groupby.apply) lets one failing collection be
    # logged and skipped without losing the rest.
    grouped = events_df.groupby("collection_id")

    import traceback

    for collection_id, group in grouped:
        try:
            stats = process_single_collection(group)
            if stats:
                results.append(stats)
        except Exception as e:
            logger.error(f"Error processing collection {collection_id}: {type(e).__name__}: {e}")
            traceback.print_exc()
            continue

    return pd.DataFrame(results)
