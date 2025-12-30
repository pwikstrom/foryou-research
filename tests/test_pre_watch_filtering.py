import unittest
import pandas as pd
import sys
import os
from datetime import datetime, timedelta

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from fyp.calc_donation_stats import process_single_donation

class TestPreWatchFiltering(unittest.TestCase):
    def test_filtering(self):
        print("\n--- Testing Pre-Watch Filtering ---")
        
        # Create a timeline of events
        # T0: Comment (Should be dropped)
        # T1: Watch (First Watch - Keep)
        # T2: Like (Keep)
        
        base_time = datetime(2024, 1, 1, 10, 0, 0)
        
        events = [
            {'date': base_time, 'feature_name': 'comment', 'donation_id': 'd1', 'primary_value': 'c1'},
            {'date': base_time + timedelta(minutes=5), 'feature_name': 'watch', 'secondary_value': 100, 'donation_id': 'd1', 'primary_value': None},
            {'date': base_time + timedelta(minutes=10), 'feature_name': 'like', 'donation_id': 'd1', 'primary_value': None}
        ]
        
        df = pd.DataFrame(events)
        
        stats = process_single_donation(df)
        
        # We expect 2 events total (Watch + Like)
        self.assertEqual(stats['total_events'], 2, "Should have 2 events (dropped the first comment)")
        self.assertEqual(stats['num_watches'], 1)
        self.assertEqual(stats['num_likes'], 1)
        self.assertEqual(stats['num_comments'], 0, "Comment before watch should be gone")
        
    def test_no_watch_events(self):
        print("\n--- Testing No Watch Events ---")
        # Only comments/likes, no watch
        # Should return empty dict
        events = [
            {'date': datetime.now(), 'feature_name': 'comment', 'donation_id': 'd2', 'primary_value': 'c2'},
            {'date': datetime.now(), 'feature_name': 'like', 'donation_id': 'd2', 'primary_value': None}
        ]
        df = pd.DataFrame(events)
        stats = process_single_donation(df)
        self.assertEqual(stats, {}, "Should return empty dict if no watch events")

if __name__ == '__main__':
    unittest.main()
