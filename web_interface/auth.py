import json
import os
import hashlib
import binascii
from flask_login import UserMixin, current_user
from functools import wraps
from flask import abort, current_app
from pathlib import Path
import logging



logger = logging.getLogger(__name__)

# --- Role Definitions ---
ROLE_ADMIN = "admin"
ROLE_RESEARCHER = "researcher" 
ROLE_VIEWER = "viewer"

ROLES = [ROLE_ADMIN, ROLE_RESEARCHER, ROLE_VIEWER]

# --- Password Hashing Helpers ---

def hash_password(password):
    """Hash a password for storing."""
    salt = hashlib.sha256(os.urandom(60)).hexdigest().encode('ascii')
    pwdhash = hashlib.pbkdf2_hmac('sha512', password.encode('utf-8'), 
                                salt, 100000)
    pwdhash = binascii.hexlify(pwdhash)
    return (salt + pwdhash).decode('ascii')

def verify_password(stored_password, provided_password):
    """Verify a stored password against one provided by user"""
    salt = stored_password[:64]
    stored_password = stored_password[64:]
    pwdhash = hashlib.pbkdf2_hmac('sha512', 
                                  provided_password.encode('utf-8'), 
                                  salt.encode('ascii'), 
                                  100000)
    pwdhash = binascii.hexlify(pwdhash).decode('ascii')
    return pwdhash == stored_password

# --- User Class ---

class User(UserMixin):
    def __init__(self, username, role, password_hash, approved=True, last_login=None, settings=None):
        self.id = username
        self.username = username
        self.role = role
        self.password_hash = password_hash
        self.approved = approved
        self.last_login = last_login
        self.settings = settings if settings is not None else {}

    def can_access_research_features(self):
        return self.role in [ROLE_ADMIN, ROLE_RESEARCHER] and self.approved

    def is_admin(self):
        return self.role == ROLE_ADMIN and self.approved
        
    def to_dict(self):
        return {
            "username": self.username,
            "role": self.role,
            "password_hash": self.password_hash,
            "approved": self.approved,
            "last_login": self.last_login,
            "settings": self.settings
        }

# --- User Manager ---

class UserManager:
    def __init__(self, filepath, gcs_bucket=None, gcs_path=None):
        self.filepath = Path(filepath)
        self.users = {}
        self.gcs_bucket = gcs_bucket
        self.gcs_path = gcs_path
        
        # Initial Load
        self.load_users()
        
        # Create default admin if empty
        if not self.users:
            logger.info("No users found. Creating default admin.")
            self.add_user("info@foryouresearch.net", "kelvingrove", ROLE_ADMIN, approved=True)
    
    def load_users(self):
        """Loads users from local JSON, syncs from GCS if configured."""
        if self.gcs_bucket and self.gcs_path:
            try:
                blob = self.gcs_bucket.blob(self.gcs_path)
                if blob.exists():
                    logger.info(f"Downloading users from GCS: gs://{self.gcs_bucket.name}/{self.gcs_path}")
                    blob.download_to_filename(str(self.filepath))
                else:
                    logger.info(f"No remote users file found at gs://{self.gcs_bucket.name}/{self.gcs_path}. Using local default.")
            except Exception as e:
                logger.error(f"Failed to sync users from GCS: {e}")

        if self.filepath.exists():
            try:
                with open(self.filepath, 'r') as f:
                    data = json.load(f)
                    for username, user_data in data.items():
                        self.users[username] = User(
                            username=user_data['username'],
                            role=user_data['role'],
                            password_hash=user_data['password_hash'],
                            approved=user_data.get('approved', True),
                            last_login=user_data.get('last_login'),
                            settings=user_data.get('settings', {})
                        )
                logger.info(f"Loaded {len(self.users)} users from {self.filepath}")
            except Exception as e:
                logger.error(f"Failed to load user database: {e}")
                self.users = {}
        else:
            logger.warning(f"User database not found at {self.filepath}")
            self.users = {}

    def save_users(self):
        """Saves users to local JSON."""
        data = {u.username: u.to_dict() for u in self.users.values()}
        try:
            with open(self.filepath, 'w') as f:
                json.dump(data, f, indent=4)
            logger.info("Saved user database.")
            
            if self.gcs_bucket and self.gcs_path:
                try:
                     blob = self.gcs_bucket.blob(self.gcs_path)
                     blob.upload_from_filename(str(self.filepath))
                     logger.info(f"Uploaded users to GCS: gs://{self.gcs_bucket.name}/{self.gcs_path}")
                except Exception as e:
                     logger.error(f"Failed to upload users to GCS: {e}")
            
        except Exception as e:
            logger.error(f"Failed to save user database: {e}")

    def get_user(self, user_id):
        return self.users.get(user_id)

    def add_user(self, username, password, role, approved=False):
        if role not in ROLES:
            return False, "Invalid role"
        
        if username in self.users:
            return False, "User already exists"
            
        password_hash = hash_password(password)
        new_user = User(username, role, password_hash, approved=approved)
        self.users[username] = new_user
        self.save_users()
        return True, "User created"

    def delete_user(self, username):
        if username not in self.users:
            return False, "User not found"
        
        # Prevent deleting the last admin
        admins = [u for u in self.users.values() if u.role == ROLE_ADMIN and u.approved]
        if self.users[username].role == ROLE_ADMIN and len(admins) <= 1:
            return False, "Cannot delete the last admin user"

        del self.users[username]
        self.save_users()
        return True, "User deleted"
    
    def update_user_role(self, username, new_role):
        if username not in self.users:
            return False, "User not found"
        if new_role not in ROLES:
            return False, "Invalid role"
            
        # Prevent demoting the last admin
        admins = [u for u in self.users.values() if u.role == ROLE_ADMIN and u.approved]
        if self.users[username].role == ROLE_ADMIN and len(admins) <= 1 and new_role != ROLE_ADMIN:
             return False, "Cannot demote the last admin user"

        self.users[username].role = new_role
        self.save_users()
        return True, "Role updated"
        
    def approve_user(self, username):
        if username not in self.users:
            return False, "User not found"
        
        self.users[username].approved = True
        self.save_users()
        return True, "User approved"

    def update_password(self, username, new_password):
        if username not in self.users:
             return False, "User not found"
             
        password_hash = hash_password(new_password)
        self.users[username].password_hash = password_hash
        self.save_users()
        return True, "Password updated"

    def update_last_login(self, username):
        if username in self.users:
            import datetime
            self.users[username].last_login = datetime.datetime.now().isoformat()
            self.save_users()

    def update_user_settings(self, username, settings):
        if username not in self.users:
            return False, "User not found"
        
        # Merge or replace? Let's generic replace for top-level keys, but maybe merge is safer?
        # For now, strict replacement of the settings dict provided
        # Or better: update existing dict with new keys
        if self.users[username].settings is None:
             self.users[username].settings = {}
             
        self.users[username].settings.update(settings)
        self.save_users()
        return True, "Settings updated"

    def verify_user(self, username, password):
        user = self.users.get(username)
        if user and verify_password(user.password_hash, password):
            if not user.approved:
                return None # Or handle differently in calling code
            return user
        return None

# --- Decorators ---

def role_required(roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return current_app.login_manager.unauthorized()
            
            if current_user.role not in roles:
                 abort(403) # Forbidden
                 
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def admin_required(f):
    return role_required([ROLE_ADMIN])(f)

def researcher_required(f):
    return role_required([ROLE_ADMIN, ROLE_RESEARCHER])(f)
