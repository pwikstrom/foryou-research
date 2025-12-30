
import sys
import os
import json
import pytest
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))
sys.path.append(str(PROJECT_ROOT / "web_interface"))

# Import the app
from web_interface.app import app

def test_persona_stats_api():
    client = app.test_client()
    
    print(f"\nTesting /api/persona_stats ...")
    
    try:
        response = client.post(f'/api/persona_stats')
        
        if response.status_code == 404:
            print("Study not found or no events. Trying 'baseline_only'...")
            study_name = "baseline_only"
            response = client.post(f'/api/persona_stats/{study_name}')

        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = json.loads(response.data)
            print(f"Success! Received {len(data)} records.")
            if len(data) > 0:
                print("Sample record keys:", data[0].keys())
                print("Sample record values:", data[0])
                
                # Verify key metrics exist
                assert 'chattiness' in data[0]
                assert 'enthusiasm' in data[0]
                assert 'moniker' in data[0]
                assert 'sessions_per_day' in data[0]
                
        else:
            print("Error Response:", response.data.decode('utf-8'))
            
    except Exception as e:
        print(f"Test failed with exception: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_persona_stats_api()
