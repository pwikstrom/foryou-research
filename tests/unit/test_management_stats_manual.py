"""Unit tests for the study stats calculation (`_calculate_stats`).

The function loads ``enrichment_status.parquet``, builds the study's recoded
dataset, and returns ``(stats, df_recoded, df_status)`` — the same triple the
``run_study_refresh`` worker persists into ``studies.json``.
"""

import unittest
from unittest.mock import patch

import pandas as pd

from web_interface.services.stats_service import _calculate_stats

STUDY_CONFIG = {"STUDY_NAME": "test_study", "SELECTED_COLLECTIONS": ["d1", "d2", "d3"]}


class TestStudyStats(unittest.TestCase):

    @patch('web_interface.services.stats_service.create_study_recoded_dataset')
    @patch('web_interface.services.stats_service.data_io.load_parquet')
    @patch('web_interface.services.stats_service.data_io.exists')
    def test_calculate_stats(self, mock_exists, mock_load_parquet, mock_create_study):
        # 1. Mock Study Dataset (output of create_study_recoded_dataset).
        # 5 activities over 5 videos in 3 collections. No local_timestamp, so
        # the event-window/play-observe filters are skipped and every row counts.
        df_study = pd.DataFrame({
            'item_id': [1, 2, 3, 4, 5],
            'collection_id': ['d1', 'd1', 'd2', 'd2', 'd3'],
        })
        mock_create_study.return_value = df_study

        # 2. Mock Enrichment Status, indexed by item_id as the real parquet is:
        # - not scraped, no failure (items 1, 2)
        # - scrape failed          (item 3)
        # - scraped, not annotated (item 4)
        # - scraped and annotated  (item 5)
        df_status = pd.DataFrame({
            'scraped_ok':   [False, False, False, True, True],
            'scraped_fail': [False, False, True,  False, False],
            'annotated_ok': [False, False, False, False, True],
        }, index=pd.Index([1, 2, 3, 4, 5], name='item_id'))

        mock_exists.return_value = True
        mock_load_parquet.return_value = df_status

        # 3. Call Function
        stats, df_recoded, status_out = _calculate_stats(STUDY_CONFIG, save_to_cache=False)

        # 4. Assertions — the pass-throughs the refresh worker reuses.
        self.assertIs(df_recoded, df_study)
        self.assertIs(status_out, df_status)

        self.assertEqual(stats['total_activities'], 5)
        self.assertEqual(stats['unique_videos'], 5)
        self.assertEqual(stats['unique_collections'], 3)
        # No local_timestamp column in the study frame.
        self.assertEqual(stats['active_days'], 0)

        # scraped_videos: sum(scraped_ok) = 2 (items 4, 5)
        self.assertEqual(stats['scraped_videos'], 2)

        # annotated_videos: sum(annotated_ok) = 1 (item 5)
        self.assertEqual(stats['annotated_videos'], 1)

        # Activity-level counts: one activity per video here, so they match.
        self.assertEqual(stats['activities_scraped'], 2)
        self.assertEqual(stats['activities_annotated'], 1)

    @patch('web_interface.services.stats_service.create_study_recoded_dataset')
    @patch('web_interface.services.stats_service.data_io.exists')
    def test_calculate_stats_without_enrichment_status(self, mock_exists, mock_create_study):
        """No enrichment_status.parquet — counts still work, enrichment is zero."""
        df_study = pd.DataFrame({
            'item_id': [1, 2, 3],
            'collection_id': ['d1', 'd1', 'd2'],
        })
        mock_create_study.return_value = df_study
        mock_exists.return_value = False

        stats, _, status_out = _calculate_stats(STUDY_CONFIG, save_to_cache=False)

        self.assertIsNone(status_out)
        self.assertEqual(stats['unique_videos'], 3)
        self.assertEqual(stats['unique_collections'], 2)
        self.assertEqual(stats['scraped_videos'], 0)
        self.assertEqual(stats['annotated_videos'], 0)

    def test_calculate_stats_no_selected_collections(self):
        """An empty study returns zeroed stats before any expensive work."""
        stats, df_recoded, status_out = _calculate_stats(
            {"STUDY_NAME": "test_study", "SELECTED_COLLECTIONS": []})

        self.assertIsNone(df_recoded)
        self.assertIsNone(status_out)
        self.assertEqual(stats['total_activities'], 0)
        self.assertEqual(stats['unique_videos'], 0)
        self.assertEqual(stats['unique_collections'], 0)


if __name__ == '__main__':
    unittest.main()
