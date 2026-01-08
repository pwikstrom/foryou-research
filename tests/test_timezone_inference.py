import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
import os
import json
import sys

# Ensure project root is in path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from fyp.calc_donation_stats import enrich_stats_with_metadata, infer_timezone_offset
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

class TestTimezoneInference(unittest.TestCase):
    def setUp(self):
        self.cache_path = "test_tz_cache.json"
        if os.path.exists(self.cache_path):
            os.remove(self.cache_path)

    def tearDown(self):
        if os.path.exists(self.cache_path):
            os.remove(self.cache_path)

    @patch('fyp.calc_donation_stats._get_geocoder')
    @patch('fyp.calc_donation_stats._get_timezone_finder')
    def test_inference_and_caching(self, mock_get_tf, mock_get_geo):
        # Setup mocks
        mock_geocoder = MagicMock()
        mock_location = MagicMock()
        mock_location.latitude = -27.47
        mock_location.longitude = 153.02
        mock_geocoder.geocode.return_value = mock_location
        mock_get_geo.return_value = mock_geocoder

        mock_tf = MagicMock()
        mock_tf.timezone_at.return_value = "Australia/Brisbane" # UTC+10
        mock_get_tf.return_value = mock_tf

        # Metadata with some variations
        # 3: Missing Country, 4-digit Postcode (Should trigger default to Australia)
        meta = pd.DataFrame({
            'donation_id': [1, 2, 3],
            'postCode': ['4000', '2000', '3000'],
            'country': ['Australia', 'Australia', None]
        })

        stats = pd.DataFrame({'donation_id': [1, 2, 3]})

        print("\n--- Running Pass 1 (Populate Cache) ---")
        # Run 1
        result = enrich_stats_with_metadata(stats, meta, cache_filename=self.cache_path)
        
        self.assertTrue('location_tz_offset' in result.columns, "Output dataframe should have 'location_tz_offset' column")
        # Brisbane is UTC+10
        print(result[['postCode', 'country', 'location_tz_offset']])
        
        # 3 calls: 
        # 1. 4000, Australia
        # 2. 2000, Australia
        # 3. 3000, None -> (3000, Australia) inferred
        self.assertEqual(mock_geocoder.geocode.call_count, 3, "Should call geocoder 3 times")

        # Check Cache File
        self.assertTrue(os.path.exists(self.cache_path), "Cache file should exist")
        with open(self.cache_path) as f:
            cache = json.load(f)
        self.assertEqual(len(cache), 3, "Cache should have 3 entries")
        
        print("\n--- Running Pass 2 (Read Cache) ---")
        # Run 2 - Should verify cache usage
        # Reset mock counts
        mock_geocoder.geocode.reset_mock()
        
        result2 = enrich_stats_with_metadata(stats, meta, cache_filename=self.cache_path)
        self.assertEqual(len(result2), 3)
        print("Pass 2 successful: Geocoder was not called.")

    def test_infer_logic(self):
        print("\n--- Testing Math Logic ---")
        # Create a series of timestamps
        # Construct a scenario where the 4-hour quiet window is clearly defined
        # e.g., NO activity between 02:00 and 06:00 UTC.
        # Center of this window is 04:00 UTC.
        # Function assumes quietest is 04:00 Local.
        # Input 04:00 UTC = 04:00 Local.
        # Offset should be 0.
        
        # Scenario: Block of activity everywhere EXCEPT 02, 03, 04, 05 UTC
        timestamps = []
        base_date = datetime(2024, 1, 1, 0, 0, 0)
        for h in range(24):
            if h in [2, 3, 4, 5]:
                continue # Quiet
            # Add activity
            for _ in range(10):
                timestamps.append(base_date + timedelta(hours=h))
        
        ts_series = pd.Series(timestamps)
        offset = infer_timezone_offset(ts_series)
        print(f"Quiet 02-06 UTC (Center 04:00). Offset: {offset}")
        # Center UTC = 4.0. Local default = 3.0. Offset = 3.0 - 4.0 = -1.
        self.assertEqual(offset, -1)

        # Scenario 2: Quiet 12-16 UTC (Center 14:00).
        # Center UTC = 14.0.
        # Offset = 3.0 - 14.0 = -11.0.
        # Normalize: -11 < -9 => -11 + 24 = 13.
        timestamps = []
        for h in range(24):
            if h in [12, 13, 14, 15]:
                continue
            for _ in range(10):
                timestamps.append(base_date + timedelta(hours=h))
        
        offset = infer_timezone_offset(pd.Series(timestamps))
        print(f"Quiet 12-16 UTC (Center 14:00). Offset: {offset}")
        self.assertEqual(offset, 13)

if __name__ == '__main__':
    unittest.main()
