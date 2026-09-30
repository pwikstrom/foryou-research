"""Admin user management: the user list and its create / update / delete, orphaned participant accounts, and each account's activity log and admin notes."""

from datetime import UTC, datetime

from email_validator import EmailNotValidError, validate_email
from flask import jsonify, request
from flask_login import current_user

import fyp.core.data_io as data_io
from web_interface.auth import accounts, email_verification
from web_interface.auth.accounts import user_manager
from web_interface.auth.permissions import permission_required
from web_interface.integrations.mail_utils import (
    mail_configured,
    send_welcome_email_async,
)
from web_interface.services import activity_log, admin_notes
from web_interface.services.admin_settings import (
    get_default_new_user_role,
)
from web_interface.services.collection_accounts import (
    load_owner_map,
    orphan_placeholder_accounts,
    unlink_user,
)
from web_interface.tasks import worker_registry

from ._blueprint import auth_bp


@auth_bp.route("/api/admin/users", methods=["GET", "POST", "PUT", "DELETE"])
@permission_required("tab.admin.new_users", "tab.admin.active_users")
def api_admin_users():
    """The admin user list (GET) and account create / update / delete."""
    if request.method == "GET":
        return _list_users()
    if request.method == "POST":
        return _create_user()
    if request.method == "PUT":
        return _update_user()
    if request.method == "DELETE":
        return _delete_user()


def _list_users():
    """Every account (no password hashes), with its linked collections and stats."""
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
        del ud["password_hash"]
        ud["can_login"] = u.can_login()
        ud["email_verified"] = u.email_verified()
        ud["collections"] = sorted(owned.get(u.username, []))
        ud["collections_count"] = len(ud["collections"])

        # Init stats
        ud["stats"] = {
            "notes": 0,
            "closed_tags": 0,
            "open_tags": 0,
            "unique_videos": 0,
            "used_tags": [],
            "user_notes": [],
        }

        # Refactored for single file structure
        user_filename = f"{u.username}.json"

        # Try to load file directly (data_io.load_json returns None if missing/fail)
        try:
            user_data_file = data_io.load_json(storage_location="users", filename=user_filename)

            # Try lowercase if failed
            if not user_data_file:
                user_filename_lower = f"{u.username.lower()}.json"
                user_data_file = data_io.load_json(
                    storage_location="users", filename=user_filename_lower
                )

            if user_data_file:
                user_annotations = user_data_file.get("annotations", {})

                notes_count = 0
                closed_count = 0
                open_count = 0
                unique_videos = set()
                used_tags = set()
                user_notes = []  # List of {item_id: text}

                for item_id, item_vars in user_annotations.items():
                    has_annotation = False
                    for key, value in item_vars.items():
                        if key.endswith("__NOTES"):
                            notes_count += 1
                            has_annotation = True
                            user_notes.append({"item": item_id, "text": value})
                        elif key.endswith("__CLOSED_TAGGING"):
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

                ud["stats"] = {
                    "notes": notes_count,
                    "closed_tags": closed_count,
                    "open_tags": open_count,
                    "unique_videos": len(unique_videos),
                    "used_tags": sorted(list(used_tags)),
                    "user_notes": user_notes,
                }
        except Exception as e:
            print(f"Error loading stats for {u.username}: {e}")

        users_list.append(ud)
    return jsonify(users_list)


def _create_user():
    """Create an account from the admin form."""
    data = request.json
    username = data.get("username")
    display_username = data.get("display_username")
    password = data.get("password")
    # Role is no longer admin-selectable per user; the configured default
    # role applies to everyone (signups and admin-created users alike).
    role = get_default_new_user_role()

    if not username or not password:
        return jsonify({"error": "Missing email or password"}), 400

    try:
        validate_email(username, check_deliverability=False)
    except EmailNotValidError as e:
        return jsonify({"error": f"Invalid email: {e!s}"}), 400

    cleaned_display, display_err = accounts.validate_display_username(display_username)
    if display_err:
        return jsonify({"error": display_err}), 400

    success, msg = user_manager.add_user(
        username,
        password,
        role,
        approved=True,
        display_username=cleaned_display,
        origin={
            "source": "admin",
            "at": datetime.now(UTC).isoformat(),
            "by": current_user.username,
        },
        email_verified_via=accounts.EMAIL_VERIFIED_ADMIN,
    )
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


def _update_user():
    """Apply one admin ``action`` to an account.

    approve, reset_password, set_display_username, change_role, mark_verified,
    resend_verification or set_profile.
    """
    data = request.json
    action = data.get("action")
    username = data.get("username")

    if not username:
        return jsonify({"error": "Missing username"}), 400

    if action == "approve":
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
        else:
            return jsonify({"error": msg}), 400

    elif action == "reset_password":
        new_password = data.get("new_password")
        if not new_password:
            return jsonify({"error": "Missing new password"}), 400

        success, msg = user_manager.update_password(username, new_password)
        if success:
            activity_log.record(
                actor=current_user.username,
                category=activity_log.CATEGORY_USER_MANAGEMENT,
                action="user.reset_password",
                target=username,
            )
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"error": msg}), 400

    elif action == "set_display_username":
        prev_user = user_manager.get_user(username)
        old_name = prev_user.display_username if prev_user else None
        success, msg = user_manager.update_display_username(username, data.get("display_username"))
        if success:
            activity_log.record(
                actor=current_user.username,
                category=activity_log.CATEGORY_USER_MANAGEMENT,
                action="user.set_display_username",
                target=username,
                details={"from": old_name, "to": data.get("display_username")},
            )
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"error": msg}), 400

    elif action == "change_role":
        new_role = data.get("role")
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
        else:
            return jsonify({"error": msg}), 400

    elif action == "mark_verified":
        success, msg = user_manager.mark_email_verified(username, via=accounts.EMAIL_VERIFIED_ADMIN)
        if success:
            activity_log.record(
                actor=current_user.username,
                category=activity_log.CATEGORY_USER_MANAGEMENT,
                action="user.mark_email_verified",
                target=username,
            )
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"error": msg}), 400

    elif action == "resend_verification":
        target = user_manager.get_user(username)
        if target is None:
            return jsonify({"error": "User not found"}), 404
        if target.email_verified():
            return jsonify({"error": "This account is already verified"}), 400
        if not target.can_login():
            return jsonify({"error": "This account has no password to verify"}), 400
        if not mail_configured():
            return jsonify({"error": "Outgoing mail is not configured on this instance"}), 400
        email_verification.send_verification_link(user_manager, target, force=True)
        activity_log.record(
            actor=current_user.username,
            category=activity_log.CATEGORY_USER_MANAGEMENT,
            action="user.resend_verification",
            target=username,
        )
        return jsonify({"status": "success", "message": "Verification link sent"})

    elif action == "set_profile":
        success, msg = user_manager.update_profile(username, data.get("profile"))
        if success:
            activity_log.record(
                actor=current_user.username,
                category=activity_log.CATEGORY_USER_MANAGEMENT,
                action="user.set_profile",
                target=username,
                details={"fields": sorted((data.get("profile") or {}).keys())},
            )
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"error": msg}), 400

    return jsonify({"error": "Invalid action"}), 400


def _delete_user():
    """Delete an account, unlinking its collections first.

    With ``cascade_collections`` the unlinked collections are then deleted by
    the ``collection_delete`` worker; the account's participant study pair is
    cleaned up either way.
    """
    username = request.args.get("username")
    cascade = request.args.get("cascade_collections", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )

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
        from web_interface.services.collection_accounts import set_collection_owner

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
        from web_interface.services.participant_studies import ensure_participant_studies

        ensure_participant_studies(username)
    except Exception as exc:
        print(f"[delete_user] participant-study cleanup failed (non-fatal): {exc}")

    result = {"status": "success", "message": msg, "unlinked_collections": unlinked}
    if cascade and unlinked:
        from web_interface.tasks.process_manager import start_process

        ok, pmsg = start_process(
            "collection_delete",
            worker_registry.worker_module("collection_delete"),
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
            result["message"] = (
                f"{msg}. Collections were unlinked but the delete task could not "
                f"start: {pmsg}. Delete them from Edit Collections."
            )
    return jsonify(result)


@auth_bp.route("/api/admin/users/orphan_participants", methods=["GET", "POST"])
@permission_required("tab.admin.active_users")
def api_admin_orphan_participants():
    """Placeholder participant accounts (p-N@…) that own no collection.

    GET lists them; POST deletes them. A placeholder exists only to hold the
    demographics that came with a donation, so once its collections are gone
    it is dead weight — but removal stays an explicit admin action.
    """
    orphans = orphan_placeholder_accounts()
    if request.method == "GET":
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


@auth_bp.route("/api/admin/users/<path:username>/log", methods=["GET"])
@permission_required("tab.admin.active_users")
def api_admin_user_log(username):
    """Return the activity log for the given user (newest first)."""
    entries = activity_log.read(username)
    return jsonify({"entries": entries})


@auth_bp.route("/api/admin/users/<path:username>/notes", methods=["GET", "POST"])
@permission_required("tab.admin.active_users")
def api_admin_user_notes(username):
    """The admin's log for one account: GET lists notes (newest first), POST adds one.

    Notes are attributed to the admin who wrote them and are never visible to
    the account holder. Adding a note is also recorded in the writer's own
    activity log so the audit trail shows who annotated whom.
    """
    if user_manager.get_user(username) is None:
        return jsonify({"error": "User not found"}), 404
    if request.method == "GET":
        return jsonify({"notes": admin_notes.read(username)})

    data = request.get_json(silent=True) or {}
    note, err = admin_notes.add(username, author=current_user.username, text=data.get("text", ""))
    if err:
        status = 500 if err.startswith("Failed") else 400
        return jsonify({"error": err}), status
    activity_log.record(
        actor=current_user.username,
        category=activity_log.CATEGORY_USER_MANAGEMENT,
        action="user.note_added",
        target=username,
    )
    return jsonify({"status": "success", "note": note})


@auth_bp.route("/api/admin/users/<path:username>/notes/<note_id>", methods=["DELETE"])
@permission_required("tab.admin.active_users")
def api_admin_user_note_delete(username, note_id):
    """Remove one note from an account's admin's log."""
    removed, err = admin_notes.delete(username, note_id)
    if err:
        status = 500 if err.startswith("Failed") else 404
        return jsonify({"error": err}), status
    activity_log.record(
        actor=current_user.username,
        category=activity_log.CATEGORY_USER_MANAGEMENT,
        action="user.note_deleted",
        target=username,
        details={"author": removed.get("author", "")},
    )
    return jsonify({"status": "success"})
