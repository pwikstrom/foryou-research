
import pandas as pd


def analyze_activity_peak(df: pd.DataFrame, period_hours: int = 1) -> dict:
    """
    Analyzes activity peaks in a dataframe with MultiIndex (date, hour) or just hour column.
    
    Args:
        df: DataFrame with MultiIndex (date, hour) OR a 'hour' column. Must have 'event_count' column.
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
    
    # Check if 'hour' is available or in index
    if 'hour' not in df_proc.columns:
        if isinstance(df_proc.index, pd.MultiIndex) and 'hour' in df_proc.index.names:
            df_proc['hour'] = df_proc.index.get_level_values('hour')
        elif 'hour' in df_proc.index.names: # Single index named hour
             df_proc['hour'] = df_proc.index
        else:
             # Try to extract from datetime index if possible
             if isinstance(df_proc.index, pd.DatetimeIndex):
                 df_proc['hour'] = df_proc.index.hour
             else:
                 # If we can't find hour, we might be passed a pre-aggregated DF by hour?
                 # If rows are just hours 0-23?
                 pass

    # Simplified logic: If index is DatetimeIndex, we can do rolling.
    # If it is already aggregated by hour (0-23) and we just want to find peak of the profile? 
    # But for 'consistency', we need multiple data points per hour (e.g. across days).
    
    # If input is raw hourly counts over time (DatetimeIndex):
    if isinstance(df_proc.index, pd.DatetimeIndex):
        # Rolling logic
        if period_hours > 1:
            df_proc['event_count'] = df_proc['event_count'].rolling(window=period_hours, min_periods=1).sum()
            df_proc['event_count'] = df_proc['event_count'].shift(-(period_hours - 1))
            df_proc = df_proc.dropna()
            
        # Group by hour to get stats (mean across days)
        df_proc['hour'] = df_proc.index.hour
        hourly_stats = df_proc.groupby('hour')['event_count'].agg(['mean', 'std'])
        
    elif 'hour' in df_proc.columns and not isinstance(df_proc.index, pd.DatetimeIndex):
        # We assume it's MultiIndex (date, hour) flattened?
        # Or if passed raw 0-23 profile?
        # The previous implementation assumed MultiIndex (date, hour).
        # Let's support the one I wrote for `calc_collection_stats` which passes:
        # result of `df.set_index('local_date').resample('h').size()` -> DatetimeIndex.
        # So the above block covers it.
        pass
    else:
        # Fallback for MultiIndex
        try:
           # Attempt to convert to DatetimeIndex as before
           dates = df_proc.index.get_level_values(0)
           hours = df_proc.index.get_level_values(1)
           timestamps = pd.to_datetime(dates) + pd.to_timedelta(hours, unit='h')
           df_proc.index = timestamps
           df_proc = df_proc.sort_index()
           
           # Recursive call with fixed index
           return analyze_activity_peak(df_proc, period_hours)
        except:
           raise ValueError("Input format not supported. Expect DatetimeIndex or (date, hour) MultiIndex.")


    # Calculate Stats
    # hourly_stats should be ready from DatetimeIndex block
    if 'hourly_stats' not in locals():
         # Safety fallback
         raise ValueError("Could not calculate hourly stats")

    # Fill missing hours with 0 if needed (reindex)
    hourly_stats = hourly_stats.reindex(range(24), fill_value=0)
    
    # Find Peak
    peak_hour = hourly_stats['mean'].idxmax()
    peak_stats = hourly_stats.loc[peak_hour]
    
    stats = {
        'peak_starting_hour': int(peak_hour),
        'period_hours': period_hours,
        'mean_activity_count': float(peak_stats['mean']),
        'consistency_std': float(peak_stats['std']),
        'consistency_cv': float(peak_stats['std'] / peak_stats['mean']) if peak_stats['mean'] > 0 else 0.0,
        'hourly_profile': hourly_stats
    }
    
    return stats
