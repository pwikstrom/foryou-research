import binascii
import hashlib
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

from flask import abort, current_app
from flask_login import AnonymousUserMixin, UserMixin, current_user

import fyp.data_io as data_io

logger = logging.getLogger(__name__)

# --- Role Definitions ---
ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"

class AnonymousUser(AnonymousUserMixin):


    def is_admin(self):
        return False

    def can_access(self, perm_key: str) -> bool:
        return False


# --- Role Manager ---

class RoleManager:
    """Manages roles and their permission sets, persisted in roles.json.

    Storage format (current):
        {"admin": {"permissions": ["*"]},
         "viewer": {"permissions": ["tab.explore", ...]}}

    Legacy format (auto-migrated on load):
        ["admin", "viewer", "researcher"]
    """

    def __init__(self, storage_location="users"):
        self.storage_location = storage_location
        self.roles: dict[str, dict] = {}
        self.filename = "roles.json"

        self.load_roles()

    def load_roles(self):
        """Load roles from roles.json, migrating the legacy list format if needed."""
        if data_io.exists(storage_location=self.storage_location, filename=self.filename):
            loaded = data_io.load_json(storage_location=self.storage_location, filename=self.filename)
            if isinstance(loaded, list):
                logger.info("Migrating legacy roles.json (list format) to dict-with-permissions format.")
                self.roles = self._migrate_legacy(loaded)
                self.save_roles()
            elif isinstance(loaded, dict):
                self.roles = {
                    name: {"permissions": list(entry.get("permissions", []))}
                    for name, entry in loaded.items()
                    if isinstance(entry, dict)
                }
            else:
                logger.warning(f"Invalid format for {self.filename}; resetting to defaults.")
                self.roles = {}
        else:
            self.roles = {}

        self._ensure_defaults()

    def _migrate_legacy(self, legacy_list: list) -> dict[str, dict]:
        """Convert a flat list of role names into the dict-with-permissions format.

        Admin gets ``"*"`` (full access). Every other role gets the same
        default set the current viewer experience uses today, so existing
        installations don't see behavioural drift after upgrade.
        """
        from web_interface.permissions import DEFAULT_NON_ADMIN_PERMISSIONS
        migrated: dict[str, dict] = {}
        for name in legacy_list:
            if not isinstance(name, str):
                continue
            if name == ROLE_ADMIN:
                migrated[name] = {"permissions": ["*"]}
            else:
                migrated[name] = {"permissions": list(DEFAULT_NON_ADMIN_PERMISSIONS)}
        return migrated

    def _ensure_defaults(self):
        """Make sure the built-in admin and viewer roles exist with sensible defaults."""
        from web_interface.permissions import DEFAULT_NON_ADMIN_PERMISSIONS
        changed = False
        if ROLE_ADMIN not in self.roles:
            self.roles[ROLE_ADMIN] = {"permissions": ["*"]}
            changed = True
        if ROLE_VIEWER not in self.roles:
            self.roles[ROLE_VIEWER] = {"permissions": list(DEFAULT_NON_ADMIN_PERMISSIONS)}
            changed = True
        if changed:
            self.save_roles()

    def save_roles(self):
        """Persist the current roles dict to roles.json."""
        try:
            data_io.save_json(data=self.roles, storage_location=self.storage_location, filename=self.filename)
        except Exception as e:
            logger.error(f"Failed to save roles: {e}")

    def get_roles(self) -> list[str]:
        """Return role names only (backward-compatible with the legacy API)."""
        return list(self.roles.keys())

    def get_roles_with_permissions(self) -> list[dict]:
        """Return [{name, permissions}] for every role — used by the admin matrix UI."""
        return [
            {"name": name, "permissions": list(entry.get("permissions", []))}
            for name, entry in self.roles.items()
        ]

    def get_role_permissions(self, role_name: str | None) -> list[str]:
        """Return the permission list for ``role_name``, or [] if unknown."""
        if not role_name:
            return []
        entry = self.roles.get(role_name)
        if not entry:
            return []
        return list(entry.get("permissions", []))

    def set_role_permissions(self, role_name: str, permissions: list[str]):
        """Replace the permission list for ``role_name``."""
        if role_name == ROLE_ADMIN:
            return False, "Cannot modify admin role permissions"
        if role_name not in self.roles:
            return False, "Role not found"
        self.roles[role_name]["permissions"] = list(permissions)
        self.save_roles()
        return True, "Permissions updated"

    def add_role(self, role_name):
        from web_interface.permissions import DEFAULT_NON_ADMIN_PERMISSIONS
        if role_name in self.roles:
            return False, "Role already exists"
        self.roles[role_name] = {"permissions": list(DEFAULT_NON_ADMIN_PERMISSIONS)}
        self.save_roles()
        return True, "Role added"

    def delete_role(self, role_name, user_manager_instance):
        if role_name == ROLE_ADMIN:
            return False, "Cannot delete admin role"

        if role_name not in self.roles:
            return False, "Role not found"

        for u in user_manager_instance.users.values():
            if u.role == role_name:
                return False, f"Cannot delete role '{role_name}' because it is assigned to user '{u.username}'"

        del self.roles[role_name]
        self.save_roles()
        return True, "Role deleted"

    def role_exists(self, role_name):
        return role_name in self.roles


# Initialize Role Manager
role_manager = RoleManager(storage_location="users")


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
    def __init__(self, username, role, password_hash, approved=True, last_login=None, settings=None, machine_annotation_votes=None):
        self.id = username
        self.username = username
        self.role = role
        self.password_hash = password_hash
        self.approved = approved
        self.last_login = last_login
        self.settings = settings if settings is not None else {}
        self.machine_annotation_votes = machine_annotation_votes if machine_annotation_votes is not None else {}

    def is_admin(self):
        return self.role == ROLE_ADMIN and self.approved

    def can_access(self, perm_key: str) -> bool:
        """Return True if this user has access to ``perm_key``.

        Admin role always passes. Other roles must have ``perm_key`` (or
        ``"*"``) in their stored permission list. Parent-tab keys (``tab.admin``,
        ``tab.data_management``) are implicitly granted whenever any of their
        sub-pages is granted — delegates to ``permissions.user_has_permission``.
        """
        # Imported here to avoid a circular import at module load.
        from web_interface.permissions import user_has_permission
        return user_has_permission(self, perm_key)

    def to_dict(self):
        return {
            "username": self.username,
            "role": self.role,
            "password_hash": self.password_hash,
            "approved": self.approved,
            "last_login": self.last_login,
            "settings": self.settings,
            "machine_annotation_votes": self.machine_annotation_votes
        }

# --- User Manager ---

class UserManager:
    def __init__(self, storage_location="users", bulk_load=True):
        """Initialize the user manager.

        Args:
            storage_location: Named storage bucket / local dir in data_io.
            bulk_load: If True (default), eagerly migrate legacy data and
                load every user JSON into memory at startup — needed on the
                web service that authenticates browser traffic via
                Flask-Login. If False, skip both the migration and the
                initial fan-out; users are loaded on demand by
                ``get_user()``. Services that don't authenticate (e.g.
                task-runner serving only Cloud Tasks internal routes)
                should pass ``bulk_load=False`` so their cold-start cost
                stays O(1) in the number of users.
        """
        self.storage_location = storage_location
        self.users = {}
        self.bulk_load = bulk_load

        if not bulk_load:
            # Lazy mode: no migration, no preload, no default admin. The
            # service that owns user data (web) handles migration and
            # bootstrap; task-runner just needs `get_user()` to work
            # on-demand when something (unexpectedly) asks for one.
            print(
                f"[AUTH] UserManager initialized in lazy mode "
                f"(storage={self.storage_location}) — users loaded on demand",
                flush=True,
            )
            return

        # Migration from legacy monolithic files
        self.migrate_legacy_data()

        # Initial Load
        self.load_users()

        # Create default admin if empty
        if not self.users:
            logger.info("No users found. Creating default admin.")
            self.add_user("admin@admin.net", "admin", ROLE_ADMIN, approved=True)
    
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
                        "role": user_data.get('role', 'viewer'), # Default to viewer if missing
                        "password_hash": user_data.get('password_hash'),
                        "approved": user_data.get('approved', True),
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
        """Loads users from individual JSON files in the storage location.

        GCS reads are per-file round-trips (~50ms each) so we fan them out
        across a thread pool — turning ~3.4s serial into ~0.2s for 60+ users.
        """
        self.users = {}
        _t_start = time.perf_counter()
        try:
            # 1. List all .json files in storage location
            files = data_io.listdir(storage_location=self.storage_location, return_absolute_path=False)
            json_files = [f for f in files if f.endswith('.json') and not f.endswith('_tags.json') and f != "roles.json"]

            def _load_one(fname):
                try:
                    return fname, data_io.load_json(storage_location=self.storage_location, filename=fname)
                except Exception as e:
                    logger.error(f"Failed to load user file {fname}: {e}")
                    return fname, None

            max_workers = min(32, max(4, len(json_files)))
            results = []
            if json_files:
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    results = list(pool.map(_load_one, json_files))

            for fname, user_data in results:
                if user_data and 'username' in user_data:
                    username = user_data['username']
                    self.users[username] = User(
                        username=username,
                        role=user_data.get('role', 'viewer'),
                        password_hash=user_data.get('password_hash'),
                        approved=user_data.get('approved', True),
                        last_login=user_data.get('last_login'),
                        settings=user_data.get('settings', {}),
                        machine_annotation_votes=user_data.get('machine_annotation_votes', {})
                    )

            elapsed = time.perf_counter() - _t_start
            # Use print() so the timing line reliably surfaces in Cloud Logging
            # (default root logger level is WARNING, which drops logger.info).
            print(f"[AUTH] Loaded {len(self.users)} users from {self.storage_location} in {elapsed:.2f}s (workers={max_workers})")
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
        """Return the User for ``user_id``, or None.

        In bulk-load mode the lookup is a simple dict hit against the
        in-memory cache populated at startup. In lazy mode we fall back
        to reading ``{user_id}.json`` from the storage location on cache
        miss, then cache the result — keeping cold-start cost O(1) while
        still returning the right object if something unexpectedly asks.
        """
        user = self.users.get(user_id)
        if user is not None:
            return user

        if not self.bulk_load and isinstance(user_id, str) and user_id:
            filename = f"{user_id}.json"
            try:
                if not data_io.exists(
                    storage_location=self.storage_location, filename=filename
                ):
                    return None
                user_data = data_io.load_json(
                    storage_location=self.storage_location, filename=filename
                )
            except Exception as e:
                logger.error(f"Lazy load of user {user_id!r} failed: {e}")
                return None

            if not user_data or "username" not in user_data:
                return None

            user = User(
                username=user_data["username"],
                role=user_data.get("role", "viewer"),
                password_hash=user_data.get("password_hash"),
                approved=user_data.get("approved", True),
                last_login=user_data.get("last_login"),
                settings=user_data.get("settings", {}),
                machine_annotation_votes=user_data.get("machine_annotation_votes", {}),
            )
            self.users[user_id] = user
            return user

        return None

    def add_user(self, username, password, role, approved=False):
        if not role_manager.role_exists(role):
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
        
        # Ideally: data_io.delete(storage_location=..., filename=...)
        # Since we don't have delete exposed in data_io consistently/easily for all backends (though we do have remove),
        # we try to use data_io.remove
        try:
            if data_io.exists(storage_location=self.storage_location, filename=filename):
                data_io.remove(storage_location=self.storage_location, filename=filename)
                logger.info(f"Removed user file {filename}")
        except Exception as e:
            logger.error(f"Failed to remove user file {filename}: {e}")
            
        return True, "User deleted"
    
    def update_user_role(self, username, new_role):
        if username not in self.users:
            return False, "User not found"
        if not role_manager.role_exists(new_role):
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

    def register_annotation_vote(self, username, collection_id, period):
        if username not in self.users:
            return False, "User not found"
            
        user = self.users[username]
        votes = user.machine_annotation_votes
        
        # Initialize list for this collection if missing
        if collection_id not in votes:
             votes[collection_id] = []
             
        # Add the period if they haven't voted for it already
        if period not in votes[collection_id]:
             votes[collection_id].append(period)
             self.save_user(username)
             return True, "Vote registered"
             
        return True, "Already voted"

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
            
            # Allow admin to access everything
            if current_user.is_admin():
                return f(*args, **kwargs)

            # Check if user's role is in the allowed list
            if current_user.role not in roles:
                 abort(403) # Forbidden
                 
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def admin_required(f):
    return role_required([ROLE_ADMIN])(f)



