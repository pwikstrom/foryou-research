import os
from datetime import datetime

import pandas as pd
from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

import fyp.data_io as data_io
import web_interface.auth as auth
from fyp.fyp_config import fyp_cf

from ..admin_settings import (
    DEFAULTS as ADMIN_SETTINGS_DEFAULTS,
    SETTING_TYPES as ADMIN_SETTING_TYPES,
    get_default_new_user_role,
    get_new_user_approval_required,
    load_admin_settings,
    save_admin_settings,
)
from ..mail_utils import send_welcome_email_async
from ..permissions import permission_required
from ..security import user_manager

auth_bp = Blueprint('auth_bp', __name__)

from ..slack_service import get_recent_messages
from ..static_content import HOME_CONTENT


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # Check if user exists first to distinguish between "Wrong password" and "Not approved"
        user_obj = user_manager.get_user(username)
        
        if user_obj:
            # Verify password first (Mitigates timing attacks by always checking password)
            if auth.verify_password(user_obj.password_hash, password):
                if not user_obj.approved:
                    flash('Your account is pending approval from an administrator.')
                else:
                    login_user(user_obj)
                    user_manager.update_last_login(user_obj.username)
                    session['login_time'] = datetime.now().strftime('%Y-%m-%d %H:%M')
                    next_page = request.args.get('next')
                    return redirect(next_page or url_for('index'))
            else:
                flash('Invalid username or password')
        else:
            # Timing attack mitigation: Perform dummy hash check
            # Use a dummy hash (random but consistent format)
            dummy_hash = "77d9c0e5a6c0e5a6c0e5a6c0e5a6c0e5a6c0e5a6c0e5a6c0e5a6c0e5a6c0e5a6" + "a" * 128
            auth.verify_password(dummy_hash, "dummy_password")
            flash('Invalid username or password')
    
    slack_configured = bool(os.environ.get("SLACK_BOT_TOKEN"))
    slack_messages = get_recent_messages() if slack_configured else []
    return render_template('login.html', slack_messages=slack_messages, slack_configured=slack_configured, content=HOME_CONTENT)

@auth_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
         return redirect(url_for('index'))
         
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if password != confirm_password:
            flash("Passwords do not match")
            return render_template('signup.html')
            
        try:
            validate_email(username, check_deliverability=False)
        except EmailNotValidError as e:
            flash(f"Invalid email: {e!s}")
            return render_template('signup.html')
            
        # Admin-controlled flag (UI-toggleable, persisted in admin_settings.json).
        require_approval = get_new_user_approval_required()

        # If approval is required, approved=False. If not required, approved=True.
        is_approved = not require_approval
        
        success, msg = user_manager.add_user(username, password, get_default_new_user_role(), approved=is_approved)
        if success:
            if is_approved:
                flash("Account created! You can now login.")
            else:
                flash("Account created! Please wait for an administrator to approve your account.")
            return redirect(url_for('auth_bp.login'))
        else:
            flash(msg)
            
    return render_template('signup.html')

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth_bp.login'))

@auth_bp.route('/api/admin/users', methods=['GET', 'POST', 'PUT', 'DELETE'])
@permission_required('tab.admin.new_users', 'tab.admin.active_users')
def api_admin_users():
    if request.method == 'GET':
        # Return list of users (excluding password hashes)
        users_list = []
        
        # We iterate through active users and attempt to load their data file directly
        # This avoids potential issues with listdir filenames vs user.username casing
        
        for u in user_manager.users.values():
            ud = u.to_dict()
            del ud['password_hash']
            
            # Init stats
            ud['stats'] = {
                'notes': 0,
                'closed_tags': 0,
                'open_tags': 0,
                'unique_videos': 0,
                'used_tags': [],
                'user_notes': []
            }
            
            # Refactored for single file structure
            user_filename = f"{u.username}.json"
            
            # Try to load file directly (data_io.load_json returns None if missing/fail)
            try:
                #print(f"[DEBUG] Attempting to load {user_filename} for user {u.username}")
                user_data_file = data_io.load_json(storage_location="users", filename=user_filename)
                
                # Try lowercase if failed
                if not user_data_file:
                     #print(f"[DEBUG] Failed to load {user_filename}, trying lowercase...")
                     user_filename_lower = f"{u.username.lower()}.json"
                     user_data_file = data_io.load_json(storage_location="users", filename=user_filename_lower)

                if user_data_file:
                    #print(f"[DEBUG] Successfully loaded data for {u.username}")
                    user_annotations = user_data_file.get('annotations', {})
                    
                    notes_count = 0
                    closed_count = 0
                    open_count = 0
                    unique_videos = set()
                    used_tags = set()
                    user_notes = [] # List of {item_id: text}
                    
                    for item_id, item_vars in user_annotations.items():
                        has_annotation = False
                        for key, value in item_vars.items():
                            if key.endswith('__NOTES'):
                                notes_count += 1
                                has_annotation = True
                                user_notes.append({'item': item_id, 'text': value})
                            elif key.endswith('__CLOSED_TAGGING'):
                                closed_count += 1
                                has_annotation = True
                            else:
                                # Open Tags
                                if isinstance(value, list) and value:
                                    open_count += len(value)
                                    used_tags.update(value)
                                    has_annotation = True
                        
                        if has_annotation:
                            unique_videos.add(item_id)
                    
                    ud['stats'] = {
                        'notes': notes_count,
                        'closed_tags': closed_count,
                        'open_tags': open_count,
                        'unique_videos': len(unique_videos),
                        'used_tags': sorted(list(used_tags)),
                        'user_notes': user_notes
                    }
            except Exception as e:
                print(f"Error loading stats for {u.username}: {e}")

            users_list.append(ud)
        return jsonify(users_list)
        
    elif request.method == 'POST':
        data = request.json
        username = data.get('username')
        password = data.get('password')
        # Role is no longer admin-selectable per user; the configured default
        # role applies to everyone (signups and admin-created users alike).
        role = get_default_new_user_role()

        if not username or not password:
            return jsonify({"error": "Missing username or password"}), 400

        try:
            validate_email(username, check_deliverability=False)
        except EmailNotValidError as e:
            return jsonify({"error": f"Invalid email: {e!s}"}), 400

        success, msg = user_manager.add_user(username, password, role, approved=True)
        if success:
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"error": msg}), 400

    elif request.method == 'PUT':
        data = request.json
        action = data.get('action')
        username = data.get('username')
        
        if not username:
             return jsonify({"error": "Missing username"}), 400
             
        if action == 'approve':
             success, msg = user_manager.approve_user(username)
             if success: 
                 send_welcome_email_async(username)
                 return jsonify({"status": "success", "message": msg})
             else: return jsonify({"error": msg}), 400
             
        elif action == 'reset_password':
             new_password = data.get('new_password')
             if not new_password: return jsonify({"error": "Missing new password"}), 400
             
             success, msg = user_manager.update_password(username, new_password)
             if success: return jsonify({"status": "success", "message": msg})
             else: return jsonify({"error": msg}), 400
             
        elif action == 'change_role':
             new_role = data.get('role')
             success, msg = user_manager.update_user_role(username, new_role)
             if success: return jsonify({"status": "success", "message": msg})
             else: return jsonify({"error": msg}), 400

        return jsonify({"error": "Invalid action"}), 400

    elif request.method == 'DELETE':
        username = request.args.get('username')
        
        if not username:
             return jsonify({"error": "Missing username"}), 400

        success, msg = user_manager.delete_user(username)
        if success:
             return jsonify({"status": "success", "message": msg})
        else:
             return jsonify({"error": msg}), 400

@auth_bp.route('/api/admin/roles', methods=['GET', 'POST', 'DELETE'])
# GET is needed by any admin sub-page that lists or picks roles: New Users
# (default-role label), Active Users (per-user role dropdown), General
# (default-role setting dropdown), and Roles itself (the matrix UI).
# Write methods (POST/DELETE) are still restricted to tab.admin.roles below.
@permission_required('tab.admin.roles', 'tab.admin.active_users', 'tab.admin.new_users', 'tab.admin.general')
def api_admin_roles():
    from ..permissions import user_has_permission

    if request.method == 'GET':
        return jsonify(auth.role_manager.get_roles_with_permissions())

    # POST / DELETE manage the role catalog itself — only the Roles sub-page.
    if not user_has_permission(current_user, 'tab.admin.roles'):
        return jsonify({"error": "Forbidden"}), 403

    if request.method == 'POST':
        data = request.json
        role_name = data.get('role_name')
        if not role_name:
             return jsonify({"error": "Missing role name"}), 400

        role_name = role_name.strip().lower()

        success, msg = auth.role_manager.add_role(role_name)
        if success:
             return jsonify({"status": "success", "message": msg})
        else:
             return jsonify({"error": msg}), 400

    elif request.method == 'DELETE':
        role_name = request.args.get('role_name')
        if not role_name:
             return jsonify({"error": "Missing role name"}), 400

        success, msg = auth.role_manager.delete_role(role_name, user_manager)
        if success:
             return jsonify({"status": "success", "message": msg})
        else:
             return jsonify({"error": msg}), 400


@auth_bp.route('/api/admin/permissions/catalog', methods=['GET'])
@permission_required('tab.admin.roles')
def api_admin_permissions_catalog():
    from ..permissions import PERMISSION_CATALOG
    return jsonify(PERMISSION_CATALOG)


@auth_bp.route('/api/admin/roles/<role_name>/permissions', methods=['PUT'])
@permission_required('tab.admin.roles')
def api_admin_role_permissions(role_name):
    from ..permissions import ALL_PERMISSION_KEYS

    data = request.json or {}
    perms = data.get('permissions')
    if not isinstance(perms, list):
        return jsonify({"error": "Body must contain a 'permissions' list"}), 400

    invalid = [p for p in perms if p not in ALL_PERMISSION_KEYS]
    if invalid:
        return jsonify({"error": f"Unknown permission keys: {invalid}"}), 400

    success, msg = auth.role_manager.set_role_permissions(role_name, perms)
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"error": msg}), 400

@auth_bp.route('/api/admin/settings', methods=['GET', 'PUT'])
# GET is also useful to the New Users sub-page (it needs to display the
# configured default role for new signups). PUT stays restricted to General
# via the method-specific check below.
@permission_required('tab.admin.general', 'tab.admin.new_users')
def api_admin_settings():
    if request.method == 'GET':
        merged = {**ADMIN_SETTINGS_DEFAULTS, **load_admin_settings()}
        return jsonify({"settings": merged})

    # PUT — only the General sub-page can write settings.
    from ..permissions import user_has_permission
    if not user_has_permission(current_user, 'tab.admin.general'):
        return jsonify({"error": "Forbidden"}), 403

    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    allowed_keys = set(ADMIN_SETTINGS_DEFAULTS.keys())
    unknown = [k for k in data if k not in allowed_keys]
    if unknown:
        return jsonify({"error": f"Unknown settings: {unknown}"}), 400

    # Per-key type validation. Unknown-type keys default to bool to preserve
    # the historical contract for any legacy boolean flag.
    for k, v in data.items():
        expected = ADMIN_SETTING_TYPES.get(k, bool)
        if not isinstance(v, expected):
            return jsonify({"error": f"Setting '{k}' must be a {expected.__name__}"}), 400
        # Extra check: the default-role setting must reference an existing role.
        if k == "default_new_user_role" and not auth.role_manager.role_exists(v):
            return jsonify({"error": f"Unknown role: {v!r}"}), 400

    current = load_admin_settings()
    current.update(data)
    save_admin_settings(current)

    merged = {**ADMIN_SETTINGS_DEFAULTS, **current}
    return jsonify({"status": "success", "message": "Settings updated", "settings": merged})


@auth_bp.route('/api/user/settings', methods=['GET', 'POST'])
@login_required
def api_user_settings():
    if request.method == 'GET':
        s = current_user.settings or {}
        if 'share_annotations' not in s:
            s['share_annotations'] = True
        return jsonify(s)
    
    elif request.method == 'POST':
        settings = request.json
        success, msg = user_manager.update_user_settings(current_user.username, settings)
        if success:
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"error": msg}), 400

@auth_bp.route('/api/admin/annotations', methods=['GET'])
@permission_required('tab.admin.annotations')
def api_admin_annotations():
    # item_id -> { stats: {...}, details: { variable: { open: {tag: [users]}, notes: [{user, text}], closed: {val: [users]} } } }
    master_index = {}

    # Iterate through all known users instead of listing files to avoid casing/sync issues
    for u in user_manager.users.values():
        username = u.username
        user_filename = f"{username}.json"
        
        try:
            user_data_file = data_io.load_json(storage_location="users", filename=user_filename)
            if not user_data_file: continue
            
            user_annotations = user_data_file.get('annotations', {})
            
            for item_id, item_vars in user_annotations.items():
                if item_id not in master_index:
                    master_index[item_id] = {
                        'item_id': item_id,
                        'stats': {'notes': 0, 'open_tags': 0, 'closed_tags': 0, 'unique_users': set()},
                        'details': {}
                    }
                
                entry = master_index[item_id]
                entry['stats']['unique_users'].add(username)
                
                for key, value in item_vars.items():
                    # Check types
                    var_name = key
                    type_ = 'open'
                    
                    if key.endswith('__NOTES'):
                        var_name = key[:-7] # remove __NOTES
                        type_ = 'note'
                    elif key.endswith('__CLOSED_TAGGING'):
                        var_name = key[:-16] # remove __CLOSED_TAGGING
                        type_ = 'closed'
                        
                    if var_name not in entry['details']:
                        # Resolve Friendly Name
                        friendly_name = var_name
                        if 'var_schema' in fyp_cf:
                            df = fyp_cf['var_schema']
                            if isinstance(df, pd.DataFrame):
                                match = df[df['variable_name'] == var_name]
                                if not match.empty:
                                    try:
                                        sec = match['section'].iloc[0]
                                        disp = match['display_name'].iloc[0]
                                        
                                        if pd.isna(sec): sec = "Unknown"
                                        if pd.isna(disp): disp = var_name
                                        
                                        friendly_name = f"{sec} - {disp}"
                                    except:
                                        pass

                            
                        entry['details'][var_name] = {
                            'label': friendly_name,
                            'open': {}, 
                            'notes': [], 
                            'closed': {}
                        }
                        
                    det = entry['details'][var_name]
                    
                    if type_ == 'note':
                        det['notes'].append({'user': username, 'text': value})
                        entry['stats']['notes'] += 1
                        
                    elif type_ == 'closed':
                        val_str = str(value)
                        if val_str not in det['closed']: det['closed'][val_str] = []
                        det['closed'][val_str].append(username)
                        entry['stats']['closed_tags'] += 1
                        
                    else:
                        # Open tags list
                        if isinstance(value, list):
                            for tag in value:
                                if tag not in det['open']: det['open'][tag] = []
                                det['open'][tag].append(username)
                                entry['stats']['open_tags'] += 1

        except Exception as e:
            print(f"Error processing {username}: {e}")

    # Convert to list and fix unique_users count
    results = []
    for item in master_index.values():
        if isinstance(item['stats']['unique_users'], set):
            item['stats']['unique_users'] = len(item['stats']['unique_users'])
        results.append(item)

    # Sort by total activity (desc)
    results.sort(key=lambda x: x['stats']['notes'] + x['stats']['open_tags'] + x['stats']['closed_tags'], reverse=True)
    
    return jsonify(results)
