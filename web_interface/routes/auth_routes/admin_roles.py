"""Admin roles and permissions: the role list, the permission catalog, and a role's permission set."""

from flask import jsonify, request
from flask_login import current_user

from web_interface.auth import accounts
from web_interface.auth.accounts import user_manager
from web_interface.auth.permissions import permission_required

from ._blueprint import auth_bp


@auth_bp.route("/api/admin/roles", methods=["GET", "POST", "DELETE"])
# GET is needed by any admin sub-page that lists or picks roles: New Users
# (default-role label), Active Users (per-user role dropdown), General
# (default-role setting dropdown), and Roles itself (the matrix UI).
# Write methods (POST/DELETE) are still restricted to tab.admin.roles below.
@permission_required(
    "tab.admin.roles", "tab.admin.active_users", "tab.admin.new_users", "tab.admin.general"
)
def api_admin_roles():
    from web_interface.auth.permissions import user_has_permission

    if request.method == "GET":
        return jsonify(accounts.role_manager.get_roles_with_permissions())

    # POST / DELETE manage the role catalog itself — only the Roles sub-page.
    if not user_has_permission(current_user, "tab.admin.roles"):
        return jsonify({"error": "Forbidden"}), 403

    if request.method == "POST":
        data = request.json
        role_name = data.get("role_name")
        if not role_name:
            return jsonify({"error": "Missing role name"}), 400

        role_name = role_name.strip().lower()

        success, msg = accounts.role_manager.add_role(role_name)
        if success:
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"error": msg}), 400

    elif request.method == "DELETE":
        role_name = request.args.get("role_name")
        if not role_name:
            return jsonify({"error": "Missing role name"}), 400

        success, msg = accounts.role_manager.delete_role(role_name, user_manager)
        if success:
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"error": msg}), 400


@auth_bp.route("/api/admin/permissions/catalog", methods=["GET"])
@permission_required("tab.admin.roles")
def api_admin_permissions_catalog():
    from web_interface.auth.permissions import PERMISSION_CATALOG

    return jsonify(PERMISSION_CATALOG)


@auth_bp.route("/api/admin/roles/<role_name>/permissions", methods=["PUT"])
@permission_required("tab.admin.roles")
def api_admin_role_permissions(role_name):
    from web_interface.auth.permissions import ALL_PERMISSION_KEYS

    data = request.json or {}
    perms = data.get("permissions")
    if not isinstance(perms, list):
        return jsonify({"error": "Body must contain a 'permissions' list"}), 400

    invalid = [p for p in perms if p not in ALL_PERMISSION_KEYS]
    if invalid:
        return jsonify({"error": f"Unknown permission keys: {invalid}"}), 400

    success, msg = accounts.role_manager.set_role_permissions(role_name, perms)
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"error": msg}), 400
