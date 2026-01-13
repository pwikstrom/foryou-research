import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import pandas as pd
import json

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from web_interface.fyp_data_hub import app

class TestExplorerFilterOptimization(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        app.config['LOGIN_DISABLED'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        self.client = app.test_client()
        
        # Dummy Data
        self.dummy_df = pd.DataFrame({
            'A': [1, 2, 3], 
            'B': ['x', 'y', 'z'],
            'annotated_ok': [True, True, True]
        })
        self.dummy_col_types = {'A': 'number', 'B': 'category'}
        self.dummy_stats = {'mean': 10, 'count': 100}

    @patch('web_interface.routes.data_routes.get_explorer_data')
    @patch('web_interface.routes.data_routes.explorer.filter_dataframe')
    @patch('web_interface.routes.data_routes.explorer.get_current_stats')
    @patch('web_interface.routes.data_routes.data_io.exists')
    @patch('web_interface.routes.data_routes.data_io.load_json')
    def test_filter_identical_slices_optimization(self, mock_load_json, mock_exists, mock_get_stats, mock_filter_df, mock_get_data):
        """Test that identical filters reuse S1 stats for S2"""
        mock_get_data.return_value = (self.dummy_df, self.dummy_col_types)
        mock_filter_df.return_value = self.dummy_df
        mock_get_stats.return_value = {'stats': self.dummy_stats, 'count': 100}
        
        # Mock Meta Cache MISS
        mock_exists.return_value = False 

        # Payload with IDENTICAL filters
        payload = {
            "study": "test_study",
            "filters": {"A": 1},
            "filters2": {"A": 1}, # Identical
            "search_query": "foo",
            "search_query2": "foo", # Identical
            "trigger_slice": None # Trigger Both
        }
        
        resp = self.client.post('/api/explorer/filter', json=payload)
        data = resp.get_json()
        
        self.assertEqual(resp.status_code, 200)
        self.assertIn('stats', data)
        self.assertIn('stats2', data)
        self.assertEqual(data['stats'], data['stats2'])
        
        # VERIFICATION:
        self.assertEqual(mock_filter_df.call_count, 1)
        self.assertEqual(mock_get_stats.call_count, 1)

    @patch('web_interface.routes.data_routes.get_explorer_data')
    @patch('web_interface.routes.data_routes.explorer.filter_dataframe')
    @patch('web_interface.routes.data_routes.explorer.get_current_stats')
    @patch('web_interface.routes.data_routes.data_io.exists')
    @patch('web_interface.routes.data_routes.data_io.load_json')
    def test_filter_metadata_cache_optimization(self, mock_load_json, mock_exists, mock_get_stats, mock_filter_df, mock_get_data):
        """Test that empty filters use cached metadata stats"""
        mock_get_data.return_value = (self.dummy_df, self.dummy_col_types)
        
        # Mock Meta Cache HIT
        mock_exists.return_value = True
        mock_load_json.return_value = {"total_stats": {"cached": "yes"}}

        # Payload with EMPTY filters
        payload = {
            "study": "test_study",
            "filters": {},
            "trigger_slice": 1
        }
        
        resp = self.client.post('/api/explorer/filter', json=payload)
        data = resp.get_json()
        
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data['stats'], {"cached": "yes"})
        
        # VERIFICATION:
        self.assertEqual(mock_filter_df.call_count, 0)
        self.assertEqual(mock_get_stats.call_count, 0)

    @patch('web_interface.routes.data_routes.get_explorer_data')
    @patch('web_interface.routes.data_routes.explorer.filter_dataframe')
    @patch('web_interface.routes.data_routes.explorer.get_current_stats')
    @patch('web_interface.routes.data_routes.data_io.exists')
    @patch('web_interface.routes.data_routes.data_io.load_json')
    def test_combined_optimizations_initial_load(self, mock_load_json, mock_exists, mock_get_stats, mock_filter_df, mock_get_data):
        """Test Initial Load: Cache HIT + Identical Reuse"""
        mock_get_data.return_value = (self.dummy_df, self.dummy_col_types)
        
        # Mock Meta Cache HIT
        mock_exists.return_value = True
        mock_load_json.return_value = {"total_stats": {"cached": "yes"}}

        # Initial Load Payload (Empty filters, Both slices)
        payload = {
            "study": "test_study",
            "filters": {},
            "filters2": {},
            "search_query": "",
            "search_query2": "",
            "trigger_slice": None
        }
        
        resp = self.client.post('/api/explorer/filter', json=payload)
        data = resp.get_json()
        
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data['stats'], {"cached": "yes"})
        self.assertEqual(data['stats2'], {"cached": "yes"})
        
        # VERIFICATION:
        # Should be ZERO calculations because:
        #  - S1 used cache
        #  - S2 reused S1 result (identical check)
        self.assertEqual(mock_filter_df.call_count, 0)
        self.assertEqual(mock_get_stats.call_count, 0)

if __name__ == '__main__':
    unittest.main()
