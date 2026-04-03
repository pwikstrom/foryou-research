
import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
import sys
import os

# Adjust path to include project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Import the function to test
# We need to import management_routes but avoid importing the whole flask app if possible
# or just patch what we need.
from web_interface.routes.management_routes import _calculate_stats

class TestStudyStats(unittest.TestCase):

    @patch('web_interface.routes.management_routes.create_study_recoded_dataset')
    @patch('web_interface.routes.management_routes.data_io.load_parquet')
    def test_calculate_stats(self, mock_load_parquet, mock_create_study):
        # 1. Mock Study Dataset (output of create_study_recoded_dataset)
        # 5 items: 1, 2, 3, 4, 5
        df_study = pd.DataFrame({
            'item_id': [1, 2, 3, 4, 5],
            'collection_id': ['d1', 'd1', 'd2', 'd2', 'd3']
        })
        mock_create_study.return_value = df_study

        # 2. Mock Enrichment Status
        # We need to cover cases:
        # - to_scrape: scraped_ok=False, scraped_fail=False (items 1, 2)
        # - scraped_fail: scraped_ok=False, scraped_fail=True (item 3)
        # - to_annotate: scraped_ok=True, annotated_ok=False (item 4)
        # - fully_done: scraped_ok=True, annotated_ok=True (item 5)
        
        df_status = pd.DataFrame({
            'scraped_ok':   [False, False, False, True, True],
            'scraped_fail': [False, False, True,  False, False], 
            'annotated_ok': [False, False, False, False, True]
        }, index=[1, 2, 3, 4, 5]) # index is item_id
        
        mock_load_parquet.return_value = df_status
        
        # 3. Call Function
        study_config = {"STUDY_NAME": "test_study"}
        stats = _calculate_stats(study_config)
        
        print("Calculated Stats:", stats)
        
        # 4. Assertions
        self.assertEqual(stats['unique_videos'], 5)
        self.assertEqual(stats['unique_donations'], 3)
        
        # scraped_videos: sum(scraped_ok) = 2 (items 4, 5)
        self.assertEqual(stats['scraped_videos'], 2)
        
        # annotated_videos: sum(annotated_ok) = 1 (item 5)
        self.assertEqual(stats['annotated_videos'], 1)
        
        # to_scrape_count: scraped_ok==False & scraped_fail==False -> items 1, 2 -> count 2
        self.assertEqual(stats['to_scrape_count'], 2)
        
        # to_annotate_count: scraped_ok==True & annotated_ok==False -> item 4 -> count 1
        self.assertEqual(stats['to_annotate_count'], 1)

if __name__ == '__main__':
    unittest.main()
