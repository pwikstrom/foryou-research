
import pandas as pd
import numpy as np
import math
from datetime import timedelta
import emoji
from collections import Counter
from fyp.activity_analysis import analyze_activity_peak
import json
import os
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder
import pytz
from datetime import datetime
import time

def infer_timezone_offset(timestamps: pd.Series) -> float:
    """
    Infers timezone offset by finding the 4-hour window with minimum activity.
    Assumes this quietest window centers around 04:00 local time.
    
    Args:
        timestamps: Series of UTC timestamps
        
    Returns:
        Offset in hours (float) from UTC. e.g. +10.0 for Brisbane.
    """
    if len(timestamps) < 10:
        return 0.0 # Not enough data to infer
        
    # Create a DataFrame to aggregate by hour
    df_ts = pd.DataFrame({'ts': timestamps})
    df_ts['hour'] = df_ts['ts'].dt.hour
    
    # Count activity per UTC hour (0-23)
    hourly_counts = df_ts.groupby('hour').size().reindex(range(24), fill_value=0)
    
    # We want a rolling 4-hour window sum. 
    # To handle wrap-around (e.g. 23:00 -> 02:00), we concat the counts 
    hourly_counts_ext = pd.concat([hourly_counts, hourly_counts.iloc[:3]], ignore_index=True)
    
    # Calculate rolling sum
    rolling_sum = hourly_counts_ext.rolling(window=4).sum()
    
    # We strip the first 3 (NaNs/partial from standard rolling if not min_periods=0) 
    # but we used concat so we have valid range. 
    # The result has length 24 + 3 = 27.
    # Indices 0,1,2 are NaNs (window size 4).
    # Valid indices start at 3.
    # Index 3 corresponds to window [0,1,2,3] of extended array = [0,1,2,3] of original.
    # Index 26 corresponds to window [23,0,1,2].
    
    # Extract only the 24 valid windows representing starts 0..23 (wrapped)
    # Window ending at i (where i >= 3) corresponds to hours ...?
    # Let's map rolling_sum index to "Center Hour".
    # We want indices 3 to 26 inclusive (24 values).
    valid_sums = rolling_sum.iloc[3:].reset_index(drop=True)
    # valid_sums now has indices 0 to 23.
    # Index k in valid_sums came from rolling_sum index k+3.
    # rolling_sum index k+3 sums extended array [k, k+1, k+2, k+3].
    # Which corresponds to hours [k%24, (k+1)%24, (k+2)%24, (k+3)%24].
    # Center is roughly k + 1.5.
    
    min_val = valid_sums.min()
    min_indices = valid_sums[valid_sums == min_val].index.tolist()
    
    # Calculate circular mean of these indices
    # Convert hours (indices) to angles, mean vector, convert back
    angles = [2 * np.pi * idx / 24.0 for idx in min_indices]
    y = np.sum(np.sin(angles))
    x = np.sum(np.cos(angles))
    avg_angle = np.arctan2(y, x)
    avg_idx = avg_angle * 24.0 / (2 * np.pi)
    
    if avg_idx < 0:
        avg_idx += 24
        
    # avg_idx represents the "Start Hour" of the window (k).
    # Center of window is k + 2.0 (Midpoint of 4 discrete hour buckets [k, k+3]).
    # e.g. Window [2,3,4,5] -> Center is 4.0.
    # We assume this center is 03:00 Local (Shifted -1 from original 04:00).
    
    center_utc = avg_idx + 2.0
    if center_utc >= 24:
        center_utc -= 24
        
    # Offset = Local - UTC = 3.0 - Center (Shifted -1 from 4.0)
    offset = 3.0 - center_utc
    
    # Normalize to -9 to 15 (User specified range to handle date line wrap)
    # "Add 24 hours to timezones calculated to UTC-11" -> Map -11 to +13.
    # Standard range [-9, 15] covers West Coast US (-8) to NZ (+12/13).
    while offset < -9:
        offset += 24
    while offset > 15:
        offset -= 24
        
    return round(offset) # Round to nearest hour for simplicity (or keeping half hours?)
                         # User said rough guess. 
                         






def analyze_emojis_list(comments: list) -> dict:
    """
    Extracts and analyzes emojis from a list of comment strings.
    """
    if not comments:
        return {"top_emoji": None, "emoji_rate": 0.0}
        
    all_text = "".join([str(c) for c in comments if pd.notna(c)])
    emoji_list = emoji.emoji_list(all_text)
    
    if not emoji_list:
        return {"top_emoji": None, "emoji_rate": 0.0}
        
    # Count emojis
    emojis_found = [e['emoji'] for e in emoji_list]
    counts = Counter(emojis_found)
    
    top_emoji, count = counts.most_common(1)[0]
    
    # Rate: Emojis per character of text? Or just total count? 
    # Old logic: emoji_count / total_char_count.
    total_len = len(all_text)
    rate = count / total_len if total_len > 0 else 0
    
    return {"top_emoji": top_emoji, "emoji_rate": rate}


def generate_moniker(pers: dict, time_of_day_shares: dict) -> str:
    """
    Generates a descriptive moniker based on persona statistics.
    """
    if not time_of_day_shares:
        return "Unknown Persona"
        
    # Composite scores
    composite_expressiveness = pers.get("chattiness", 0)
    composite_watching = (pers.get("patience", 0) + pers.get("binge_level", 0)) / 2

    adjectives_for_expressiveness = [
        "Quiet", "Reserved", "Mellow", "Chatty", "Talkative", "Expressive", "Vocal", "Outgoing"
    ]
    adjectives_for_enthusiasm = [
        "Cool", "Steady", "Curious", "Cheerful", "Energetic", "Exited", "Spirited", "Ecstatic"
    ]
    adjectives_for_watching = [
        "Nibbling", "Sampling", "Dabbling", "Exploring", "Appreciating", "Enthusiastic", "Savouring", "Connoisseuring"
    ]

    # Determine dominant time of day
    day_person_key = max(time_of_day_shares, key=time_of_day_shares.get)
    
    label_map = {
        'Morning': 'Morning Person',
        'Afternoon': 'Afternoon Ace',
        'Evening': 'Night Owl',
        'Owl': 'Overnighter'
    }
    day_person_label = label_map.get(day_person_key, "Person")

    # Map scores to indices
    def get_index(score, array):
        idx = int(math.floor(score * len(array)))
        return min(idx, len(array) - 1)

    idx_expr = get_index(composite_expressiveness, adjectives_for_expressiveness)
    idx_enth = get_index(pers.get("enthusiasm", 0), adjectives_for_enthusiasm)
    idx_watch = get_index(composite_watching, adjectives_for_watching)

    return f"{adjectives_for_expressiveness[idx_expr]}, {adjectives_for_enthusiasm[idx_enth]}, {adjectives_for_watching[idx_watch]} {day_person_label}"



def process_single_donation(df_raw: pd.DataFrame) -> dict:
    """
    Calculates statistics for a single donation (user).
    """
    if df_raw.empty:
        return {}
        
    # 1. Prepare Data
    # Filter to valid dates
    df = df_raw.dropna(subset=['date']).copy()
    if df.empty:
        return {}
        
    df['date'] = pd.to_datetime(df['date'], utc=True)
    df = df.sort_values('date')
    
    # 2. Filter: Start from first 'watch' event
    # Only events after (or including) the first watch event are considered relevant
    watch_events = df[df['feature_name'] == 'watch']
    if not watch_events.empty:
        first_watch_ts = watch_events['date'].iloc[0]
        df = df[df['date'] >= first_watch_ts]
    else:
        # If no watch events, arguably no valid stats?
        # User said "screws up the stats" relating to watch based anchors.
        # Let's return empty if no watch events found, as implied by requirement.
        return {}

    if df.empty:
        return {}
        
    # 3. Infer Timezone
    tz_offset = infer_timezone_offset(df['date'])
    
    # Create Local Time column
    df['local_date'] = df['date'] + pd.Timedelta(hours=tz_offset)
    df['local_hour'] = df['local_date'].dt.hour
    
    # 4. Basic Activity Stats
    total_events = len(df)
    first_date = df['local_date'].min()
    last_date = df['local_date'].max()
    active_days = df['local_date'].dt.date.nunique()
    lifespan_days = (last_date - first_date).days + 1
    events_per_day = total_events / max(1, active_days)
    
    # 5. Video Consumption (Watch Events)
    # Using 'watch' feature and 'secondary_value' (duration)
    # Ensure numeric conversion
    watch_df = df[df['feature_name'] == 'watch'].copy()
    watch_df['duration'] = pd.to_numeric(watch_df['secondary_value'], errors='coerce')
    
    # Filter insane durations (> 1 hour?) or keep all? 
    # Ideally filter outliers or very long paused videos
    valid_watches = watch_df.dropna(subset=['duration'])
    # Keeping logic from old code: duration <= 300s considered 'normal' short form watch?
    # User didn't specify, but old code did. Let's keep raw metrics then stats on filtered.
    
    total_watch_time = valid_watches['duration'].sum()
    avg_watch_time = valid_watches['duration'].mean() if not valid_watches.empty else 0
    median_watch_time = valid_watches['duration'].median() if not valid_watches.empty else 0
    
    # 6. Sessions
    # Defined by gap > 15 mins (900s)
    # Calculate time diffs
    df['prev_ts'] = df['date'].shift(1)
    df['diff_sec'] = (df['date'] - df['prev_ts']).dt.total_seconds()
    
    # New session if diff > 900 or first event (NaN)
    input_session_gap = 15 * 60
    df['is_new_session'] = (df['diff_sec'] > input_session_gap) | (df['diff_sec'].isna())
    df['session_id'] = df['is_new_session'].astype(int).cumsum()
    
    num_sessions = df['session_id'].max()
    
    # Session Durations
    session_stats = df.groupby('session_id')['date'].agg(start_time='min', end_time='max')
    session_stats['duration'] = (session_stats['end_time'] - session_stats['start_time']).dt.total_seconds()
    
    avg_session_duration = session_stats['duration'].mean()
    longest_session = session_stats['duration'].max()
    
    # Binge Level: % sessions longer than 20 mins (1200s)
    long_sessions = (session_stats['duration'] > 1200).sum()
    binge_level = long_sessions / max(1, num_sessions)
    
    # 7. Engagement Rates
    # Comments
    comments_df = df[df['feature_name'] == 'comment']
    num_comments = len(comments_df)
    
    # Likes
    # Mapped from 'ItemFavoriteList' in donations.py -> 'fave_item'
    likes_df = df[df['feature_name'].isin(['like', 'fave_item'])]
    num_likes = len(likes_df)
    
    # Posts
    posts_df = df[df['feature_name'] == 'post']
    num_posts = len(posts_df)
    
    # Emojis in comments
    comment_texts = comments_df['primary_value'].tolist()
    emoji_stats = analyze_emojis_list(comment_texts)
    
    # 8. Advanced Behavioural Metrics (New)
    # A. Session Velocity (Doomscroll Index)
    # Videos per minute of active session time
    # Check for valid session duration
    total_session_minutes = session_stats['duration'].sum() / 60.0
    if total_session_minutes > 1.0:
        session_velocity_vpm = len(watch_df) / total_session_minutes
    else:
        session_velocity_vpm = 0.0 # undefined or too short
        
    # B. Weekend Bias
    # Ratio of avg events/day (Sat-Sun) vs (Mon-Fri)
    # 'weekday' is 0=Mon, 6=Sun. Weekend = 5,6.
    # Group by date first to count events per day
    daily_events = df.groupby(df['local_date'].dt.date).size()
    # Map each date to weekend (True/False)
    # Use values to avoid index alignment issues (Series vs DateIndex)
    is_weekend = pd.to_datetime(daily_events.index).weekday.isin([5,6])
    
    avg_weekend = daily_events[is_weekend].mean() if is_weekend.any() else 0
    avg_weekday = daily_events[~is_weekend].mean() if (~is_weekend).any() else 0
    
    if avg_weekday > 0:
        weekend_bias = avg_weekend / avg_weekday
    elif avg_weekend > 0:
        weekend_bias = 10.0 # Extreme bias
    else:
        weekend_bias = 0.0 # No activity?
        
    # C. Comment Depth (Avg chars)
    # Handle mixed types in primary_value
    if num_comments > 0:
         chars = comments_df['primary_value'].astype(str).str.len()
         avg_comment_len_chars = chars.mean()
    else:
        avg_comment_len_chars = 0.0
        
    # D. Activity Trend (Slope)
    # Events per day index (0..n)
    if len(daily_events) > 1:
        y = daily_events.values
        x = np.arange(len(y))
        # Simple linreg: slope
        slope, _ = np.polyfit(x, y, 1)
        activity_trend_slope = slope
    else:
        activity_trend_slope = 0.0
        
    # E. Return Probability
    # Prob of being active on D+1 given active on D
    active_dates_sorted = sorted(daily_events.index)
    if len(active_dates_sorted) > 1:
        consecutive_count = 0
        for i in range(len(active_dates_sorted) - 1):
             if (active_dates_sorted[i+1] - active_dates_sorted[i]).days == 1:
                 consecutive_count += 1
        day_to_day_return_prob = consecutive_count / (len(active_dates_sorted) - 1)
    else:
        day_to_day_return_prob = 0.0

    # 9. Time Patterns (Local Time)
    # Weekday Shares (Monday=0 in Python)
    df['weekday'] = df['local_date'].dt.weekday
    weekday_counts = df['weekday'].value_counts(normalize=True).reindex(range(7), fill_value=0)
    # Map to list [Mon, Tue, ... Sun]
    weekday_shares = weekday_counts.sort_index().tolist()
    
    days_week = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    most_active_day_idx = weekday_counts.idxmax()
    most_active_day = days_week[most_active_day_idx]
    
    # Time of Day Buckets
    # Morning (5-11), Afternoon (12-17), Evening (18-23), Owl (0-4)
    def assign_tod(h):
        if 5 <= h < 12: return 'Morning'
        elif 12 <= h < 18: return 'Afternoon'
        elif 18 <= h <= 23: return 'Evening'
        else: return 'Owl'
        
    df['tod'] = df['local_hour'].apply(assign_tod)
    tod_shares = df['tod'].value_counts(normalize=True).to_dict()
    
    # Activity Peak Analysis
    # We need a DF with index=timestamp, col='event_count'
    # Resample to hourly counts for analysis
    # Resample to hourly counts for analysis
    hourly_ts = df.set_index('local_date').resample('h').size().to_frame(name='event_count')
    # Convert index to DatetimeIndex if it's PyArrow-backed (to access .hour)
    hourly_ts.index = pd.DatetimeIndex(hourly_ts.index)
    # Add hour column for the function
    hourly_ts['hour'] = hourly_ts.index.hour
    
    # Find consistent peak (3-hour window)
    peak_stats = analyze_activity_peak(hourly_ts, period_hours=3)
    
    # 10. Advanced Persona Stats (Expressiveness, etc.)
    # Videos per day (using lifespan)
    videos_per_day = len(watch_df) / max(1, lifespan_days)
    comments_per_day = num_comments / max(1, lifespan_days)
    likes_per_day = num_likes / max(1, lifespan_days)
    
    # Chattiness (Expressiveness)
    # Capped at 1.0
    chattiness = comments_per_day / videos_per_day if videos_per_day > 0 else 0
    chattiness = min(1.0, chattiness)
    
    # Enthusiasm
    enthusiasm = likes_per_day / videos_per_day if videos_per_day > 0 else 0
    enthusiasm = min(1.0, enthusiasm)
    
    # Patience: % of watches >= 30s
    if not valid_watches.empty:
        patience = (valid_watches['duration'] >= 30).mean()
    else:
        patience = 0.0
        
    # Generate Moniker
    pers_input = {
        "chattiness": chattiness,
        "patience": patience,
        "binge_level": binge_level,
        "enthusiasm": enthusiasm
    }
    moniker = generate_moniker(pers_input, tod_shares)

    # Consistency (Legacy: Share of top 2 hours)
    if not hourly_ts.empty:
        hourly_profile_shares = hourly_ts.groupby('hour')['event_count'].mean() 
        hourly_profile_shares = hourly_profile_shares / hourly_profile_shares.sum() if hourly_profile_shares.sum() > 0 else hourly_profile_shares
        consistency_top_2 = hourly_profile_shares.nlargest(2).sum()
    else:
        consistency_top_2 = 0.0
        
    # Emoji Level (Log)
    emoji_level = math.log(1 + emoji_stats['emoji_rate'])

    # Compile Result
    result = {
        'donation_id': df['donation_id'].iloc[0],
        'inferred_tz_offset': float(tz_offset),
        'active_days': int(active_days),
        'lifespan_days': int(lifespan_days),
        'total_events': int(total_events),
        'events_per_active_day': float(events_per_day),
        'sessions_per_day': float(num_sessions / max(1, lifespan_days)),
        'videos_per_day': float(videos_per_day),
        'comments_per_day': float(comments_per_day),
        'likes_per_day': float(likes_per_day),
        'likes_per_video': float(num_likes / max(1, len(watch_df))),
        'daily_watch_time_s': float(total_watch_time / max(1, lifespan_days)),
        
        'num_watches': len(watch_df),
        'total_watch_time_s': float(total_watch_time),
        'avg_watch_time_s': float(avg_watch_time),
        'median_watch_time_s': float(median_watch_time),
        
        'num_sessions': int(num_sessions),
        'avg_session_duration_s': float(avg_session_duration),
        'longest_session_s': float(longest_session),
        'binge_level': float(binge_level),
        
        'num_comments': int(num_comments),
        'num_likes': int(num_likes),
        'num_posts': int(num_posts),
        'top_emoji': emoji_stats['top_emoji'],
        'emoji_rate': float(emoji_stats['emoji_rate']),
        'emoji_level_log': float(emoji_level),
        
        'peak_activity_hour_local': peak_stats['peak_starting_hour'],
        'activity_consistency_cv': peak_stats['consistency_cv'],
        'consistency_top_2_hours': float(consistency_top_2),
        
        # New Detailed Metrics
        'session_velocity_vpm': float(session_velocity_vpm),
        'weekend_bias': float(weekend_bias),
        'avg_comment_len_chars': float(avg_comment_len_chars),
        'activity_trend_slope': float(activity_trend_slope),
        'day_to_day_return_prob': float(day_to_day_return_prob),
        
        'share_morning': tod_shares.get('Morning', 0.0),
        'share_afternoon': tod_shares.get('Afternoon', 0.0),
        'share_evening': tod_shares.get('Evening', 0.0),
        'share_owl': tod_shares.get('Owl', 0.0),
        'peak_day_segment': max(tod_shares, key=tod_shares.get) if tod_shares else 'Unknown',
        
        # Advanced Persona
        'expressiveness': float(chattiness), # Requested alias for chattiness
        'chattiness': float(chattiness),
        'enthusiasm': float(enthusiasm),
        'patience': float(patience),
        'moniker': moniker,
        'most_active_weekday': most_active_day,
        
        # Timestamps for first/last event
        'first_event_ts': first_date.isoformat() if pd.notna(first_date) else None,
        'last_event_ts': last_date.isoformat() if pd.notna(last_date) else None,

        # Storing raw lists for arrays if needed (e.g. for charts)
        # 'weekday_shares': weekday_shares, 
    }
    
    return result


def calculate_all_donation_stats(events_df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculates statistics for all donations in the input DataFrame.
    """
    if events_df.empty:
        return pd.DataFrame()
        
    results = []
    
    # Group by donation and process
    # Using groupby apply might be slow for complex logic, iterating groups is safer for debugging
    grouped = events_df.groupby('donation_id')
    
    for donation_id, group in grouped:
        try:
            stats = process_single_donation(group)
            if stats:
                results.append(stats)
        except Exception as e:
            print(f"Error processing donation {donation_id}: {e}")
            continue
            
    return pd.DataFrame(results)


# --- Location-based Timezone Inference & Caching ---

_geocoder = None
_timezone_finder = None

def _get_geocoder():
    """Lazy-load the geocoder."""
    global _geocoder
    if _geocoder is None:
        _geocoder = Nominatim(user_agent="fyp_persona_explorer")
    return _geocoder

def _get_timezone_finder():
    """Lazy-load the timezone finder."""
    global _timezone_finder
    if _timezone_finder is None:
        _timezone_finder = TimezoneFinder()
    return _timezone_finder




"""def load_tz_cache(cache_path: str) -> dict:
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not load timezone cache: {e}")
    return {}



def save_tz_cache(cache: dict, cache_path: str):
    if not cache_path:
        return
    try:
        with open(cache_path, 'w') as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save timezone cache: {e}")"""






def infer_tz_from_location(postcode, country, cache: dict = None) -> float:
    """
    Infer UTC offset from postcode and country using geocoding.
    Checks cache first if provided.
    Returns the UTC offset in hours (float), or None if inference fails.
    """
    # Handle None, NA, empty strings
    def is_empty(val):
        if val is None: return True
        try:
            if pd.isna(val): return True
        except: pass
        if isinstance(val, str) and val.strip() == '': return True
        return False
    
    if is_empty(postcode) and is_empty(country):
        return None

    # Normalise keys for cache
    pc_str = str(postcode).strip() if not is_empty(postcode) else ""
    co_str = str(country).strip() if not is_empty(country) else ""
    
    # Heuristic: If country is missing but postcode is 4 digits, assume Australia
    if not co_str and len(pc_str) == 4 and pc_str.isdigit():
        co_str = "Australia"

    cache_key = f"{pc_str}|{co_str}"
    
    # Check cache
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    try:
        # Build query
        query_parts = []
        if pc_str: query_parts.append(pc_str)
        if co_str: query_parts.append(co_str)
        
        if not query_parts:
            return None
            
        query = ", ".join(query_parts)
        
        
        # Geocode
        geocoder = _get_geocoder()
        location = geocoder.geocode(query, timeout=5)
        
        # Respect Nominatim Rate Limit (1 req/sec)
        time.sleep(1.0)
        
        if location is None:
            # Cache failure as None? Or don't cache?
            # Caching None avoids repeated failed API calls for bad data.
            if cache is not None:
                cache[cache_key] = None
            return None
        
        # Get timezone
        tf = _get_timezone_finder()
        tz_name = tf.timezone_at(lat=location.latitude, lng=location.longitude)
        
        if tz_name is None:
            if cache is not None:
                cache[cache_key] = None
            return None
        
        # Get offset
        tz = pytz.timezone(tz_name)
        now = datetime.now(tz)
        offset_seconds = now.utcoffset().total_seconds()
        offset_hours = int(offset_seconds / 3600)
        
        # Update cache
        if cache is not None:
            cache[cache_key] = offset_hours
            
        return offset_hours
        
    except Exception as e:
        print(f"Error inferring timezone for {cache_key}: {e}")
        return None





def enrich_stats_with_metadata(cf, stats_df: pd.DataFrame, metadata_df: pd.DataFrame, cache_filename: str = None) -> pd.DataFrame:
    """
    Merges metadata into stats_df and adds checking location-based timezone.
    """
    import fyp.data_io as data_io

    if stats_df.empty:
        return stats_df

    # Load cache
    #tz_cache = load_tz_cache(cache_path) if cache_filename else {}
    tz_cache = data_io.load_json(cf, "ddp_main", cache_filename, verbose=False) if cache_filename else {}
    initial_cache_size = len(tz_cache)
    
    # Merge Logic (taken from app.py)
    # Handle columns that might be lists
    list_columns = ['age', 'email', 'name', 'tiktokHandle', 'country', 'postCode']
    for col in list_columns:
        if col in metadata_df.columns:
            metadata_df[col] = metadata_df[col].apply(
                lambda x: ', '.join(str(v) for v in x) if hasattr(x, '__iter__') and not isinstance(x, str) else x
            )

    if 'donation_id' in metadata_df.columns:
        cols_to_merge = ['donation_id'] + [c for c in ['email', 'name', 'date', 'age', 'tiktokHandle', 'country', 'postCode'] if c in metadata_df.columns]
        # Drop duplicates in metadata just in case
        meta_subset = metadata_df[cols_to_merge].drop_duplicates('donation_id')
        
        stats_df = stats_df.merge(meta_subset, on='donation_id', how='left')
        
        print("Inferring timezone from location data...")
        
        def safe_get(row, col):
            val = row.get(col)
            # Basic cleaning
            if val is None or pd.isna(val): return None
            return str(val)

        # Apply inference
        offsets = []
        for idx, row in stats_df.iterrows():
            off = infer_tz_from_location(safe_get(row, 'postCode'), safe_get(row, 'country'), cache=tz_cache)
            offsets.append(off)
            
        stats_df['location_tz_offset'] = offsets
        
        print(f"Location timezone inferred for {stats_df['location_tz_offset'].notna().sum()} donations")
        
    # Save cache if changed
    if cache_filename and len(tz_cache) > initial_cache_size:
        data_io.save_json(cf, "ddp_main", cache_filename, tz_cache)
        print(f"Updated timezone cache saved to {cache_filename} (entries: {len(tz_cache)})")
        
    return stats_df
