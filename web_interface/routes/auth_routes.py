import pandas as pd
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from datetime import datetime
from flask_login import login_user, logout_user, login_required, current_user
import web_interface.auth as auth
from ..security import user_manager
from email_validator import validate_email, EmailNotValidError
from ..mail_utils import send_welcome_email_async
from fyp.fyp_config import fyp_cf
import fyp.data_io as data_io

auth_bp = Blueprint('auth_bp', __name__)

from ..slack_service import get_recent_messages

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
    
    slack_messages = get_recent_messages()
    return render_template('login.html', slack_messages=slack_messages)

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
            flash(f"Invalid email: {str(e)}")
            return render_template('signup.html')
            
        success, msg = user_manager.add_user(username, password, auth.ROLE_VIEWER, approved=False)
        if success:
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
@auth.admin_required
def api_admin_users():
    if request.method == 'GET':
        # Return list of users (excluding password hashes)
        users_list = []
        
        # Get list of files in 'users' storage to check for annotations
        try:
            stored_files = set(data_io.listdir(storage_location = "users", return_absolute_path=False, verbose=False))
        except Exception as e:
            print(f"Error listing users directory: {e}")
            stored_files = set()

        for u in user_manager.users.values():
            ud = u.to_dict()
            del ud['password_hash']
            
            # Init stats
            ud['stats'] = {
                'notes': 0,
                'closed_tags': 0,
                'open_tags': 0,
                'unique_videos': 0,
                'used_tags': []
            }
            
            tag_filename = f"{u.username}_tags.json"
            if tag_filename in stored_files:
                try:
                    user_data = data_io.load_json(storage_location="users", filename=tag_filename)
                    if user_data:
                        notes_count = 0
                        closed_count = 0
                        open_count = 0
                        unique_videos = set()
                        used_tags = set()
                        
                        for item_id, item_vars in user_data.items():
                            has_annotation = False
                            for key, value in item_vars.items():
                                if key.endswith('__NOTES'):
                                    notes_count += 1
                                    has_annotation = True
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
                            'used_tags': sorted(list(used_tags))
                        }
                except Exception as e:
                    print(f"Error loading stats for {u.username}: {e}")

            users_list.append(ud)
        return jsonify(users_list)
        
    elif request.method == 'POST':
        data = request.json
        username = data.get('username')
        password = data.get('password')
        role = data.get('role', auth.ROLE_VIEWER)
        
        if not username or not password:
            return jsonify({"error": "Missing username or password"}), 400
            
        try:
            validate_email(username, check_deliverability=False)
        except EmailNotValidError as e:
            return jsonify({"error": f"Invalid email: {str(e)}"}), 400

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
@auth.admin_required
def api_admin_annotations():
    # item_id -> { stats: {...}, details: { variable: { open: {tag: [users]}, notes: [{user, text}], closed: {val: [users]} } } }
    master_index = {}

    try:
        stored_files = set(data_io.listdir(storage_location = "users", return_absolute_path=False, verbose=False))
    except Exception as e:
        print(f"Error listing users directory: {e}")
        return jsonify([])

    for filename in stored_files:
        if not filename.endswith('_tags.json'):
            continue
            
        username = filename.replace('_tags.json', '')
        
        try:
            user_data = data_io.load_json(storage_location="users", filename=filename)
            if not user_data: continue
            
            for item_id, item_vars in user_data.items():
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
            print(f"Error processing {filename}: {e}")

    # Convert to list and fix unique_users count
    results = []
    for item in master_index.values():
        if isinstance(item['stats']['unique_users'], set):
            item['stats']['unique_users'] = len(item['stats']['unique_users'])
        results.append(item)

    # Sort by total activity (desc)
    results.sort(key=lambda x: x['stats']['notes'] + x['stats']['open_tags'] + x['stats']['closed_tags'], reverse=True)
    
    return jsonify(results)
