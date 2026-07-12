
import pandas as pd
import numpy as np

def analyze_activity_peak(df: pd.DataFrame, period_hours: int = 1) -> dict:
    """
    Analyzes activity peaks in a dataframe with MultiIndex (date, hour).
    
    Args:
        df: DataFrame with MultiIndex (date, hour) and single column 'event_count'
        period_hours: Window size in hours to aggregate activity (default 1)
        
    Returns:
        Dictionary containing:
        - peak_hour: The hour (0-23) with highest average activity (start of window if period > 1)
        - peak_mean: The average event count for that hour
        - consistency_std: Standard deviation of event count for that hour (lower = more consistent)
        - consistency_cv: Coefficient of variation (std/mean) for that hour
    """
    # Validate input
    if 'event_count' not in df.columns:
        raise ValueError("DataFrame must have 'event_count' column")
        
    # Work on a copy to avoid modifying original
    df_proc = df.copy()
    
    # 1. Handle Preprocessing & Rolling Window
    # Convert MultiIndex to DatetimeIndex to handle time gaps/rolling correctly
    try:
        # Create timestamp from date and hour levels
        # Assuming level 0 is date, level 1 is hour
        dates = df_proc.index.get_level_values(0)
        hours = df_proc.index.get_level_values(1)
        
        # Combine into datetime series
        timestamps = pd.to_datetime(dates) + pd.to_timedelta(hours, unit='h')
        df_proc.index = timestamps
        
        # Sort index
        df_proc = df_proc.sort_index()
        
        # Reindex to full hourly range to handle missing hours (0 events)
        full_idx = pd.date_range(start=df_proc.index.min(), end=df_proc.index.max(), freq='h')
        df_proc = df_proc.reindex(full_idx, fill_value=0)
        
    except Exception as e:
        raise ValueError(f"Could not convert index to DatetimeIndex: {e}")

    # Apply Rolling Window if needed
    if period_hours > 1:
        # Rolling sum, backward looking by default. 
        # So value at 14:00 with window=2 is sum(13:00, 14:00).
        # We might want to attribute this to the start hour?
        # Let's align it to the start of the window for 'peak hour' meaning.
        # shifting backward by period_hours - 1 would align the rolling sum window [t, t+k] to t.
        # But standard rolling is [t-k+1, t].
        # If we use rolling(window=3).sum(), the value at 12:00 is sum(10,11,12).
        # The user likely wants to know "starting at 10am, activity is high".
        # So we can calculate rolling sum, then shift it back?
        # Let's just stick to standard rolling and denote it's ending at that hour, 
        # or shift it. Shifting back makes more sense for "Period starting at X".
        # Let's assume input is "activity during hour H". 
        # If period=2, we want activity at H and H+1. 
        # Rolling sum at H+1 would capture H and H+1. 
        # So we take rolling sum and shift it backwards by (period_hours - 1).
        
        df_proc['event_count'] = df_proc['event_count'].rolling(window=period_hours, min_periods=1).sum()
        
        # If we want the label to be the START of the period:
        # Example: Period=2. 
        # orig: 10:00 (5), 11:00 (10).
        # rolling(2) at 11:00 is 15.
        # We want to say "Starting at 10:00, count is 15".
        # So we shift -1.
        df_proc['event_count'] = df_proc['event_count'].shift(-(period_hours - 1))
        
        # Initial rolling will produce NaNs at the end after shifting?
        # Rolling at Start produces NaNs at start. Shift neg produces NaNs at end.
        # We should drop NaNs created by this process strictly speaking if we want full windows.
        df_proc = df_proc.dropna()

    # 2. Group by Hour
    # Extract hour from index
    df_proc['hour'] = df_proc.index.hour
    
    # Calculate stats per hour
    hourly_stats = df_proc.groupby('hour')['event_count'].agg(['mean', 'std'])
    
    # 3. Find Peak
    peak_hour = hourly_stats['mean'].idxmax()
    peak_stats = hourly_stats.loc[peak_hour]
    
    stats = {
        'peak_starting_hour': int(peak_hour),
        'period_hours': period_hours,
        'mean_activity_count': float(peak_stats['mean']),
        'consistency_std': float(peak_stats['std']),
        'consistency_cv': float(peak_stats['std'] / peak_stats['mean']) if peak_stats['mean'] > 0 else 0.0,
        'hourly_profile': hourly_stats  # Returning the whole profile might be useful
    }
    
    return stats
