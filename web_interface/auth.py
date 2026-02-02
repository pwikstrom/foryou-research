import json
import os
import hashlib
import binascii
from flask_login import UserMixin, current_user
from functools import wraps
from flask import abort, current_app
from pathlib import Path
import logging
import fyp.data_io as data_io



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
    def __init__(self, storage_location="users"):
        self.storage_location = storage_location
        self.users = {}
        
        # Migration from legacy monolithic files
        self.migrate_legacy_data()

        # Initial Load
        self.load_users()
        
        # Create default admin if empty
        if not self.users:
            logger.info("No users found. Creating default admin.")
            self.add_user("info@foryouresearch.net", "kelvingrove", ROLE_ADMIN, approved=True)
    
    def migrate_legacy_data(self):
        """Migrates legacy users.json and _tags.json to individual {username}.json files."""
        legacy_file = "users.json"
        
        # Check if legacy file exists using data_io
        if data_io.exists(storage_location=self.storage_location, filename=legacy_file):
            logger.info("Found legacy users.json, starting migration...")
            
            try:
                legacy_users = data_io.load_json(storage_location=self.storage_location, filename=legacy_file)
                if not legacy_users:
                    return

                for username, user_data in legacy_users.items():
                    target_filename = f"{username}.json"
                    
                    # 1. Check if already migrated
                    if data_io.exists(storage_location=self.storage_location, filename=target_filename):
                        logger.info(f"Skipping migration for {username}, {target_filename} already exists.")
                        continue
                        
                    # 2. Build new user object structure
                    new_user_data = {
                        "username": user_data.get('username', username),
                        "role": user_data.get('role', ROLE_VIEWER),
                        "password_hash": user_data.get('password_hash'),
                        "approved": user_data.get('approved', True),
                        "last_login": user_data.get('last_login'),
                        "last_login": user_data.get('last_login'),
                        "settings": user_data.get('settings', {})
                    }
                    
                    # Ensure defaults for settings
                    default_settings = {
                        "share_annotations": True,
                        "video_autostart": False
                    }
                    # Update defaults with existing settings (existing override defaults)
                    merged_settings = default_settings.copy()
                    merged_settings.update(new_user_data['settings'])
                    new_user_data['settings'] = merged_settings
                    
                    # 3. Check for and merge legacy tags
                    tags_filename = f"{username}_tags.json"
                    tags_data = {}
                    if data_io.exists(storage_location=self.storage_location, filename=tags_filename):
                         logger.info(f"Merging legacy tags for {username}...")
                         tags_data = data_io.load_json(storage_location=self.storage_location, filename=tags_filename)
                         if tags_data:
                             new_user_data['annotations'] = tags_data
                             
                    # 4. Save new individual file
                    data_io.save_json(data=new_user_data, storage_location=self.storage_location, filename=target_filename)
                    logger.info(f"Migrated {username} to {target_filename}")
                    
                    # 5. Handle legacy tags file "rename" (Load -> SaveAs -> Remove)
                    if tags_data: # Only if we successfully loaded it
                        try:
                            migrated_tags_filename = f"{username}_tags.json.migrated"
                            data_io.save_json(data=tags_data, storage_location=self.storage_location, filename=migrated_tags_filename)
                            data_io.remove(storage_location=self.storage_location, filename=tags_filename)
                        except Exception as e:
                            logger.error(f"Failed to rename legacy tags file for {username}: {e}")

                # 6. Handle legacy users.json "rename"
                try:
                    migrated_users_filename = "users.json.migrated"
                    data_io.save_json(data=legacy_users, storage_location=self.storage_location, filename=migrated_users_filename)
                    data_io.remove(storage_location=self.storage_location, filename=legacy_file)
                    logger.info("Legacy users.json migrated and renamed.")
                except Exception as e:
                    logger.error(f"Failed to rename legacy users.json: {e}")

            except Exception as e:
                logger.error(f"Migration failed: {e}")

    def load_users(self):
        """Loads users from individual JSON files in the storage location."""
        self.users = {}
        try:
            # 1. List all .json files in users directory
            files = data_io.listdir(storage_location=self.storage_location, return_absolute_path=False)
            json_files = [f for f in files if f.endswith('.json') and not f.endswith('_tags.json')]
            
            for f in json_files:
                try:
                    user_data = data_io.load_json(storage_location=self.storage_location, filename=f)
                    if user_data and 'username' in user_data:
                        username = user_data['username']
                        self.users[username] = User(
                            username=username,
                            role=user_data.get('role', ROLE_VIEWER),
                            password_hash=user_data.get('password_hash'),
                            approved=user_data.get('approved', True),
                            last_login=user_data.get('last_login'),
                            settings=user_data.get('settings', {})
                        )
                except Exception as e:
                    logger.error(f"Failed to load user file {f}: {e}")
            
            logger.info(f"Loaded {len(self.users)} users from {self.storage_location}")
        except Exception as e:
            logger.error(f"Failed to list user directory: {e}")
            self.users = {}

    def save_user(self, username):
        """Saves a specific user to their individual JSON file."""
        user = self.users.get(username)
        if not user: return
        
        filename = f"{username}.json"
        
        # Load existing file to preserve 'annotations' if present (crucial for preserving tags during auth updates)
        existing_data = {}
        if data_io.exists(storage_location=self.storage_location, filename=filename):
            existing_data = data_io.load_json(storage_location=self.storage_location, filename=filename) or {}
            
        # Update with current user object state
        user_dict = user.to_dict()
        existing_data.update(user_dict)
        
        try:
            data_io.save_json(data=existing_data, storage_location=self.storage_location, filename=filename)
            logger.info(f"Saved user {username}.")
        except Exception as e:
            logger.error(f"Failed to save user {username}: {e}")

    def get_user(self, user_id):
        return self.users.get(user_id)

    def add_user(self, username, password, role, approved=False):
        if role not in ROLES:
            return False, "Invalid role"
        
        if username in self.users:
            return False, "User already exists"
            
        password_hash = hash_password(password)
        new_user = User(username, role, password_hash, approved=approved)
        # Fix Default Settings for New Users
        new_user.settings = {
            "share_annotations": True,
            "video_autostart": False
        }
        
        self.users[username] = new_user
        self.save_user(username)
        return True, "User created"

    def delete_user(self, username):
        if username not in self.users:
            return False, "User not found"
        
        # Prevent deleting the last admin
        admins = [u for u in self.users.values() if u.role == ROLE_ADMIN and u.approved]
        if self.users[username].role == ROLE_ADMIN and len(admins) <= 1:
            return False, "Cannot delete the last admin user"

        del self.users[username]
        # Also delete the file? Or keep as archive? Usually delete.
        filename = f"{username}.json"
        # We can implement delete in data_io but it might not be exposed. 
        # For now, just removing from memory effectively bans them until reload, but file persists.
        # Ideally: data_io.delete(storage_location=..., filename=...)
        # Since we don't have delete exposed, we rely on removing from self.users.
        # Wait, if we reload, they come back! We MUST delete or rename the file.
        # Assuming we can't delete easily, we should mark as deleted in the file? 
        # Or just empty the file content?
        # Let's save an empty dict or a dict with disabled flag.
        # Better: try to use os.remove directly if local? data_io abstracts this.
        # Let's write a file with "deleted": True and filter in load_users.
        
        # Actually, let's just save the file with a flag and handle it in load_users or overwrite with garbage?
        # A simple approach: save an empty file or specific "deleted" marker.
        try:
           # Overwrite with empty object or specific marker
           # But load_users checks 'username' key. If we save {}, it won't load. Correct.
           data_io.save_json(data={}, storage_location=self.storage_location, filename=filename)
        except:
           pass
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
        self.save_user(username)
        return True, "Role updated"
        
    def approve_user(self, username):
        if username not in self.users:
            return False, "User not found"
        
        self.users[username].approved = True
        self.save_user(username)
        return True, "User approved"

    def update_password(self, username, new_password):
        if username not in self.users:
             return False, "User not found"
             
        password_hash = hash_password(new_password)
        self.users[username].password_hash = password_hash
        self.save_user(username)
        return True, "Password updated"

    def update_last_login(self, username):
        if username in self.users:
            import datetime
            self.users[username].last_login = datetime.datetime.now().isoformat()
            self.save_user(username)

    def update_user_settings(self, username, settings):
        if username not in self.users:
            return False, "User not found"
        
        # Merge or replace? Let's generic replace for top-level keys, but maybe merge is safer?
        # For now, strict replacement of the settings dict provided
        # Or better: update existing dict with new keys
        if self.users[username].settings is None:
             self.users[username].settings = {}
             
        self.users[username].settings.update(settings)
        self.save_user(username)
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
