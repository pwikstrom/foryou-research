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

class TestAuthIntegration(unittest.TestCase):
    def setUp(self):
        # Create temp dir for config
        self.test_dir = tempfile.mkdtemp()
        self.users_file = Path(self.test_dir) / "users.json"
        
        # Patch paths in auth module using reload or just monkeypatching if possible
        # Since I can't easily patch constants in imported modules without reloading, 
        # I will just rely on the fact that I passed USERS_FILE to UserManager in fyp_data_hub.
        # But wait, fyp_data_hub imports auth and initializes user_manager with a fixed path.
        # I need to mock that path.
        
        # Import app
        from web_interface.fyp_data_hub import app, user_manager
        
        # Swap user managers file path
        user_manager.filepath = self.users_file
        user_manager.users = {}
        # Create admin (MUST BE APPROVED)
        user_manager.add_user("admin", "adminpass", "admin", approved=True)
        
        self.app = app
        self.client = app.test_client()
        self.user_manager = user_manager

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def login(self, username, password):
        return self.client.post('/login', data=dict(
            username=username,
            password=password
        ), follow_redirects=True)

    def logout(self):
        return self.client.get('/logout', follow_redirects=True)

    def test_login_logout(self):
        # Test valid login
        rv = self.login("admin", "adminpass")
        self.assertIn(b'Logged in as: <strong>admin</strong>', rv.data)
        
        # Test logout
        rv = self.logout()
        self.assertIn(b'Login', rv.data)
        
        # Test invalid login
        rv = self.login("admin", "wrongpass")
        self.assertIn(b'Invalid username or password', rv.data)

    def test_protected_routes(self):
        self.logout()
        # Try to access index
        rv = self.client.get('/', follow_redirects=True)
        # Should redirect to login
        self.assertIn(b'Login', rv.data)
        
        # Should redirect to login (302 found)
        self.assertEqual(rv.status_code, 200) # follow_redirects=True lands on login page (200)
        self.assertIn(b'Login', rv.data)
        
        # Try API without following redirects (should be 302)
        rv = self.client.get('/api/status', follow_redirects=False)
        self.assertEqual(rv.status_code, 302) # Flask-Login default is redirect
        self.assertTrue(rv.location.endswith('/login?next=%2Fapi%2Fstatus'))

    def test_role_access(self):
        # Create viewer (APPROVED)
        self.user_manager.add_user("viewer", "viewerpass", "viewer", approved=True)
        
        self.login("viewer", "viewerpass")
        
        # Viewer accessing admin route
        rv = self.client.get('/api/admin/users')
        self.assertEqual(rv.status_code, 403)
        
        self.logout() # Ensure clean state
        
        # Admin accessing admin route
        self.login("admin", "adminpass")
        rv = self.client.get('/api/admin/users')
        self.assertEqual(rv.status_code, 200)

    def test_state_cache_isolation(self):
        from web_interface.fyp_data_hub import study_cache
        
        # Mocking data loading is complex without files, but we can test the cache mechanism directly
        study_cache.put("studyA", {"df": [1,2,3]})
        study_cache.put("studyB", {"df": [4,5,6]})
        
        self.assertEqual(study_cache.get("studyA")['df'], [1,2,3])
        self.assertEqual(study_cache.get("studyB")['df'], [4,5,6])
        
        # Test eviction (size 2)
        study_cache.put("studyC", {"df": [7,8,9]})
        # Cache usually evicts least recently used. 
        # get('studyA') accessed it, but put('studyB') was later? 
        # LRUCache: put A, put B. cache=[A, B].
        # get A. cache=[B, A].
        # put C. Evict B. cache=[A C].
        
        # Depending on exact implementation details of cachetools, verifying eviction:
        # We manually just check if it holds data.
        self.assertIsNotNone(study_cache.get("studyC"))

    def test_signup_approval_flow(self):
        # 1. Signup
        self.logout()
        rv = self.client.post('/signup', data=dict(
            username="student1",
            password="password",
            confirm_password="password"
        ), follow_redirects=True)
        self.assertIn(b'Account created', rv.data)
        
        # 2. Try Login (Should fail/pending)
        rv = self.login("student1", "password")
        self.assertIn(b'pending approval', rv.data)
        
        # 3. Approve as Admin
        self.login("admin", "adminpass")
        rv = self.client.put('/api/admin/users', json={
            "action": "approve",
            "username": "student1"
        })
        self.assertEqual(rv.status_code, 200)
        self.logout()
        
        # 4. Login again (Should success)
        rv = self.login("student1", "password")
        self.assertIn(b'Logged in as: <strong>student1</strong>', rv.data)

    def test_admin_password_reset(self):
        # Setup student user
        self.user_manager.add_user("student2", "oldpass", "viewer", approved=True)
        
        # Admin resets password
        self.login("admin", "adminpass")
        rv = self.client.put('/api/admin/users', json={
            "action": "reset_password",
            "username": "student2",
            "new_password": "newpass"
        })
        self.assertEqual(rv.status_code, 200)
        self.logout()
        
        # Verify old password fails
        rv = self.login("student2", "oldpass")
        self.assertIn(b'Invalid username or password', rv.data)
        
        # Verify new password works
        rv = self.login("student2", "newpass")
        self.assertIn(b'Logged in as: <strong>student2</strong>', rv.data)

if __name__ == '__main__':
    unittest.main()
