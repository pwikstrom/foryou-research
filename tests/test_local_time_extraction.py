import unittest
import pandas as pd
import sys
import os
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo
import tempfile

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from fyp.organize_datasets import extract_local_time_features

class TestLocalTimeExtraction(unittest.TestCase):
    def setUp(self):
        # Create temp directory for mock DDP main
        self.temp_dir = tempfile.mkdtemp()
        self.stats_cache_path = os.path.join(self.temp_dir, 'persona_stats_cache.parquet')
        
        # Mock Config
        self.mock_cf = {
            'paths': {
                'ddp_main': self.temp_dir
            },
            'study_defs': {
                'test_study': {
                    'TIME_ZONE': 'Australia/Brisbane' # UTC+10
                }
            }
        }
        
        # Create Dummy Stats Cache
        # d1: UTC+2 (e.g. Europe/Paris summer)
        # d2: UTC-5 (e.g. US East)
        stats_data = {
            'donation_id': ['d1', 'd2'],
            'inferred_tz_offset': [2.0, -5.0]
        }
        pd.DataFrame(stats_data).to_parquet(self.stats_cache_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_ddp_offsets(self):
        print("\n--- Testing DDP Per-Donation Offsets ---")
        
        # Create events
        # Base UTC time: 2024-01-01 12:00:00 UTC
        # timestamp = 1704110400
        utc_ts = 1704110400 # 12:00:00 UTC
        
        # d1 (UTC+2): Should be 14:00:00
        # d2 (UTC-5): Should be 07:00:00
        # d3 (Missing -> Default Bris UTC+10): Should be 22:00:00
        
        events_data = {
            'donation_id': ['d1', 'd2', 'd3'],
            'item_id': ['i1', 'i2', 'i3'],
            'utc_timestamp': [utc_ts, utc_ts, utc_ts],
            'primary_label': ['link', 'link', 'link'],
            'feature_name': ['watch', 'watch', 'watch'],
            'primary_value': ['v1', 'v2', 'v3']
        }
        df = pd.DataFrame(events_data)
        
        # Run extraction
        res_df = extract_local_time_features(
            cf=self.mock_cf,
            some_events_df_in=df,
            kind_of_log='ddp',
            verbose=True
        )
        
        # Check Local Hours
        # d1: 14
        self.assertEqual(res_df.loc[0, 'local_hour'], 14, "d1 should be 14:00 (UTC+2)")
        # d2: 7
        self.assertEqual(res_df.loc[1, 'local_hour'], 7, "d2 should be 07:00 (UTC-5)")
        # d3: 22 (Fall back to Brisbane UTC+10)
        self.assertEqual(res_df.loc[2, 'local_hour'], 22, "d3 should be 22:00 (Default UTC+10)")
        
        # Check Naive Naive (Wall Clock)
        # Should not have timezone info
        self.assertIsNone(res_df['local_timestamp'].dt.tz, "local_timestamp should be tz-naive")
        
        # Check specific values
        self.assertEqual(res_df.loc[0, 'local_timestamp'], datetime(2024, 1, 1, 14, 0, 0))

    def test_baseline_stripping(self):
        print("\n--- Testing Baseline Tz Stripping ---")
        
        # Baseline uses 'source_url.tz_name'
        # Create events with naive timestamp (as expected by function)
        ts = pd.Timestamp("2024-01-01 10:00:00") # Naive
        
        events_data = {
            'timestamp_collected': [ts],
            'source_url.tz_name': ['Europe/Paris']
        }
        df = pd.DataFrame(events_data)
        
        res_df = extract_local_time_features(
            cf=self.mock_cf,
            some_events_df_in=df,
            kind_of_log='baseline',
            verbose=True
        )
        
        # Should retain 10:00 but match naive format
        self.assertIsNone(res_df['local_timestamp'].dt.tz, "Baseline should strip timezone")
        self.assertEqual(res_df.loc[0, 'local_hour'], 10)
        self.assertEqual(res_df.loc[0, 'local_timestamp'], datetime(2024, 1, 1, 10, 0, 0))

if __name__ == '__main__':
    unittest.main()
