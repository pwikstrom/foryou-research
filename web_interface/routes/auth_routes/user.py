"""A signed-in user's own settings: preferences, variable catalog and variable preferences, and profile."""

from flask import jsonify, request
from flask_login import current_user, login_required

from web_interface.auth import accounts
from web_interface.auth.accounts import user_manager
from web_interface.auth.permissions import permission_required
from web_interface.services import activity_log
from web_interface.services.collection_accounts import (
    collections_for_user,
)

from ._blueprint import auth_bp

# The closed set of user-settings keys the POST endpoint accepts. The store
# itself is schemaless, so this whitelist is the only guard against arbitrary
# unbounded keys landing in a user record.
USER_SETTINGS_KEYS = frozenset(
    {
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
    }
)


@auth_bp.route("/api/user/settings", methods=["GET", "POST"])
@login_required
def api_user_settings():
    if request.method == "GET":
        s = current_user.settings or {}
        if "share_annotations" not in s:
            # Annotation sharing is opt-in: an unset value reads as off.
            s["share_annotations"] = False
        return jsonify(s)

    elif request.method == "POST":
        settings = request.json
        if not isinstance(settings, dict):
            return jsonify({"error": "Settings must be a JSON object"}), 400
        unknown = sorted(set(settings) - USER_SETTINGS_KEYS)
        if unknown:
            return jsonify({"error": f"Unknown settings keys: {', '.join(unknown)}"}), 400
        if "variable_prefs" in settings:
            err = _validate_variable_prefs(settings["variable_prefs"])
            if err:
                return jsonify({"error": err}), 400
        success, msg = user_manager.update_user_settings(current_user.username, settings)
        if success:
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"error": msg}), 400


@auth_bp.route("/api/user/variable-catalog", methods=["GET"])
@login_required
def api_user_variable_catalog():
    """Study-independent variable catalog for the My Stuff preference panels.

    Everything the "Customize variables" panels need — the canonical variable
    order, the four global per-surface ON lists, and the schema map with
    sections/display names — comes from the synthesized var_schema, so it is
    the same for every study. The tabs used to supply this from their loaded
    study metadata; My Stuff has no study loaded, hence this endpoint.
    """
    from web_interface.services.study_data import make_serializable
    from web_interface.services.user_variables import load_schema_metadata

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


@auth_bp.route("/api/user/profile", methods=["GET", "POST"])
@permission_required("tab.my_stuff.profile")
def api_user_profile():
    """Read or update the current user's own profile.

    The email (account id) is immutable and returned read-only. POST accepts
    ``display_username`` and/or a ``profile`` object (see ``PROFILE_FIELDS``);
    either may be omitted to leave it unchanged.
    """
    if request.method == "GET":
        return jsonify(
            {
                "email": current_user.username,
                "display_username": current_user.display_username,
                "profile": current_user.profile,
                "profile_fields": list(accounts.PROFILE_FIELDS),
                "collections": collections_for_user(current_user.username),
            }
        )

    data = request.json or {}
    if "display_username" in data:
        success, msg = user_manager.update_display_username(
            current_user.username, data.get("display_username")
        )
        if not success:
            return jsonify({"error": msg}), 400
    if "profile" in data:
        success, msg = user_manager.update_profile(current_user.username, data.get("profile"))
        if not success:
            return jsonify({"error": msg}), 400
        activity_log.record(
            actor=current_user.username,
            category=activity_log.CATEGORY_USER_MANAGEMENT,
            action="user.update_profile",
            target=current_user.username,
            details={"fields": sorted((data.get("profile") or {}).keys())},
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
