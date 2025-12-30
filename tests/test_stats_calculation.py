
import sys
import os
sys.path.append(os.getcwd())

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from fyp.calc_donation_stats import infer_timezone_offset, process_single_donation

def create_synthetic_events(offset_hours=0):
    """
    Creates synthetic events for a user with a specific timezone pattern.
    Default user sleeps 00:00-08:00 Local.
    """
    dates = []
    features = []
    values = []
    sec_values = []
    
    base_time = pd.Timestamp("2025-01-01 00:00:00", tz='UTC')
    
    # Generate 5 days of data
    for day in range(5):
        day_start = base_time + timedelta(days=day)
        
        # Morning session (08:00 Local -> 08-Offset UTC)
        # Let's say offset is +10 (Brisbane). 
        # 08:00 Local = 22:00 UTC (prev day).
        # We construct in Local time then convert to UTC
        
        # 08:00 - 09:00 Local: Watch videos
        for m in range(0, 60, 5): # Every 5 mins
            local_ts = day_start + timedelta(hours=8, minutes=m)
            utc_ts = local_ts - timedelta(hours=offset_hours)
            dates.append(utc_ts)
            features.append('watch')
            values.append('video_link')
            sec_values.append(60) # 60s watch
            
        # Evening session (20:00 - 22:00 Local)
        # 20:00 Local = 10:00 UTC (if +10)
        for m in range(0, 120, 10): # Every 10 mins
            local_ts = day_start + timedelta(hours=20, minutes=m)
            utc_ts = local_ts - timedelta(hours=offset_hours)
            dates.append(utc_ts)
            
            # Mix of watch and comment
            if m % 20 == 0:
                features.append('comment')
                values.append('Love this! 🔥')
                sec_values.append(None)
            else:
                features.append('watch')
                values.append('video_link')
                sec_values.append(30)
                
            # Add a 'like' every 30 mins
            if m % 30 == 0:
                local_ts = day_start + timedelta(hours=20, minutes=m)
                utc_ts = local_ts - timedelta(hours=offset_hours)
                dates.append(utc_ts)
                features.append('like')
                values.append(None)
                sec_values.append(None)
        
        # Add random scatter noise during the day (10:00 - 18:00 Local)
        # To ensure the "gap" is clearly at night (22:00 - 08:00)
        num_noise = 20
        for _ in range(num_noise):
            h = np.random.randint(10, 18)
            m = np.random.randint(0, 60)
            local_ts = day_start + timedelta(hours=h, minutes=m)
            utc_ts = local_ts - timedelta(hours=offset_hours)
            dates.append(utc_ts)
            features.append('watch')
            values.append('noise_vid')
            sec_values.append(10)
            
    # Sort by date
    df = pd.DataFrame({
        'donation_id': 'test_user_1',
        'date': dates,
        'feature_name': features,
        'primary_value': values,
        'secondary_value': sec_values
    })
    df = df.sort_values('date').reset_index(drop=True)
    
    return df

def test_timezone_inference():
    print("--- Testing Timezone Inference ---")
    
    # Case 1: UTC User (Sleeps 00-08 UTC)
    # Most active 08-22 UTC. Quietest 00-04 UTC.
    # infer_timezone_offset should find window around 04:00 UTC is minimum.
    # Center 04:00 UTC. Offset = 04:00 - 04:00 = 0.
    df_utc = create_synthetic_events(offset_hours=0)
    offset_utc = infer_timezone_offset(df_utc['date'])
    print(f"Expected 0.0, Got: {offset_utc}")
    
    # Case 2: Brisbane User (UTC+10)
    # Sleeps 00-08 Local -> 14:00-22:00 UTC.
    # Quietest window 16:00-20:00 UTC approx?
    # Center ~18:00 UTC.
    # Offset = 04:00 - 18:00 = -14 = +10.
    df_bne = create_synthetic_events(offset_hours=10)
    offset_bne = infer_timezone_offset(df_bne['date'])
    print(f"Expected 10.0, Got: {offset_bne}")
    
    # Case 3: NY User (UTC-5)
    # Sleeps 00-08 Local -> 05:00-13:00 UTC.
    # Quietest window ~09:00 UTC.
    # Offset = 04:00 - 09:00 = -5.
    df_ny = create_synthetic_events(offset_hours=-5)
    offset_ny = infer_timezone_offset(df_ny['date'])
    print(f"Expected -5.0, Got: {offset_ny}")


def test_stats_calc():
    print("\n--- Testing Stats Calculation ---")
    df = create_synthetic_events(offset_hours=10) # Brisbane user
    
    # Run stats
    stats = process_single_donation(df)
    
    print(f"Inferred TZ: {stats['inferred_tz_offset']}")
    print(f"Top Emoji (should be 🔥): {stats['top_emoji']}")
    print(f"Num Sessions (should be approx 2 per day * 5 days = 10): {stats['num_sessions']}")
    
    # Evening session was 20:00-22:00 Local. That's 'Evening' (18-23).
    # Morning session was 08:00-09:00 Local. That's 'Morning' (5-11).
    print(f"Share Morning: {stats['share_morning']:.2f}")
    print(f"Share Evening: {stats['share_evening']:.2f}")
    
    # Peak activity
    print(f"Peak Activity Hour Local (should be ~8): {stats['peak_activity_hour_local']}")
    
    print(f"Moniker: {stats['moniker']}")
    print(f"Chattiness: {stats['chattiness']:.2f}")
    print(f"Enthusiasm: {stats['enthusiasm']:.2f}")
    
    print(f"Session Velocity (VPM): {stats['session_velocity_vpm']:.2f}")
    print(f"Weekend Bias: {stats['weekend_bias']:.2f}")
    print(f"Avg Comment Len: {stats['avg_comment_len_chars']:.1f}")
    print(f"Activity Trend Slope: {stats['activity_trend_slope']:.2f}")
    print(f"Return Probability: {stats['day_to_day_return_prob']:.2f}")


if __name__ == "__main__":
    test_timezone_inference()
    test_stats_calc()
