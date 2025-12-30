
import pandas as pd
import numpy as np
import math
from datetime import timedelta
import emoji
from collections import Counter
from fyp.activity_analysis import analyze_activity_peak

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
    # Center of window is k + 1.5.
    # We assume this center is 04:00 Local.
    
    center_utc = avg_idx + 1.5
    if center_utc >= 24:
        center_utc -= 24
        
    # Offset = Local - UTC = 4.0 - Center
    offset = 4.0 - center_utc
    
    # Normalize to -12 to 14
    while offset < -12:
        offset += 24
    while offset > 14:
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
    
    # 2. Infer Timezone
    tz_offset = infer_timezone_offset(df['date'])
    
    # Create Local Time column
    df['local_date'] = df['date'] + pd.Timedelta(hours=tz_offset)
    df['local_hour'] = df['local_date'].dt.hour
    
    # 3. Basic Activity Stats
    total_events = len(df)
    first_date = df['local_date'].min()
    last_date = df['local_date'].max()
    active_days = df['local_date'].dt.date.nunique()
    lifespan_days = (last_date - first_date).days + 1
    events_per_day = total_events / max(1, active_days)
    
    # 4. Video Consumption (Watch Events)
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
    
    # 5. Sessions
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
    
    # 6. Engagement Rates
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
    
    # 7. Advanced Behavioural Metrics (New)
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

    # 8. Time Patterns (Local Time)
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
    
    # 8. Advanced Persona Stats (Expressiveness, etc.)
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
