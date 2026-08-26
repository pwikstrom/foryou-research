import os
from datetime import datetime, timezone

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
    get_session_floors,
    load_admin_settings,
    save_admin_settings,
    study_names as admin_study_names,
    validate_setting_value,
)
from .. import activity_log
from ..collection_accounts import (
    collections_for_user,
    load_owner_map,
    orphan_placeholder_accounts,
    unlink_user,
)
from ..mail_utils import is_email, send_new_user_pending_email_async, send_welcome_email_async
from ..permissions import permission_required
from ..security import user_manager

auth_bp = Blueprint('auth_bp', __name__)

from ..slack_service import get_recent_messages


def _safe_next(target: str | None) -> str | None:
    """Restrict a ``?next=`` redirect target to same-site relative paths.

    An absolute URL (or a scheme-relative ``//host`` one) in ``next`` would let
    a crafted login link bounce a fresh session to an attacker's site.
    """
    if target and target.startswith('/') and not target.startswith('//'):
        return target
    return None


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
                    # Lazy provisioning of the participant study pair: most
                    # donation-linked accounts never log in, so the pair is
                    # created on first login rather than for every owner.
                    # Idempotent and cheap; must run AFTER update_last_login
                    # (the dormancy gate reads it) and never fails the login.
                    from ..services.participant_studies import ensure_on_login
                    ensure_on_login(user_obj.username)
                    session['login_time'] = datetime.now(timezone.utc).isoformat()
                    next_page = _safe_next(request.args.get('next'))
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
    return render_template('login.html', slack_messages=slack_messages, slack_configured=slack_configured)

@auth_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
         return redirect(url_for('index'))
         
    # Kept through the whole signup → login round trip so a funnel entry
    # (e.g. the participation wizard's "?next=/participate/go-upload") still
    # lands where it intended after the account exists.
    next_target = _safe_next(request.form.get('next') or request.args.get('next'))

    if request.method == 'POST':
        username = request.form.get('username')
        display_username = request.form.get('display_username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not request.form.get('accept_terms'):
            flash("Please accept the terms of use to create an account")
            return render_template('signup.html', next_target=next_target)

        if password != confirm_password:
            flash("Passwords do not match")
            return render_template('signup.html', next_target=next_target)

        try:
            validate_email(username, check_deliverability=False)
        except EmailNotValidError as e:
            flash(f"Invalid email: {e!s}")
            return render_template('signup.html', next_target=next_target)

        cleaned_display, display_err = auth.validate_display_username(display_username)
        if display_err:
            flash(display_err)
            return render_template('signup.html', next_target=next_target)

        # Admin-controlled flag (UI-toggleable, persisted in admin_settings.json).
        require_approval = get_new_user_approval_required()

        # If approval is required, approved=False. If not required, approved=True.
        is_approved = not require_approval

        # A participant account created from donation data has no password.
        # Signing up with that email claims it — the profile and linked
        # collections stay, the person gains a login — instead of bouncing
        # on "User already exists".
        terms_accepted_at = datetime.now(timezone.utc).isoformat()

        existing = user_manager.find_user_by_email(username)
        if existing is not None and not existing.can_login() and not existing.placeholder:
            success, msg = user_manager.claim_participant_account(
                existing.username, password, cleaned_display, approved=is_approved,
                terms_accepted_at=terms_accepted_at)
            username = existing.username
        else:
            success, msg = user_manager.add_user(
                username, password, get_default_new_user_role(), approved=is_approved,
                display_username=cleaned_display,
                origin={"source": "signup", "at": datetime.now(timezone.utc).isoformat()},
                terms_accepted_at=terms_accepted_at)
        if success:
            if next_target and next_target.startswith('/participate'):
                # Funnel-origin signup: queue the guided tour for the first
                # visit to the app shell (index.html checks this setting).
                user_manager.update_user_settings(username, {"hub_tour_pending": True})
            if is_approved:
                flash("Account created! You can now login.")
            else:
                flash("Account created! Please wait for an administrator to approve your account.")
                # Approval gating is on: email the oldest admin so they know a
                # request is waiting, and stamp the pending user once it sends.
                _notify_admin_of_pending_signup(username, cleaned_display)
            return redirect(url_for('auth_bp.login', next=next_target) if next_target
                            else url_for('auth_bp.login'))
        else:
            flash(msg)

    return render_template('signup.html', next_target=next_target)


@auth_bp.route('/api/signup/email-check')
def api_signup_email_check():
    """Tell the signup form whether an email can still register.

    Called on blur of the email field so a duplicate is caught before the
    visitor fills in the rest of the form. Three answers: ``available``
    (no account), ``claimable`` (a passwordless participant account exists and
    signing up will claim it, keeping its linked collections), ``taken``
    (an account that can already log in).
    """
    email = (request.args.get('email') or '').strip()
    if not email:
        return jsonify({"status": "available"})
    existing = user_manager.find_user_by_email(email)
    if existing is None:
        return jsonify({"status": "available"})
    if not existing.can_login() and not existing.placeholder:
        return jsonify({"status": "claimable"})
    return jsonify({"status": "taken"})


def _notify_admin_of_pending_signup(new_username: str, new_display: str | None) -> None:
    """Email the oldest admin that ``new_username`` is awaiting approval.

    Fire-and-forget: the send runs in a background thread so signup stays
    responsive, and the sent-at / sent-to marker is recorded on the pending user
    only when the email actually goes out (accurate even when MAIL_PASSWORD is
    unset in local dev). A no-op if no emailable admin exists.

    Args:
        new_username: Email (account id) of the just-created pending user.
        new_display: The new user's chosen display name, if any.
    """
    admin = user_manager.get_oldest_admin()
    if admin is None or not is_email(admin.username):
        return
    admin_email = admin.username
    send_new_user_pending_email_async(
        to_email=admin_email,
        new_user_email=new_username,
        new_user_display=new_display,
        on_success=lambda: user_manager.record_approval_notification(
            new_username, sent_to=admin_email),
    )

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    # Land on the home page: for a now-anonymous visitor, index() renders the
    # public landing page, which has the Log in item in its top menu.
    return redirect(url_for('index'))

@auth_bp.route('/api/admin/users', methods=['GET', 'POST', 'PUT', 'DELETE'])
@permission_required('tab.admin.new_users', 'tab.admin.active_users')
def api_admin_users():
    if request.method == 'GET':
        # Return list of users (excluding password hashes)
        users_list = []
        
        # We iterate through active users and attempt to load their data file directly
        # This avoids potential issues with listdir filenames vs user.username casing

        # Collections linked to each account, from the collections sidecar.
        owned: dict[str, list[str]] = {}
        for cid, uid in load_owner_map().items():
            if uid:
                owned.setdefault(uid, []).append(cid)

        for u in user_manager.get_all_users().values():
            ud = u.to_dict()
            del ud['password_hash']
            ud['can_login'] = u.can_login()
            ud['collections'] = sorted(owned.get(u.username, []))
            ud['collections_count'] = len(ud['collections'])

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
        display_username = data.get('display_username')
        password = data.get('password')
        # Role is no longer admin-selectable per user; the configured default
        # role applies to everyone (signups and admin-created users alike).
        role = get_default_new_user_role()

        if not username or not password:
            return jsonify({"error": "Missing email or password"}), 400

        try:
            validate_email(username, check_deliverability=False)
        except EmailNotValidError as e:
            return jsonify({"error": f"Invalid email: {e!s}"}), 400

        cleaned_display, display_err = auth.validate_display_username(display_username)
        if display_err:
            return jsonify({"error": display_err}), 400

        success, msg = user_manager.add_user(
            username, password, role, approved=True, display_username=cleaned_display,
            origin={"source": "admin", "at": datetime.now(timezone.utc).isoformat(),
                    "by": current_user.username})
        if success:
            activity_log.record(
                actor=current_user.username,
                category=activity_log.CATEGORY_USER_MANAGEMENT,
                action="user.create",
                target=username,
                details={"role": role},
            )
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
                 activity_log.record(
                     actor=current_user.username,
                     category=activity_log.CATEGORY_USER_MANAGEMENT,
                     action="user.approve",
                     target=username,
                 )
                 return jsonify({"status": "success", "message": msg})
             else: return jsonify({"error": msg}), 400

        elif action == 'reset_password':
             new_password = data.get('new_password')
             if not new_password: return jsonify({"error": "Missing new password"}), 400

             success, msg = user_manager.update_password(username, new_password)
             if success:
                 activity_log.record(
                     actor=current_user.username,
                     category=activity_log.CATEGORY_USER_MANAGEMENT,
                     action="user.reset_password",
                     target=username,
                 )
                 return jsonify({"status": "success", "message": msg})
             else: return jsonify({"error": msg}), 400

        elif action == 'set_display_username':
             prev_user = user_manager.get_user(username)
             old_name = prev_user.display_username if prev_user else None
             success, msg = user_manager.update_display_username(username, data.get('display_username'))
             if success:
                 activity_log.record(
                     actor=current_user.username,
                     category=activity_log.CATEGORY_USER_MANAGEMENT,
                     action="user.set_display_username",
                     target=username,
                     details={"from": old_name, "to": data.get('display_username')},
                 )
                 return jsonify({"status": "success", "message": msg})
             else: return jsonify({"error": msg}), 400

        elif action == 'change_role':
             new_role = data.get('role')
             # Capture the previous role before mutation so the log can show
             # both old and new values.
             prev_user = user_manager.get_user(username)
             old_role = prev_user.role if prev_user else None
             success, msg = user_manager.update_user_role(username, new_role)
             if success:
                 activity_log.record(
                     actor=current_user.username,
                     category=activity_log.CATEGORY_USER_MANAGEMENT,
                     action="user.change_role",
                     target=username,
                     details={"from": old_role, "to": new_role},
                 )
                 return jsonify({"status": "success", "message": msg})
             else: return jsonify({"error": msg}), 400

        elif action == 'set_profile':
             success, msg = user_manager.update_profile(username, data.get('profile'))
             if success:
                 activity_log.record(
                     actor=current_user.username,
                     category=activity_log.CATEGORY_USER_MANAGEMENT,
                     action="user.set_profile",
                     target=username,
                     details={"fields": sorted((data.get('profile') or {}).keys())},
                 )
                 return jsonify({"status": "success", "message": msg})
             else: return jsonify({"error": msg}), 400

        return jsonify({"error": "Invalid action"}), 400

    elif request.method == 'DELETE':
        username = request.args.get('username')
        cascade = request.args.get('cascade_collections', '0').strip().lower() in ('1', 'true', 'yes')

        if not username:
             return jsonify({"error": "Missing username"}), 400
        if user_manager.get_user(username) is None:
             return jsonify({"error": "User not found"}), 400

        # The account's collections are unlinked FIRST so no link ever points
        # at a username that no longer exists. With cascade the collections
        # are then deleted by the same task the Edit Collections page uses
        # (the participant-withdrawal case).
        unlinked = unlink_user(username)
        success, msg = user_manager.delete_user(username)
        if not success:
             # Re-link: the delete was refused (e.g. last admin), so the
             # account still exists and should keep its collections.
             from ..collection_accounts import set_collection_owner
             for cid in unlinked:
                 set_collection_owner(cid, username)
             return jsonify({"error": msg}), 400

        activity_log.record(
            actor=current_user.username,
            category=activity_log.CATEGORY_USER_MANAGEMENT,
            action="user.delete",
            target=username,
            details={"unlinked_collections": unlinked, "cascade": cascade},
        )

        # Remove the deleted account's auto-managed study pair (it now owns
        # nothing, so the sync deletes both defs and the Just Me artifacts).
        try:
            from ..services.participant_studies import ensure_participant_studies
            ensure_participant_studies(username)
        except Exception as exc:
            print(f"[delete_user] participant-study cleanup failed (non-fatal): {exc}")

        result = {"status": "success", "message": msg, "unlinked_collections": unlinked}
        if cascade and unlinked:
            from fyp.fyp_config import COLLECTION_DELETE_SCRIPT
            from ..process_manager import start_process
            ok, pmsg = start_process(
                "collection_delete",
                COLLECTION_DELETE_SCRIPT,
                task_args={"collection_ids": unlinked},
                started_by=current_user.username,
            )
            result["cascade"] = {"started": ok, "message": pmsg, "collection_ids": unlinked}
            if ok:
                activity_log.record(
                    actor=current_user.username,
                    category=activity_log.CATEGORY_DATA_MANAGEMENT,
                    action="collection.delete",
                    target=", ".join(unlinked),
                    details={"reason": f"cascade from deleting user {username}"},
                )
            else:
                result["message"] = (f"{msg}. Collections were unlinked but the delete task could not "
                                     f"start: {pmsg}. Delete them from Edit Collections.")
        return jsonify(result)


@auth_bp.route('/api/admin/users/orphan_participants', methods=['GET', 'POST'])
@permission_required('tab.admin.active_users')
def api_admin_orphan_participants():
    """Placeholder participant accounts (p-N@…) that own no collection.

    GET lists them; POST deletes them. A placeholder exists only to hold the
    demographics that came with a donation, so once its collections are gone
    it is dead weight — but removal stays an explicit admin action.
    """
    orphans = orphan_placeholder_accounts()
    if request.method == 'GET':
        return jsonify({"orphans": orphans})
    removed, failed = [], []
    for username in orphans:
        ok, msg = user_manager.delete_user(username)
        (removed if ok else failed).append(username if ok else f"{username}: {msg}")
    if removed:
        activity_log.record(
            actor=current_user.username,
            category=activity_log.CATEGORY_USER_MANAGEMENT,
            action="user.cleanup_orphan_participants",
            target=", ".join(removed),
        )
    return jsonify({"status": "success", "removed": removed, "failed": failed})


@auth_bp.route('/api/admin/users/<path:username>/log', methods=['GET'])
@permission_required('tab.admin.active_users')
def api_admin_user_log(username):
    """Return the activity log for the given user (newest first)."""
    entries = activity_log.read(username)
    return jsonify({"entries": entries})

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
# configured default role for new signups) and the Backends sub-page (the
# active backend selections live in the settings store). PUT is restricted
# per key via the method-specific check below.
@permission_required('tab.admin.general', 'tab.admin.new_users', 'tab.admin.backends')
def api_admin_settings():
    if request.method == 'GET':
        merged = {**ADMIN_SETTINGS_DEFAULTS, **load_admin_settings()}
        # Session floors fall back to the [sessions] config seed, not to
        # DEFAULTS — report what the server will actually apply, or the admin
        # page shows a number the Sessions tab is not using.
        merged.update(get_session_floors())
        from fyp.annotation.backends import BACKEND_IDS, implemented_backend_ids
        payload = {"settings": merged,
                   "backend_ids": list(BACKEND_IDS),
                   "implemented_backends": list(implemented_backend_ids())}
        # Choices for the default-study picker. Only Site Settings holders get
        # them — the other two sub-pages that may read this endpoint have no
        # business learning every study name.
        from ..permissions import user_has_permission
        if user_has_permission(current_user, 'tab.admin.general'):
            payload["study_names"] = admin_study_names()
            from ..admin_settings import demo_collection_choices
            payload["demo_collection_choices"] = demo_collection_choices()
        return jsonify(payload)

    # PUT — the backend selections belong to the Backends sub-page, every
    # other setting to Site Settings (tab.admin.general).
    from ..permissions import user_has_permission

    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    allowed_keys = set(ADMIN_SETTINGS_DEFAULTS.keys())
    unknown = [k for k in data if k not in allowed_keys]
    if unknown:
        return jsonify({"error": f"Unknown settings: {unknown}"}), 400

    _BACKEND_SETTING_KEYS = {"annotation_backend", "embedding_backend"}
    for k in data:
        required = 'tab.admin.backends' if k in _BACKEND_SETTING_KEYS else 'tab.admin.general'
        if not user_has_permission(current_user, required):
            return jsonify({"error": "Forbidden"}), 403

    # Per-key type validation. Unknown-type keys default to bool to preserve
    # the historical contract for any legacy boolean flag.
    for k, v in data.items():
        expected = ADMIN_SETTING_TYPES.get(k, bool)
        if not isinstance(v, expected):
            names = "/".join(t.__name__ for t in (expected if isinstance(expected, tuple) else (expected,)))
            return jsonify({"error": f"Setting '{k}' must be a {names}"}), 400
        # Extra check: the default-role setting must reference an existing role.
        if k == "default_new_user_role" and not auth.role_manager.role_exists(v):
            return jsonify({"error": f"Unknown role: {v!r}"}), 400
        semantic_error = validate_setting_value(k, v)
        if semantic_error:
            return jsonify({"error": semantic_error}), 400

    current = load_admin_settings()
    prev_annotation_backend = current.get(
        "annotation_backend", ADMIN_SETTINGS_DEFAULTS.get("annotation_backend"))
    prev_default_study = current.get(
        "default_study", ADMIN_SETTINGS_DEFAULTS.get("default_study"))
    current.update(data)
    save_admin_settings(current)

    # The default study is readable by every logged-in user, so changing it
    # widens (or narrows) data access — leave a trail.
    if "default_study" in data and data["default_study"] != prev_default_study:
        activity_log.record(
            actor=getattr(current_user, "username", "") or "",
            category="admin",
            action="default_study.change",
            target=data["default_study"] or "",
            details={"from": prev_default_study or "", "to": data["default_study"] or ""},
        )

    # A backend switch forks the effective annotation version — register it
    # eagerly so it shows on the Versions page without waiting for the first
    # annotation run, and report it back so the Backends page can tell the
    # admin what just changed. Never let registry plumbing fail the save.
    switch_info = None
    if ("annotation_backend" in data
            and data["annotation_backend"] != prev_annotation_backend):
        switch_info = {"from": prev_annotation_backend,
                       "to": data["annotation_backend"],
                       "annotation_version": None}
        try:
            from fyp import annotation_versioning

            minted = annotation_versioning.ensure_active_version_registered()
            switch_info["annotation_version"] = minted
            activity_log.record(
                actor=getattr(current_user, "username", "") or "",
                category="admin",
                action="annotation_backend.switch",
                details={"from": prev_annotation_backend,
                         "to": data["annotation_backend"],
                         "annotation_version": minted},
            )
        except Exception:
            pass

    merged = {**ADMIN_SETTINGS_DEFAULTS, **current}
    payload = {"status": "success", "message": "Settings updated", "settings": merged}
    if switch_info:
        payload["annotation_backend_switch"] = switch_info
    return jsonify(payload)


@auth_bp.route('/api/admin/irrelevant_words', methods=['GET', 'PUT'])
@permission_required('tab.admin.stoplist')
def api_irrelevant_words():
    """The admin-editable hashtag stoplist (see ``fyp.irrelevant_words``).

    GET returns the current list (seeding the store from config.toml on first
    access). PUT replaces the whole list; body ``{"words": [...], "etag": ...}``
    — refuses with 409 on a stale etag (concurrent edit) and 400 on invalid
    entries. Edits apply when hashtags are next extracted (scrape/annotation);
    already-stored hashtags are unchanged.
    """
    from fyp import irrelevant_words as iw

    if request.method == 'GET':
        words = iw.load_words()
        payload = iw.load_payload() or {}
        return jsonify({
            "words": words,
            "count": len(words),
            "etag": iw.compute_words_etag(),
            "updated_at": payload.get("updated_at"),
            "updated_by": payload.get("updated_by"),
        })

    data = request.json or {}
    if not isinstance(data, dict) or not isinstance(data.get('words'), list):
        return jsonify({"error": "Body must contain a 'words' list"}), 400

    try:
        result = iw.save_words(
            data['words'],
            expected_etag=data.get('etag'),
            updated_by=current_user.username,
        )
    except iw.IrrelevantWordsConflict as e:
        return jsonify({
            "error": "conflict",
            "message": str(e),
            "etag": iw.compute_words_etag(),
            "words": iw.load_words(),
        }), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    activity_log.record(
        actor=current_user.username,
        category="admin",
        action="irrelevant_words.save",
        details={"count": len(result["words"])},
    )
    return jsonify({
        "status": "success",
        "words": result["words"],
        "count": len(result["words"]),
        "etag": result["etag"],
    })


@auth_bp.route('/api/admin/irrelevant_words/apply', methods=['POST'])
@permission_required('tab.admin.stoplist')
def api_irrelevant_words_apply():
    """Start the background job that re-applies the stoplist to existing data.

    Re-tokenises the stored ``desc_hashtags`` from the preserved captions across
    the source scrape parquets with the current stoplist (see
    ``run_retokenise_hashtags``). Clean-only — the response tells the caller to
    run a forced full reconsolidation afterward. Refuses (409) while a scraper/annotator/
    consolidation is running, since it rewrites the same scrape parquets.
    """
    from fyp.fyp_config import RETOKENISE_HASHTAGS_SCRIPT

    from ..process_manager import start_process
    from .management_routes import _is_worker_running, _workers_blocking_consolidate

    if _is_worker_running("retokenise_hashtags"):
        return jsonify({"status": "error", "message": "Already running"}), 409

    blocking = _workers_blocking_consolidate()
    if _is_worker_running("consolidate_enrichment"):
        blocking.append("consolidate_enrichment")
    if blocking:
        return jsonify({
            "status": "error",
            "message": f"Cannot run while {', '.join(blocking)} running.",
        }), 409

    success, msg = start_process("retokenise_hashtags", RETOKENISE_HASHTAGS_SCRIPT,
                                 started_by=current_user.username)
    if success:
        activity_log.record(
            actor=current_user.username,
            category="admin",
            action="irrelevant_words.apply",
        )
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409


# The closed set of user-settings keys the POST endpoint accepts. The store
# itself is schemaless, so this whitelist is the only guard against arbitrary
# unbounded keys landing in a user record.
USER_SETTINGS_KEYS = frozenset({
    "variable_prefs",
    "share_annotations",
    "video_autostart",
    "getting_started_dismissed",
    "hub_tour_pending",
    "hub_tour_done",
    "hub_tour_real_data_pending",
    "funnel_stage",
    "big_dots",
    "timelines_include_empty_dates",
    "timelines_include_pre_activity",
})


@auth_bp.route('/api/user/settings', methods=['GET', 'POST'])
@login_required
def api_user_settings():
    if request.method == 'GET':
        s = current_user.settings or {}
        if 'share_annotations' not in s:
            # Annotation sharing is opt-in: an unset value reads as off.
            s['share_annotations'] = False
        return jsonify(s)
    
    elif request.method == 'POST':
        settings = request.json
        if not isinstance(settings, dict):
            return jsonify({"error": "Settings must be a JSON object"}), 400
        unknown = sorted(set(settings) - USER_SETTINGS_KEYS)
        if unknown:
            return jsonify({"error": f"Unknown settings keys: {', '.join(unknown)}"}), 400
        if 'variable_prefs' in settings:
            err = _validate_variable_prefs(settings['variable_prefs'])
            if err:
                return jsonify({"error": err}), 400
        success, msg = user_manager.update_user_settings(current_user.username, settings)
        if success:
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"error": msg}), 400






@auth_bp.route('/api/user/variable-catalog', methods=['GET'])
@login_required
def api_user_variable_catalog():
    """Study-independent variable catalog for the My Stuff preference panels.

    Everything the "Customize variables" panels need — the canonical variable
    order, the four global per-surface ON lists, and the schema map with
    sections/display names — comes from the synthesized var_schema, so it is
    the same for every study. The tabs used to supply this from their loaded
    study metadata; My Stuff has no study loaded, hence this endpoint.
    """
    from ..data_service import load_schema_metadata, make_serializable

    meta = load_schema_metadata({})
    catalog = {
        "all_variables_order": meta.get("all_variables_order") or [],
        "filter_priority": meta.get("filter_priority") or [],
        "viz_priority": meta.get("viz_priority") or [],
        "display_priority": meta.get("display_priority") or [],
        "timeline_priority": meta.get("timeline_priority") or [],
        "section_order": meta.get("section_order") or [],
        "schema_map": meta.get("schema_map") or {},
    }
    return jsonify(make_serializable(catalog))


@auth_bp.route('/api/user/profile', methods=['GET', 'POST'])
@permission_required('tab.my_stuff.profile')
def api_user_profile():
    """Read or update the current user's own profile.

    The email (account id) is immutable and returned read-only. POST accepts
    ``display_username`` and/or a ``profile`` object (see ``PROFILE_FIELDS``);
    either may be omitted to leave it unchanged.
    """
    if request.method == 'GET':
        return jsonify({
            "email": current_user.username,
            "display_username": current_user.display_username,
            "profile": current_user.profile,
            "profile_fields": list(auth.PROFILE_FIELDS),
            "collections": collections_for_user(current_user.username),
        })

    data = request.json or {}
    if 'display_username' in data:
        success, msg = user_manager.update_display_username(
            current_user.username, data.get('display_username'))
        if not success:
            return jsonify({"error": msg}), 400
    if 'profile' in data:
        success, msg = user_manager.update_profile(current_user.username, data.get('profile'))
        if not success:
            return jsonify({"error": msg}), 400
        activity_log.record(
            actor=current_user.username,
            category=activity_log.CATEGORY_USER_MANAGEMENT,
            action="user.update_profile",
            target=current_user.username,
            details={"fields": sorted((data.get('profile') or {}).keys())},
        )
    return jsonify({"status": "success", "message": "Profile updated"})


VARIABLE_PREF_SURFACES = ("filter", "display", "timeline", "viz")


def _validate_variable_prefs(prefs) -> str | None:
    """Shape-check a posted ``variable_prefs`` blob; return an error string or None.

    Expected shape: ``{surface: {"include": [names], "exclude": [names]}}`` with
    surfaces limited to :data:`VARIABLE_PREF_SURFACES`. An empty dict resets all
    customizations. Variable names are not checked against the schema here —
    unknown names are simply ignored at composition time, which lets prefs
    survive schema evolution.
    """
    if not isinstance(prefs, dict):
        return "variable_prefs must be an object"
    for surface, delta in prefs.items():
        if surface not in VARIABLE_PREF_SURFACES:
            return f"unknown surface {surface!r}"
        if not isinstance(delta, dict):
            return f"surface {surface!r} must be an object"
        for key, names in delta.items():
            if key not in ("include", "exclude"):
                return f"surface {surface!r}: unknown key {key!r}"
            if not isinstance(names, list) or len(names) > 500:
                return f"surface {surface!r}.{key} must be a list of at most 500 names"
            if not all(isinstance(n, str) for n in names):
                return f"surface {surface!r}.{key} must contain only strings"
    return None

@auth_bp.route('/api/admin/annotations', methods=['GET'])
@permission_required('tab.admin.annotations')
def api_admin_annotations():
    # item_id -> { stats: {...}, details: { variable: { open: {tag: [users]}, notes: [{user, text}], closed: {val: [users]} } } }
    master_index = {}

    # Iterate through all known users instead of listing files to avoid casing/sync issues
    for u in user_manager.get_all_users().values():
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
