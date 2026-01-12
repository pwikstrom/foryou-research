from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_user, logout_user, login_required, current_user
import web_interface.auth as auth
from ..security import user_manager

auth_bp = Blueprint('auth_bp', __name__)

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
    
    return render_template('login.html')

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
        for u in user_manager.users.values():
            ud = u.to_dict()
            del ud['password_hash']
            users_list.append(ud)
        return jsonify(users_list)
        
    elif request.method == 'POST':
        data = request.json
        username = data.get('username')
        password = data.get('password')
        role = data.get('role', auth.ROLE_VIEWER)
        
        if not username or not password:
            return jsonify({"error": "Missing username or password"}), 400
            
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
             if success: return jsonify({"status": "success", "message": msg})
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
