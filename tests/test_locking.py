import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import threading
import time
import pandas as pd

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from web_interface.data_service import get_explorer_data, study_cache

class TestDataServiceLocking(unittest.TestCase):
    def setUp(self):
        # Clear cache before test
        study_cache.cache.clear()
        
    @patch('web_interface.data_service.explorer.load_data')
    def test_concurrent_load_locking(self, mock_load_data):
        """Test that concurrent requests result in only ONE load_data call"""
        
        # Mock load_data to take some time
        def side_effect(*args, **kwargs):
            time.sleep(0.1) # Simulate IO
            return pd.DataFrame({'A': [1]}), {'A': 'number'}
            
        mock_load_data.side_effect = side_effect
        
        # Threads to simulate concurrent requests
        threads = []
        n_threads = 5
        
        def task():
            df, _ = get_explorer_data("study_X")
            self.assertIsNotNone(df)

        for _ in range(n_threads):
            t = threading.Thread(target=task)
            threads.append(t)
            t.start()
            
        for t in threads:
            t.join()
            
        # VERIFICATION:
        # load_data should be called EXACTLY ONCE despite 5 threads
        self.assertEqual(mock_load_data.call_count, 1)
        print(f"Data Loaded {mock_load_data.call_count} times (Expected: 1)")

if __name__ == '__main__':
    unittest.main()
