import sys
import os
from pathlib import Path
import pandas as pd
import numpy as np

# Add project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

import fyp

def analyze_timezones():
    # Init config to get paths
    fyp_cf = fyp.init_project(verbose=False)
    ddp_path = Path(fyp_cf['paths']['ddp_main'])
    cache_path = ddp_path / 'persona_stats_cache.parquet'
    
    if not cache_path.exists():
        print(f"Error: Cache file not found at {cache_path}")
        return

    print(f"Loading stats from {cache_path}...")
    df = pd.read_parquet(cache_path)
    
    # Filter for valid comparisons
    # We need both inferred_tz_offset (activity) and location_tz_offset (geo)
    # inferred_tz_offset is almost always present (default 0.0), but let's check.
    # location_tz_offset might be NaN.
    
    if 'location_tz_offset' not in df.columns:
        print("Error: 'location_tz_offset' not found in dataframe.")
        return

    valid = df.dropna(subset=['inferred_tz_offset', 'location_tz_offset']).copy()
    
    n = len(valid)
    print(f"Found {n} records with both timezones.")
    if n == 0:
        return

    # Hypotheses to test
    # 1. Base (Activity vs Location)
    # 2. Shifted (Activity - 1 vs Location)
    
    diff_base = np.abs(valid['inferred_tz_offset'] - valid['location_tz_offset'])
    mae_base = diff_base.mean()
    mse_base = (diff_base ** 2).mean()
    
    diff_shifted = np.abs((valid['inferred_tz_offset'] - 1) - valid['location_tz_offset'])
    mae_shifted = diff_shifted.mean()
    mse_shifted = (diff_shifted ** 2).mean()
    
    print("\n--- Hypothesis A: Inferred - 1 fits better ---")
    print(f"Base MAE: {mae_base:.4f}")
    print(f"Shifted MAE: {mae_shifted:.4f}")
    print(f"Improvement: {mae_base - mae_shifted:.4f}")
    
    if mae_shifted < mae_base:
        print("CONFIRMED: Shifted (-1) provides a better fit.")
    else:
        print("REJECTED: Base provides a better/equal fit.")

    # Hypothesis B: Date Line / Range issues
    # Look for cases where diff is huge (e.g. > 12 hours)
    # "Add 24 hours to timezones calculated to UTC-11"
    
    print("\n--- Hypothesis B: Date Line / Range Issues ---")
    # Check distribution of location TZ
    print("\nLocation TZ Value Counts:")
    print(valid['location_tz_offset'].value_counts().sort_index())
    
    # Check extreme negatives in Location
    neg_loc = valid[valid['location_tz_offset'] <= -10]
    if not neg_loc.empty:
        print(f"\nFound {len(neg_loc)} locations <= -10:")
        print(neg_loc[['donation_id', 'postCode', 'country', 'location_tz_offset', 'inferred_tz_offset']])
        
    # Check large discrepancies
    # Circular difference: min(|a-b|, 24-|a-b|)
    # But usually we just want to align them.
    
    # Let's see if there are cases where Inferred is +13 and Location is -11 (Total diff 24)
    valid['simple_diff'] = valid['inferred_tz_offset'] - valid['location_tz_offset']
    
    outliers = valid[np.abs(valid['simple_diff']) > 6]
    if not outliers.empty:
        print(f"\nFound {len(outliers)} large discrepancies (> 6 hours):")
        # Print a few
        print(outliers[['postCode', 'country', 'inferred_tz_offset', 'location_tz_offset', 'simple_diff']].head(20))
        
        # Check if adding 24 to negative locations helps?
        # Or subtracting 24 from positive inferred?
        
        # User said: "add 24 hours to timezones that are calculated to UTC-11 or something"
        # This implies modifying the RESULT of the calculation.
        # Let's see if we have valid timezones at -11. Assumedly yes (Samoa?).
        # If the user says "Calculated to -11", do they mean INFERRED activity or LOCATION?
        # "tz inferred from activity ... varies between West coast America ... and New Zealand"
        # "So perhaps UTC-9 or so to UTC+12"
        # "add 24 hours to timezones that are calculated to UTC-11"
        # This implies standardizing on a contiguous range like [-9, +15] instead of [-12, +12].
        
        # Let's simulate remapping [-12, -10] to [+12, +14]
        # range: -11 -> +13, -10 -> +14?
        # Standard Date Line is at +12/-12.
        
        # Let's see what happens if we map anything < -9 to +24
        
        def normalize_tz(x):
            if x < -9: return x + 24
            return x
            
        valid['norm_loc'] = valid['location_tz_offset'].apply(normalize_tz)
        valid['norm_inf'] = valid['inferred_tz_offset'].apply(normalize_tz)
        
        diff_norm = np.abs(valid['norm_inf'] - valid['norm_loc'])
        mae_norm = diff_norm.mean()
        print(f"\nMAE with Normalized (<-9 => +24): {mae_norm:.4f}")
        
        # Combine both adjustments
        valid['shifted_norm_inf'] = valid['inferred_tz_offset'].apply(lambda x: normalize_tz(x-1))
        diff_combined = np.abs(valid['shifted_norm_inf'] - valid['norm_loc'])
        mae_combined = diff_combined.mean()
        print(f"MAE Combined (Shift -1 & Normalize): {mae_combined:.4f}")

if __name__ == "__main__":
    analyze_timezones()
