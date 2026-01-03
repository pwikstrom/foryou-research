import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import logging
import io

# Setup path
current_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(current_dir))

import fyp.machine_annotation as ma
# We need to import the monitor script to check its setup_logger, 
# but it runs code on import if not careful (it has a main check but some setup at top level?)
# It has "Current_dir" setup at top level, which is fine.
import web_interface.monitor_scrape_folder_and_annotate as monitor

class TestRefinedSilence(unittest.TestCase):
    def test_logger_levels(self):
        # Run setup_logger
        monitor.setup_logger()
        
        # Check levels
        self.assertEqual(logging.getLogger("google").level, logging.WARNING)
        self.assertEqual(logging.getLogger("urllib3").level, logging.WARNING)
        self.assertEqual(logging.getLogger("grpc").level, logging.WARNING)
        self.assertEqual(logging.getLogger("absl").level, logging.WARNING)
        print("Logger levels verified: WARNING")

    @patch('fyp.machine_annotation._start_monitor')
    @patch('fyp.machine_annotation.call_machine')
    def test_threads_progress_bar_active(self, mock_call, mock_monitor):
        # Setup mocks
        mock_call.return_value = {"item_id": 123, "response": "{}", "finish_reason": "STOP"}
        mock_monitor.return_value = MagicMock() # Mock the thread object
        
        videos = [123, 456]
        
        # Even with verbose=False, monitor SHOULD be called now
        ma.call_machine_threads(videos, max_workers=2, verbose=False)
        
        # Check monitor WAS called
        mock_monitor.assert_called()
        print("Progress monitor was started (as expected).")

if __name__ == '__main__':
    unittest.main()
