
import sys
import unittest
import json
import os
from pathlib import Path
import tempfile
import shutil

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))
sys.path.append(str(PROJECT_ROOT / "web_interface"))

# Mock config files before importing app
os.environ['FLASK_SECRET_KEY'] = 'test-secret'

class TestTaggingAPI(unittest.TestCase):
    def setUp(self):
        # Create temp dir for config
        self.test_dir = tempfile.mkdtemp()
        self.users_file = Path(self.test_dir) / "users.json"
        
        # Setup paths patch
        from web_interface.hub_config import fyp_cf
        self.original_users_path = fyp_cf['paths'].get('users')
        fyp_cf['paths']['users'] = str(Path(self.test_dir) / "users_data")
        os.makedirs(fyp_cf['paths']['users'], exist_ok=True)
        
        # Import app
        from web_interface.fyp_data_hub import app, user_manager
        
        # Mock user manager
        user_manager.filepath = self.users_file
        user_manager.users = {}
        # Create admin
        user_manager.add_user("admin", "adminpass", "admin", approved=True)
        
        self.app = app
        self.client = app.test_client()
        self.user_manager = user_manager
        
        # Login
        self.client.post('/login', data=dict(username="admin", password="adminpass"), follow_redirects=True)

    def tearDown(self):
        from web_interface.hub_config import fyp_cf
        if self.original_users_path:
            fyp_cf['paths']['users'] = self.original_users_path
        shutil.rmtree(self.test_dir)

    def test_save_and_load_tags(self):
        # 1. Save Tags
        payload = {
            "study": "test_study",
            "item_id": "12345",
            "variable": "description",
            "tags": ["funny", "viral"]
        }
        rv = self.client.post('/api/viewer/tags/save', json=payload)
        self.assertEqual(rv.status_code, 200)
        
        # 2. Check File Created
        tag_file = Path(self.test_dir) / "users_data" / "admin_tags.json"
        self.assertTrue(tag_file.exists())
        
        # 3. Load Tags via API
        rv = self.client.get('/api/viewer/tags')
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data)
        
        self.assertIn("test_study", data)
        self.assertIn("12345", data["test_study"])
        self.assertEqual(data["test_study"]["12345"]["description"], ["funny", "viral"])
        
    def test_update_tags_cleanup(self):
        # 1. Save Initial
        self.client.post('/api/viewer/tags/save', json={
            "study": "test_study",
            "item_id": "12345",
            "variable": "desc",
            "tags": ["A"]
        })
        
        # 2. Update to Empty (Should delete)
        self.client.post('/api/viewer/tags/save', json={
            "study": "test_study",
            "item_id": "12345",
            "variable": "desc",
            "tags": []
        })
        
        rv = self.client.get('/api/viewer/tags')
        data = json.loads(rv.data)
        # Should be empty object as we prune empty studies
        self.assertEqual(data, {})

if __name__ == '__main__':
    unittest.main()
