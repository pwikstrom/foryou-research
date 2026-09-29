"""Admin site-wide settings: the settings store, the hashtag stoplist (and applying it retroactively), and the annotation-version listing."""

import pandas as pd
from flask import jsonify, request
from flask_login import current_user

import fyp.core.data_io as data_io
from fyp.core.fyp_config import fyp_cf
from web_interface.auth import accounts
from web_interface.auth.accounts import user_manager
from web_interface.auth.permissions import permission_required
from web_interface.integrations.mail_utils import (
    mail_configured,
)
from web_interface.services import activity_log
from web_interface.services.admin_settings import (
    DEFAULTS as ADMIN_SETTINGS_DEFAULTS,
)
from web_interface.services.admin_settings import (
    SETTING_TYPES as ADMIN_SETTING_TYPES,
)
from web_interface.services.admin_settings import (
    get_session_floors,
    load_admin_settings,
    save_admin_settings,
    validate_setting_value,
)
from web_interface.services.admin_settings import (
    study_names as admin_study_names,
)
from web_interface.tasks import worker_registry

from ._blueprint import auth_bp


@auth_bp.route("/api/admin/settings", methods=["GET", "PUT"])
# GET is also useful to the New Users sub-page (it needs to display the
# configured default role for new signups) and the Backends sub-page (the
# active backend selections live in the settings store). PUT is restricted
# per key via the method-specific check below.
@permission_required("tab.admin.general", "tab.admin.new_users", "tab.admin.backends")
def api_admin_settings():
    if request.method == "GET":
        merged = {**ADMIN_SETTINGS_DEFAULTS, **load_admin_settings()}
        # Session floors fall back to the [sessions] config seed, not to
        # DEFAULTS — report what the server will actually apply, or the admin
        # page shows a number the Sessions tab is not using.
        merged.update(get_session_floors())
        from fyp.annotation.backends import BACKEND_IDS, implemented_backend_ids

        payload = {
            "settings": merged,
            "backend_ids": list(BACKEND_IDS),
            "implemented_backends": list(implemented_backend_ids()),
            # Lets Site Settings say when the verification switch is
            # on but cannot take effect (no MAIL_PASSWORD / sender).
            "mail_configured": mail_configured(),
        }
        # Choices for the default-study picker. Only Site Settings holders get
        # them — the other two sub-pages that may read this endpoint have no
        # business learning every study name.
        from web_interface.auth.permissions import user_has_permission

        if user_has_permission(current_user, "tab.admin.general"):
            payload["study_names"] = admin_study_names()
            from web_interface.services.admin_settings import demo_collection_choices

            payload["demo_collection_choices"] = demo_collection_choices()
        return jsonify(payload)

    # PUT — the backend selections belong to the Backends sub-page, every
    # other setting to Site Settings (tab.admin.general).
    from web_interface.auth.permissions import user_has_permission

    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    allowed_keys = set(ADMIN_SETTINGS_DEFAULTS.keys())
    unknown = [k for k in data if k not in allowed_keys]
    if unknown:
        return jsonify({"error": f"Unknown settings: {unknown}"}), 400

    _BACKEND_SETTING_KEYS = {"annotation_backend", "embedding_backend"}
    for k in data:
        required = "tab.admin.backends" if k in _BACKEND_SETTING_KEYS else "tab.admin.general"
        if not user_has_permission(current_user, required):
            return jsonify({"error": "Forbidden"}), 403

    # Per-key type validation. Unknown-type keys default to bool to preserve
    # the historical contract for any legacy boolean flag.
    for k, v in data.items():
        expected = ADMIN_SETTING_TYPES.get(k, bool)
        if not isinstance(v, expected):
            names = "/".join(
                t.__name__ for t in (expected if isinstance(expected, tuple) else (expected,))
            )
            return jsonify({"error": f"Setting '{k}' must be a {names}"}), 400
        # Extra check: the default-role setting must reference an existing role.
        if k == "default_new_user_role" and not accounts.role_manager.role_exists(v):
            return jsonify({"error": f"Unknown role: {v!r}"}), 400
        semantic_error = validate_setting_value(k, v)
        if semantic_error:
            return jsonify({"error": semantic_error}), 400

    current = load_admin_settings()
    prev_annotation_backend = current.get(
        "annotation_backend", ADMIN_SETTINGS_DEFAULTS.get("annotation_backend")
    )
    prev_default_study = current.get("default_study", ADMIN_SETTINGS_DEFAULTS.get("default_study"))
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
    if "annotation_backend" in data and data["annotation_backend"] != prev_annotation_backend:
        switch_info = {
            "from": prev_annotation_backend,
            "to": data["annotation_backend"],
            "annotation_version": None,
        }
        try:
            from fyp.annotation import annotation_versioning

            minted = annotation_versioning.ensure_active_version_registered()
            switch_info["annotation_version"] = minted
            activity_log.record(
                actor=getattr(current_user, "username", "") or "",
                category="admin",
                action="annotation_backend.switch",
                details={
                    "from": prev_annotation_backend,
                    "to": data["annotation_backend"],
                    "annotation_version": minted,
                },
            )
        except Exception:
            pass

    merged = {**ADMIN_SETTINGS_DEFAULTS, **current}
    payload = {"status": "success", "message": "Settings updated", "settings": merged}
    if switch_info:
        payload["annotation_backend_switch"] = switch_info
    return jsonify(payload)


@auth_bp.route("/api/admin/irrelevant_words", methods=["GET", "PUT"])
@permission_required("tab.admin.stoplist")
def api_irrelevant_words():
    """The admin-editable hashtag stoplist (see ``fyp.irrelevant_words``).

    GET returns the current list (seeding the store from config.toml on first
    access). PUT replaces the whole list; body ``{"words": [...], "etag": ...}``
    — refuses with 409 on a stale etag (concurrent edit) and 400 on invalid
    entries. Edits apply when hashtags are next extracted (scrape/annotation);
    already-stored hashtags are unchanged.
    """
    from fyp.annotation import irrelevant_words as iw

    if request.method == "GET":
        words = iw.load_words()
        payload = iw.load_payload() or {}
        return jsonify(
            {
                "words": words,
                "count": len(words),
                "etag": iw.compute_words_etag(),
                "updated_at": payload.get("updated_at"),
                "updated_by": payload.get("updated_by"),
            }
        )

    data = request.json or {}
    if not isinstance(data, dict) or not isinstance(data.get("words"), list):
        return jsonify({"error": "Body must contain a 'words' list"}), 400

    try:
        result = iw.save_words(
            data["words"],
            expected_etag=data.get("etag"),
            updated_by=current_user.username,
        )
    except iw.IrrelevantWordsConflict as e:
        return jsonify(
            {
                "error": "conflict",
                "message": str(e),
                "etag": iw.compute_words_etag(),
                "words": iw.load_words(),
            }
        ), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    activity_log.record(
        actor=current_user.username,
        category="admin",
        action="irrelevant_words.save",
        details={"count": len(result["words"])},
    )
    return jsonify(
        {
            "status": "success",
            "words": result["words"],
            "count": len(result["words"]),
            "etag": result["etag"],
        }
    )


@auth_bp.route("/api/admin/irrelevant_words/apply", methods=["POST"])
@permission_required("tab.admin.stoplist")
def api_irrelevant_words_apply():
    """Start the background job that re-applies the stoplist to existing data.

    Re-tokenises the stored ``desc_hashtags`` from the preserved captions across
    the source scrape parquets with the current stoplist (see
    ``run_retokenise_hashtags``). Clean-only — the response tells the caller to
    run a forced full reconsolidation afterward. Refuses (409) while a scraper/annotator/
    consolidation is running, since it rewrites the same scrape parquets.
    """

    from web_interface.services.worker_status import is_worker_running, workers_blocking_consolidate
    from web_interface.tasks.process_manager import start_process

    if is_worker_running("retokenise_hashtags"):
        return jsonify({"status": "error", "message": "Already running"}), 409

    blocking = workers_blocking_consolidate()
    if is_worker_running("consolidate_enrichment"):
        blocking.append("consolidate_enrichment")
    if blocking:
        return jsonify(
            {
                "status": "error",
                "message": f"Cannot run while {', '.join(blocking)} running.",
            }
        ), 409

    success, msg = start_process(
        "retokenise_hashtags",
        worker_registry.worker_module("retokenise_hashtags"),
        started_by=current_user.username,
    )
    if success:
        activity_log.record(
            actor=current_user.username,
            category="admin",
            action="irrelevant_words.apply",
        )
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409


@auth_bp.route("/api/admin/annotations", methods=["GET"])
@permission_required("tab.admin.annotations")
def api_admin_annotations():
    # item_id -> { stats: {...}, details: { variable: { open: {tag: [users]}, notes: [{user, text}], closed: {val: [users]} } } }
    master_index = {}

    # Iterate through all known users instead of listing files to avoid casing/sync issues
    for u in user_manager.get_all_users().values():
        username = u.username
        user_filename = f"{username}.json"

        try:
            user_data_file = data_io.load_json(storage_location="users", filename=user_filename)
            if not user_data_file:
                continue

            user_annotations = user_data_file.get("annotations", {})

            for item_id, item_vars in user_annotations.items():
                if item_id not in master_index:
                    master_index[item_id] = {
                        "item_id": item_id,
                        "stats": {
                            "notes": 0,
                            "open_tags": 0,
                            "closed_tags": 0,
                            "unique_users": set(),
                        },
                        "details": {},
                    }

                entry = master_index[item_id]
                entry["stats"]["unique_users"].add(username)

                for key, value in item_vars.items():
                    # Check types
                    var_name = key
                    type_ = "open"

                    if key.endswith("__NOTES"):
                        var_name = key[:-7]  # remove __NOTES
                        type_ = "note"
                    elif key.endswith("__CLOSED_TAGGING"):
                        var_name = key[:-16]  # remove __CLOSED_TAGGING
                        type_ = "closed"

                    if var_name not in entry["details"]:
                        # Resolve Friendly Name
                        friendly_name = var_name
                        if "var_schema" in fyp_cf:
                            df = fyp_cf["var_schema"]
                            if isinstance(df, pd.DataFrame):
                                match = df[df["variable_name"] == var_name]
                                if not match.empty:
                                    try:
                                        sec = match["section"].iloc[0]
                                        disp = match["display_name"].iloc[0]

                                        if pd.isna(sec):
                                            sec = "Unknown"
                                        if pd.isna(disp):
                                            disp = var_name

                                        friendly_name = f"{sec} - {disp}"
                                    except Exception:
                                        pass

                        entry["details"][var_name] = {
                            "label": friendly_name,
                            "open": {},
                            "notes": [],
                            "closed": {},
                        }

                    det = entry["details"][var_name]

                    if type_ == "note":
                        det["notes"].append({"user": username, "text": value})
                        entry["stats"]["notes"] += 1

                    elif type_ == "closed":
                        val_str = str(value)
                        if val_str not in det["closed"]:
                            det["closed"][val_str] = []
                        det["closed"][val_str].append(username)
                        entry["stats"]["closed_tags"] += 1

                    else:
                        # Open tags list
                        if isinstance(value, list):
                            for tag in value:
                                if tag not in det["open"]:
                                    det["open"][tag] = []
                                det["open"][tag].append(username)
                                entry["stats"]["open_tags"] += 1

        except Exception as e:
            print(f"Error processing {username}: {e}")

    # Convert to list and fix unique_users count
    results = []
    for item in master_index.values():
        if isinstance(item["stats"]["unique_users"], set):
            item["stats"]["unique_users"] = len(item["stats"]["unique_users"])
        results.append(item)

    # Sort by total activity (desc)
    results.sort(
        key=lambda x: x["stats"]["notes"] + x["stats"]["open_tags"] + x["stats"]["closed_tags"],
        reverse=True,
    )

    return jsonify(results)
