import binascii
import datetime
import hashlib
import logging
import os
import secrets
import threading
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

# JSON files that live in the user-store directory but are NOT individual user
# records. The roster loader and the default-admin check must ignore these —
# otherwise a store that still holds e.g. var_presentation.json looks non-empty
# and no default admin is created even after every real user file was deleted.
RESERVED_USER_STORE_FILES = frozenset({
    "roles.json",
    "admin_settings.json",
    "irrelevant_words.json",
    "var_presentation.json",
    "users.json",  # legacy pre-migration roster
})


def _is_candidate_user_file(filename: str) -> bool:
    """True if ``filename`` could be an individual user record (by name alone).

    A definitive answer still needs a content check (a real user file carries a
    ``username`` field); this only cheaply rules out the reserved sidecar files
    (``*_tags.json``, ``*_log.json``, and the named singletons above).
    """
    if not filename.endswith(".json"):
        return False
    if filename.endswith("_tags.json") or filename.endswith("_log.json"):
        return False
    return filename not in RESERVED_USER_STORE_FILES

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

        self._migrate_permission_keys()
        self._ensure_defaults()

    def _migrate_permission_keys(self):
        """Migrate renamed/newly-gated permission keys in every stored role.

        The "My stuff" restructure renamed ``tab.my_studies`` and put the
        previously ungated Settings/Coding pages behind ``tab.my_stuff.*``
        keys. The Admin-tab restructure split pages out of General and
        Variable Visibility into their own keys; ``PERMISSION_KEY_IMPLIED_GRANTS``
        adds the split-out keys to any role holding the old umbrella key.
        roles.json stores explicit permission lists, so without this boot-time
        migration existing roles would silently lose access.
        Idempotent — saves only when something actually changed.
        """
        from web_interface.permissions import (
            PERMISSION_KEY_IMPLIED_GRANTS,
            PERMISSION_KEY_RENAMES,
            PERMISSION_KEYS_GRANT_ALL,
        )
        changed = False
        for name, entry in self.roles.items():
            perms = entry.get("permissions", [])
            if "*" in perms:
                continue
            new_perms = []
            for p in perms:
                mapped = PERMISSION_KEY_RENAMES.get(p, p)
                if mapped not in new_perms:
                    new_perms.append(mapped)
            for key in PERMISSION_KEYS_GRANT_ALL:
                if key not in new_perms:
                    new_perms.append(key)
            for umbrella, implied in PERMISSION_KEY_IMPLIED_GRANTS.items():
                if umbrella in new_perms:
                    for key in implied:
                        if key not in new_perms:
                            new_perms.append(key)
            if new_perms != perms:
                entry["permissions"] = new_perms
                changed = True
        if changed:
            logger.info("Migrated role permission keys to the current tab layout.")
            self.save_roles()

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

        for u in user_manager_instance.get_all_users().values():
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


def validate_display_username(name) -> tuple[str | None, str | None]:
    """Validate a display username and return ``(cleaned_name, error)``.

    Display usernames are UI-only (login stays email-based) and need not be
    unique. Rules: 3–15 characters after stripping surrounding whitespace; no
    control characters; any other characters are allowed.

    Args:
        name: The raw value submitted by the user.

    Returns:
        ``(cleaned_name, None)`` on success or ``(None, error_message)``.
    """
    if not isinstance(name, str):
        return None, "Username is required."
    name = name.strip()
    if not (3 <= len(name) <= 15):
        return None, "Username must be 3-15 characters."
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return None, "Username contains invalid characters."
    return name, None


# --- User Class ---

class User(UserMixin):
    def __init__(self, username, role, password_hash, approved=True, last_login=None, settings=None, machine_annotation_votes=None, display_username=None, created_at=None, approval_notification=None):
        self.id = username
        self.username = username
        self.role = role
        self.password_hash = password_hash
        self.approved = approved
        self.last_login = last_login
        self.settings = settings if settings is not None else {}
        self.machine_annotation_votes = machine_annotation_votes if machine_annotation_votes is not None else {}
        self.display_username = display_username or ""
        self.created_at = created_at
        # Set once, when a pending-approval signup emails an admin: a
        # {"sent_to": admin_email, "sent_at": iso_timestamp} record surfaced on
        # the New Users admin page. None until (and unless) that email is sent.
        self.approval_notification = approval_notification

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
            "display_username": self.display_username,
            "role": self.role,
            "password_hash": self.password_hash,
            "approved": self.approved,
            "last_login": self.last_login,
            "created_at": self.created_at,
            "approval_notification": self.approval_notification,
            "settings": self.settings,
            "machine_annotation_votes": self.machine_annotation_votes
        }

# --- User Manager ---

class UserManager:
    def __init__(self, storage_location="users", bootstrap=True):
        """Initialize the user manager.

        The full user roster is never eagerly loaded — cold start stays O(1) in
        the number of users on every service. ``get_user()`` lazily loads a
        single user file on demand (the auth/login hot path); the full roster is
        loaded once, on first access, by ``get_all_users()`` / ``_ensure_loaded``
        (admin pages, role checks, admin-count guards).

        Args:
            storage_location: Named storage bucket / local dir in data_io.
            bootstrap: If True (default), run the one-time legacy-data migration
                and ensure a default admin exists — the responsibility of the
                web service that owns the user store. Both checks are O(1) (a
                single ``exists`` / ``listdir``), not an O(N) roster load. The
                task-runner, which only serves Cloud Tasks internal routes and
                never authenticates browser traffic, passes ``bootstrap=False``
                to skip them entirely.
        """
        self.storage_location = storage_location
        self.bootstrap = bootstrap
        self.users = {}
        self._loaded = False
        self._load_lock = threading.Lock()

        if not bootstrap:
            # Task-runner: no migration, no preload, no default admin. The web
            # service owns user data; the task-runner just needs `get_user()` to
            # work on-demand if something (unexpectedly) asks for one.
            print(
                f"[AUTH] UserManager initialized in lazy mode "
                f"(storage={self.storage_location}) — users loaded on demand",
                flush=True,
            )
            return

        # Web service owns the user store: run the one-time migration and make
        # sure an admin exists, but do NOT preload the roster — that is deferred
        # to the first `get_all_users()` so cold start stays O(1) in user count.
        self.migrate_legacy_data()
        self._ensure_default_admin()

    def _ensure_default_admin(self):
        """Create the default admin iff the store holds no user records.

        Lists the store (not an O(N) roster load) and confirms a real user by
        content — a file with a ``username`` field — so leftover sidecar files
        (var_presentation.json, irrelevant_words.json, activity logs) don't make
        an otherwise user-less store look occupied. That false-positive left a
        reset install (admin file deleted) with no way to create a new admin.
        The password is randomly generated and shown exactly once, on the
        console of the first boot — it is never written to the user store.
        """
        try:
            files = data_io.listdir(storage_location=self.storage_location, return_absolute_path=False)
        except Exception as e:
            # Be conservative: never fabricate an admin when the listing failed.
            logger.error(f"Default-admin check could not list users: {e}")
            return

        has_user = False
        for f in files:
            if not _is_candidate_user_file(f):
                continue
            try:
                data = data_io.load_json(storage_location=self.storage_location, filename=f)
            except Exception:
                # A malformed/unreadable candidate is not proof a user exists.
                continue
            if isinstance(data, dict) and data.get("username"):
                has_user = True
                break

        if not has_user:
            logger.info("No users found. Creating default admin.")
            password = secrets.token_urlsafe(12)
            self.add_user("admin@admin.net", password, ROLE_ADMIN, approved=True)
            print(
                "\n"
                "[AUTH] ============================================================\n"
                "[AUTH] First run: created the default admin account.\n"
                "[AUTH]\n"
                f"[AUTH]   username: admin@admin.net\n"
                f"[AUTH]   password: {password}\n"
                "[AUTH]\n"
                "[AUTH] This password is shown ONCE — copy it now and change it\n"
                "[AUTH] after logging in (Settings). To generate a new one, stop\n"
                "[AUTH] the app, delete users/admin@admin.net.json from your data\n"
                "[AUTH] directory, and start the app again.\n"
                "[AUTH] ============================================================\n",
                flush=True,
            )
            logger.warning(
                "Default admin admin@admin.net created with a one-time password "
                "printed to the console."
            )

    def _ensure_loaded(self):
        """Populate the full in-memory roster once, on first roster access."""
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            # Only latch as loaded on a successful listing; a transient storage
            # failure should retry on the next roster access, not cache empty.
            if self.load_users():
                self._loaded = True

    def get_all_users(self):
        """Return the full ``{username: User}`` roster, loading it once on demand.

        The first call fans out one read per user file (parallelised); later
        calls hit the in-memory cache. Consumers that need every user (admin
        pages, role-usage checks, last-admin guards) go through here so the
        roster is never preloaded at cold start.
        """
        self._ensure_loaded()
        return self.users
    
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
                        "display_username": user_data.get('display_username', ''),
                        "role": user_data.get('role', 'viewer'), # Default to viewer if missing
                        "password_hash": user_data.get('password_hash'),
                        "approved": user_data.get('approved', True),
                        "last_login": user_data.get('last_login'),
                        "created_at": user_data.get('created_at'),
                        "settings": user_data.get('settings', {})
                    }
                    
                    # Ensure defaults for settings (annotation sharing is opt-in)
                    default_settings = {
                        "share_annotations": False,
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
        """Load every user's JSON into the in-memory roster.

        GCS reads are per-file round-trips (~50ms each) so we fan them out
        across a thread pool — turning ~3.4s serial into ~0.2s for 60+ users.
        The roster is built into a local dict and swapped in atomically so a
        concurrent ``get_user`` never observes a half-populated roster.

        Returns:
            True if the storage listing succeeded (the roster is authoritative),
            False if it failed (roster left as-is — caller should not treat it
            as fully loaded).
        """
        _t_start = time.perf_counter()
        loaded = {}
        max_workers = 0
        try:
            # 1. List all .json files in storage location
            files = data_io.listdir(storage_location=self.storage_location, return_absolute_path=False)
            json_files = [f for f in files if _is_candidate_user_file(f)]

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
                    loaded[username] = User(
                        username=username,
                        role=user_data.get('role', 'viewer'),
                        password_hash=user_data.get('password_hash'),
                        approved=user_data.get('approved', True),
                        last_login=user_data.get('last_login'),
                        settings=user_data.get('settings', {}),
                        machine_annotation_votes=user_data.get('machine_annotation_votes', {}),
                        display_username=user_data.get('display_username'),
                        created_at=user_data.get('created_at'),
                        approval_notification=user_data.get('approval_notification')
                    )

            self.users = loaded
            elapsed = time.perf_counter() - _t_start
            # Use print() so the timing line reliably surfaces in Cloud Logging
            # (default root logger level is WARNING, which drops logger.info).
            print(f"[AUTH] Loaded {len(self.users)} users from {self.storage_location} in {elapsed:.2f}s (workers={max_workers})")
            return True
        except Exception as e:
            logger.error(f"Failed to list user directory: {e}")
            return False

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
            # Function-level import: data_service imports parts of the web layer,
            # so a module-level import here would create a cycle.
            from .data_service import invalidate_user_json_cache
            invalidate_user_json_cache(username)
        except Exception as e:
            logger.error(f"Failed to save user {username}: {e}")

    def get_user(self, user_id):
        """Return the User for ``user_id``, or None.

        A cache hit is a simple dict lookup. On a miss we read
        ``{user_id}.json`` from the storage location and cache the result —
        this is the auth/login hot path, so it loads exactly one user file,
        never the whole roster.
        """
        user = self.users.get(user_id)
        if user is not None:
            return user

        if isinstance(user_id, str) and user_id:
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
                display_username=user_data.get("display_username"),
                created_at=user_data.get("created_at"),
                approval_notification=user_data.get("approval_notification"),
            )
            self.users[user_id] = user
            return user

        return None

    def add_user(self, username, password, role, approved=False, display_username=None):
        if not role_manager.role_exists(role):
            return False, "Invalid role"

        # Single-user existence check (loads just this file on a cache miss) —
        # avoids preloading the whole roster just to detect a duplicate.
        if self.get_user(username) is not None:
            return False, "User already exists"

        password_hash = hash_password(password)
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        new_user = User(username, role, password_hash, approved=approved, display_username=display_username, created_at=created_at)
        # Default Settings for New Users (annotation sharing is opt-in)
        new_user.settings = {
            "share_annotations": False,
            "video_autostart": False
        }
        
        self.users[username] = new_user
        self.save_user(username)
        return True, "User created"

    def delete_user(self, username):
        # The last-admin guard counts every admin, so load the full roster once.
        self._ensure_loaded()
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

        # Drop the deleted user from the data_service per-user JSON cache so a
        # stale copy can't resurface in shared-annotation reads. Function-level
        # import to respect the auth<->data_service import cycle.
        try:
            from .data_service import invalidate_user_json_cache
            invalidate_user_json_cache(username)
        except Exception as e:
            logger.error(f"Failed to invalidate user cache for {username}: {e}")

        return True, "User deleted"
    
    def update_user_role(self, username, new_role):
        # The last-admin demotion guard counts every admin — load the full roster.
        self._ensure_loaded()
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
        user = self.get_user(username)
        if user is None:
            return False, "User not found"

        user.approved = True
        self.save_user(username)
        return True, "User approved"

    def get_oldest_admin(self):
        """Return the approved admin with the earliest ``created_at``.

        Used to pick the single administrator who is notified when a new user
        signs up while approval gating is on. Admins whose ``created_at`` is
        unknown (accounts predating the timestamp) sort after those with one, so
        a known-oldest admin always wins; ``created_at`` values are UTC ISO
        strings, which sort chronologically.

        Returns:
            The oldest approved admin :class:`User`, or ``None`` if the store
            holds no approved admin.
        """
        self._ensure_loaded()
        admins = [u for u in self.users.values() if u.role == ROLE_ADMIN and u.approved]
        if not admins:
            return None
        admins.sort(key=lambda u: (u.created_at is None, u.created_at or ""))
        return admins[0]

    def record_approval_notification(self, username, sent_to, sent_at=None):
        """Record that a pending-approval email was sent for ``username``.

        Persists a ``{"sent_to", "sent_at"}`` marker on the user so the New Users
        admin page can show when, and to which admin, the approval request was
        emailed.

        Args:
            username: The pending user the notification is about.
            sent_to: Email address the notification was sent to (the admin).
            sent_at: ISO timestamp of the send; defaults to now (UTC).

        Returns:
            ``(True, message)`` on success, ``(False, error)`` if unknown user.
        """
        user = self.get_user(username)
        if user is None:
            return False, "User not found"

        user.approval_notification = {
            "sent_to": sent_to,
            "sent_at": sent_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self.save_user(username)
        return True, "Notification recorded"

    def update_password(self, username, new_password):
        user = self.get_user(username)
        if user is None:
            return False, "User not found"

        user.password_hash = hash_password(new_password)
        self.save_user(username)
        return True, "Password updated"

    def update_last_login(self, username):
        user = self.get_user(username)
        if user is not None:
            user.last_login = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.save_user(username)

    def update_display_username(self, username, new_name):
        """Set a user's display username after validation.

        Args:
            username: The account email (user id).
            new_name: The requested display username.

        Returns:
            ``(True, message)`` on success, ``(False, error)`` otherwise.
        """
        user = self.get_user(username)
        if user is None:
            return False, "User not found"

        cleaned, error = validate_display_username(new_name)
        if error:
            return False, error

        user.display_username = cleaned
        self.save_user(username)
        return True, "Username updated"

    def update_user_settings(self, username, settings):
        user = self.get_user(username)
        if user is None:
            return False, "User not found"

        # Merge or replace? Let's generic replace for top-level keys, but maybe merge is safer?
        # For now, strict replacement of the settings dict provided
        # Or better: update existing dict with new keys
        if user.settings is None:
            user.settings = {}

        user.settings.update(settings)
        self.save_user(username)
        return True, "Settings updated"

    def register_annotation_vote(self, username, collection_id, period):
        user = self.get_user(username)
        if user is None:
            return False, "User not found"

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
        user = self.get_user(username)
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



