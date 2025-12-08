import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
import io

# Setup path
current_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(current_dir))

import fyp.machine_annotation as ma

class TestSilence(unittest.TestCase):
    def test_consolidate_silence(self):
        # Create a dummy dataframe that triggers the "rare columns" logic
        # It needs enough rows to have non-null ratio calc, and some rare cols
        df = pd.DataFrame({
            'common': [1] * 20,
            'rare': [None] * 19 + [1], # < 10% populated
            'other': [2] * 20
        })
        
        captured_output = io.StringIO()
        sys.stdout = captured_output
        try:
            ma.consolidate_rare_columns_from_gemini_output(df, verbose=False)
        finally:
            sys.stdout = sys.__stdout__
            
        output = captured_output.getvalue()
        self.assertNotIn("hejhej", output)
        self.assertNotIn("ERROR", output) # Should not print error if verbose=False
        print("Consolidate Output (should be empty):", output)

    @patch('fyp.machine_annotation._start_monitor')
    @patch('fyp.machine_annotation.call_machine')
    def test_threads_silence(self, mock_call, mock_monitor):
        # Setup mocks
        mock_call.return_value = {"item_id": 123, "response": "{}", "finish_reason": "STOP"}
        
        # We want to verify _start_monitor is NOT called when verbose=False
        
        videos = [123, 456]
        
        captured_output = io.StringIO()
        sys.stdout = captured_output
        try:
            ma.call_machine_threads(videos, max_workers=2, verbose=False)
        finally:
            sys.stdout = sys.__stdout__
            
        output = captured_output.getvalue()
        
        # Check that checks for prints are satisfied
        self.assertNotIn("Calling", output)
        self.assertNotIn("Items processed", output)
        
        # Check monitor was NOT called
        mock_monitor.assert_not_called()
        print("Threads Output (should be empty):", output)

if __name__ == '__main__':
    unittest.main()
