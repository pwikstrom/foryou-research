"""Collection management endpoints (/api/manage/collections*, collection annotations)."""


import pandas as pd
from flask import jsonify, request
from flask_login import login_required

import fyp.data_io as data_io
from fyp.fyp_config import (
    fyp_cf,
)
from fyp.ingest import registered_raw_locations
from fyp.organize_datasets import (
    COLLECTIONS_LABEL,
)
from fyp.studies import init_study_defs

from ... import activity_log
from ...collection_accounts import collection_counts_by_user
from ...data_service import (
    invalidate_collection_tags_cache,
)
from ...security import user_manager
from ...process_manager import (
    start_process,
)
from ...permissions import permission_required



from ...services.worker_status import (
    _actor,
)



from ._blueprint import management_bp


def _find_raw_file_locations(raw_files: list[str]) -> list[tuple[str, str]]:
    """Return [(storage_location, filename), ...] for each raw file that still
    exists in any of the registered upload locations.

    The location list is derived from the collection-class registry
    (fyp.ingest.registered_raw_locations), so a new platform's upload location
    is probed automatically. Probes each location's ingestion_manifest.json
    first (fast path) and falls back to data_io.exists when the manifest is
    missing or out of sync. Files not found in any location are silently
    skipped — they were already moved or deleted previously.
    """
    found: list[tuple[str, str]] = []
    raw_files_set = set(raw_files)
    if not raw_files_set:
        return found

    upload_locations = registered_raw_locations()
    manifests: dict[str, dict] = {}
    for loc in upload_locations:
        if data_io.exists(storage_location=loc, filename="ingestion_manifest.json"):
            manifests[loc] = data_io.load_json(
                storage_location=loc, filename="ingestion_manifest.json", verbose=False
            ) or {}
        else:
            manifests[loc] = {}

    for fn in raw_files_set:
        for loc in upload_locations:
            if fn in manifests[loc] or data_io.exists(storage_location=loc, filename=fn):
                found.append((loc, fn))
                break

    return found




def _affected_studies_for_collections(collection_ids) -> list[str]:
    """Return the names of studies whose SELECTED_COLLECTIONS contains any of
    ``collection_ids``. The union, so a bulk delete refreshes each affected
    study once rather than once per collection it happens to hold."""
    init_study_defs()
    wanted = {str(c) for c in collection_ids}
    out: list[str] = []
    for sname, sdef in (fyp_cf.get('study_defs') or {}).items():
        sel = {str(c) for c in (sdef.get('SELECTED_COLLECTIONS') or [])}
        if sel & wanted:
            out.append(sname)
    return out




def _affected_studies_for_collection(collection_id: str) -> list[str]:
    """Single-collection form of _affected_studies_for_collections. Kept as the
    name the collection_delete worker imports."""
    return _affected_studies_for_collections([collection_id])




def _requested_collection_ids(source) -> list[str]:
    """Collection ids from a request, de-duplicated and order-preserving.

    Accepts the single ``collection_id`` the endpoints have always taken and
    the ``collection_ids`` list the multi-select uses. ``source`` is a JSON
    body dict or a request args MultiDict.
    """
    raw: list = []
    getlist = getattr(source, "getlist", None)
    if getlist is not None:
        raw = list(getlist("collection_ids")) + list(getlist("collection_id"))
    else:
        many = source.get("collection_ids")
        if isinstance(many, (list, tuple)):
            raw = list(many)
        one = source.get("collection_id")
        if one is not None:
            raw.append(one)

    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        cid = str(value or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out




@management_bp.route('/api/manage/collections/affected_studies', methods=['GET'])
@permission_required('tab.data_management.edit_collections')
@login_required
def affected_studies_for_collection():
    """Return the studies that reference the given collection_id(s). Used by the
    delete-collection confirmation dialog to show what will be refreshed.
    Accepts ``collection_id`` one or more times (or ``collection_ids``)."""
    collection_ids = _requested_collection_ids(request.args)
    if not collection_ids:
        return jsonify({"error": "Missing collection_id"}), 400
    return jsonify({"studies": _affected_studies_for_collections(collection_ids)})




@management_bp.route('/api/manage/collections/delete', methods=['POST'])
@permission_required('tab.data_management.edit_collections')
@login_required
def delete_collection():
    """Dispatch a collection_delete Cloud Task. The actual delete (which loads
    and rewrites the 1+ GB collections_recoded.parquet) runs on the task-runner
    so the data-hub doesn't risk OOM or timeout. The UI polls /api/status for
    completion and reads the final result from the task's emitted data payload.

    Takes ``collection_id`` or a ``collection_ids`` list. Several collections
    are deleted by ONE task, not one task each: every task reloads and rewrites
    the whole activity parquet, and concurrent runs would each write back a
    frame that still contains the others' rows.
    """
    data = request.json or {}
    collection_ids = _requested_collection_ids(data)
    if not collection_ids:
        return jsonify({"error": "Missing collection_id"}), 400

    from fyp.fyp_config import COLLECTION_DELETE_SCRIPT

    success, msg = start_process(
        "collection_delete",
        COLLECTION_DELETE_SCRIPT,
        task_args={"collection_ids": collection_ids},
        started_by=_actor(),
    )
    if success:
        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="collection.delete",
            target=", ".join(collection_ids),
        )
        return jsonify({
            "status": "started",
            "collection_id": collection_ids[0],
            "collection_ids": collection_ids,
            "message": msg,
        })
    return jsonify({"status": "error", "message": msg}), 409







@management_bp.route('/api/manage/collections/coverage', methods=['GET'])
@permission_required('tab.data_management.edit_collections')
@login_required
def collections_coverage():
    """Scraped/annotated coverage for every collection, for the table's column.

    Deliberately NOT part of ``/api/manage/collections``: coverage needs a scan
    of the whole recoded parquet, while the listing reads only the (small)
    metadata table. Keeping them apart lets the Edit Collections table render
    at metadata speed and fill this column when it arrives.

    ``?fresh=1`` re-scans instead of reading the TTL cache — for checking on an
    enrichment run that has just finished.
    """
    from ...services import collection_coverage

    fresh = bool(request.args.get('fresh'))
    return jsonify(collection_coverage.corpus_coverage(force=fresh))




@management_bp.route('/api/manage/collections', methods=['GET'])
@permission_required('tab.data_management.edit_collections')
@login_required
def list_collections():

    if True:#try:
        # Load ddp_metadata from storage
        if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
            df = data_io.load_parquet(
                storage_location="recoded", 
                filename=f"{COLLECTIONS_LABEL}_metadata.parquet", 
                verbose=False,
            )
            
            # Filter for accepted collections
            if ('other', 'accepted') in df.columns:
                df = df[df[('other', 'accepted')]]
                
            # Load annotations
            annotations = {}
            if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json"):
                annotations = data_io.load_json(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json")
                
            # Construct structured dictionaries
            collections = []

            # Account link per collection + a label for the UI (display name,
            # else the account id). Unknown ids (a deleted account whose link
            # was not unlinked) are still reported so the admin can fix them.
            user_labels = {u.username: (u.display_username or u.username)
                           for u in user_manager.get_all_users().values()}

            # Automatic-enrichment plan state, so the table can show at a glance
            # which collections are enriching themselves. One small JSON read
            # for the whole listing — cheap enough to ride along here rather
            # than earn its own round trip the way coverage does.
            from ...services import collection_enrichment as ce
            enrichment_plans = ce.load_plans()

            # Make sure we don't have pd.NA or similar incompatible types for JSON serialization
            df = df.where(pd.notnull(df), None)
            
            # Helper to convert pandas/pyarrow types cleanly to standard Python types
            def safe_val(val):
                if pd.isna(val) or val is None:
                    return None
                if hasattr(val, "item"):
                    try:
                        val = val.item()
                    except Exception:
                        pass
                if hasattr(val, "isoformat"):
                    return val.isoformat()
                return val

            for index, row in df.iterrows():
                # Use collection_id column if available, otherwise fall back to index
                if 'collection_id' in df.columns:
                    row_id = str(row['collection_id'])
                else:
                    row_id = str(index)
                item = {
                    "id": row_id,
                    "participants": {},
                    "other": {},
                    "personas": {}
                }
                
                # Fetch participant info
                for c in df.columns:
                    if c[0] == 'participants':
                        item['participants'][c[1]] = safe_val(row[c])
                    elif c[0] == 'other':
                        item['other'][c[1]] = safe_val(row[c])
                    elif c[0] == 'personas':
                        item['personas'][c[1]] = safe_val(row[c])
                        
                # Attach annotations (keyed by collection ID, not row index)
                ann = annotations.get(row_id, {})
                item['displayId'] = ann.get('display_collection_id', None)
                item['tags'] = ann.get('annotation_tags', [])
                item['hidden'] = ann.get('hidden', False)
                uid = ann.get('user_id')
                item['user_id'] = uid
                item['user_label'] = user_labels.get(uid, uid) if uid else None
                item['user_known'] = bool(uid) and uid in user_labels
                plan = enrichment_plans.get(row_id)
                item['enrichment_state'] = (plan.get('state')
                                            if isinstance(plan, dict) else None)

                collections.append(item)

            return jsonify(collections)
        else:
            print(f"{COLLECTIONS_LABEL}_metadata.parquet not found")
            return jsonify([])


@management_bp.route('/api/manage/collection/save_annotation', methods=['POST'])
@permission_required('tab.data_management.edit_collections')
@login_required
def save_collection_annotation():
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    collection_id = data.get('collection_id')
    if not collection_id:
        return jsonify({"error": "Missing collection_id"}), 400

    # Account link: absent = leave as is; null = explicitly unassign;
    # a string must be an existing account id.
    set_user = 'user_id' in data
    user_id = data.get('user_id')
    if set_user and user_id is not None:
        if not isinstance(user_id, str) or user_manager.get_user(user_id) is None:
            return jsonify({"error": f"Unknown user account: {user_id!r}"}), 400

    try:
        annotations = {}
        if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json"):
            annotations = data_io.load_json(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json")

        # Update in place so keys this endpoint doesn't own (the account
        # link, anything added later) survive a tag edit.
        entry = annotations.get(str(collection_id))
        if not isinstance(entry, dict):
            entry = {}
        entry["display_collection_id"] = data.get('display_collection_id', None)
        entry["annotation_tags"] = data.get('tags', [])
        entry["hidden"] = data.get('hidden', False)
        previous_user = entry.get("user_id")
        if set_user:
            entry["user_id"] = user_id
        annotations[str(collection_id)] = entry

        data_io.save_json(
            data=annotations,
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_tags.json",
            verbose=False
        )
        invalidate_collection_tags_cache()

        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="collection.annotation.save",
            target=str(collection_id),
            details={
                "tags": data.get('tags', []),
                "hidden": bool(data.get('hidden', False)),
                **({"user_id": {"from": previous_user, "to": user_id}} if set_user else {}),
            },
        )

        # An ownership change moves the collection between accounts' auto-
        # managed study pairs — reconcile both sides (new owner gains it,
        # previous owner loses it) on a background thread so the save stays
        # snappy. Never fails the save.
        if set_user and user_id != previous_user:
            try:
                from ...services.participant_studies import sync_for_cids
                sync_for_cids(
                    [str(collection_id)],
                    usernames=[u for u in (previous_user,) if u],
                )
            except Exception as exc:
                print(f"[save_collection_annotation] participant-study sync failed: {exc}")

        return jsonify({"status": "success"})
    except Exception as e:
        print(f"Error saving annotation: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/accounts', methods=['GET'])
@permission_required('tab.data_management.ingestion', 'tab.data_management.edit_collections')
@login_required
def list_accounts():
    """Lightweight account list for the "link collection to account" pickers
    (upload modal, Edit Collections). No profile data — just enough to pick.
    Sorted: members first, then participant accounts, placeholders last."""
    counts = collection_counts_by_user()
    rows = []
    for u in user_manager.get_all_users().values():
        rows.append({
            "username": u.username,
            "display_username": u.display_username or "",
            "account_kind": u.account_kind,
            "placeholder": bool(u.placeholder),
            "can_login": u.can_login(),
            "collections_count": counts.get(u.username, 0),
        })
    rows.sort(key=lambda r: (r["placeholder"], r["account_kind"] != "member",
                             (r["display_username"] or r["username"]).lower()))
    return jsonify(rows)




@management_bp.route('/api/manage/collections/<collection_id>/enrichment', methods=['GET'])
@permission_required('tab.data_management.edit_collections')
@login_required
def get_collection_enrichment(collection_id):
    """The collection's automatic-enrichment plan + live progress, for the modal."""
    from ...services import collection_enrichment as ce
    from ... import admin_settings

    from .enrichment import _annotation_cost_estimate

    entry = ce.get_plan(collection_id)
    payload = {
        "enabled_site_wide": bool(admin_settings.get_setting("auto_enrichment_enabled")),
        "armed": entry is not None,
        "settings": (entry or {}).get("settings") or dict(ce.DEFAULT_SETTINGS),
        "progress": ce.progress(collection_id, entry),
        # The supervisor is a global singleton, so this is the last tick of ANY
        # collection — the panel uses it only to report the outcome of a tick it
        # just fired (it polls until start_time changes), never as plan state.
        "last_tick": ce.last_tick(),
        # What the machinery is doing right now (scraping / annotating /
        # consolidating / waiting), for the panel's status strip.
        "activity": ce.activity((entry or {}).get("platform")),
        # Per-1000-items annotation estimate for the target readout (None when
        # the active backend has no pricing, e.g. a local model).
        "cost_per_1000": _annotation_cost_estimate(1000),
    }
    return jsonify(payload)


@management_bp.route('/api/manage/collections/<collection_id>/enrichment', methods=['POST'])
@permission_required('tab.data_management.edit_collections')
@login_required
def save_collection_enrichment(collection_id):
    """Arm, pause, resume or reconfigure a collection's enrichment plan.

    Payload: ``{"state": "running"|"paused", "settings": {...}}`` — both
    optional; settings are normalized/clamped server-side. Arming a collection
    that has never been armed seeds the ledger entry (cursors start unset, so
    the first cycle begins at the newest day/month). Pausing keeps every
    cursor, so resuming continues where it stopped.
    """
    from ...services import collection_enrichment as ce
    from ...collection_accounts import load_owner_map

    data = request.json or {}
    cid = str(collection_id)

    patch = {}
    state = data.get("state")
    if state is not None:
        if state not in (ce.STATE_RUNNING, ce.STATE_PAUSED):
            return jsonify({"error": f"Unknown state '{state}'"}), 400
        patch["state"] = state
        if state == ce.STATE_RUNNING:
            # (Re)arming clears the fault fields so a blocked/stalled plan can
            # be revived deliberately from the modal.
            patch["stall_count"] = 0
            patch["last_error"] = None
    if isinstance(data.get("settings"), dict):
        patch["settings"] = ce.normalize_settings(data["settings"])

    existing = ce.get_plan(cid)
    if existing is None:
        try:
            owner = (load_owner_map() or {}).get(cid)
        except Exception:
            owner = None
        patch.setdefault("state", ce.STATE_PAUSED)
        patch.setdefault("settings", ce.normalize_settings(data.get("settings")))
        patch.update({"owner": owner, "spent_items": 0, "cycles": 0,
                      "stall_count": 0, "created_at": ce.now_iso(),
                      "created_by": _actor()})
    ce.save_plan(cid, patch)

    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="collection.enrichment.save",
        target=cid,
        details={k: v for k, v in patch.items() if k != "settings"} |
                ({"settings": patch["settings"]} if "settings" in patch else {}),
    )
    entry = ce.get_plan(cid)
    return jsonify({"status": "success", "armed": entry is not None,
                    "settings": (entry or {}).get("settings"),
                    "progress": ce.progress(cid, entry)})


@management_bp.route('/api/manage/collections/<collection_id>/enrichment/tick', methods=['POST'])
@permission_required('tab.data_management.edit_collections')
@login_required
def tick_collection_enrichment(collection_id):
    """Run one supervisor cycle now, serving only this collection.

    The manual escape hatch: works even while the site-wide switch is off (the
    explicit click is the authorization), so a plan can be tested end to end
    before automatic ticks are enabled.
    """
    from fyp.fyp_config import ENRICHMENT_SUPERVISOR_SCRIPT
    from ...services import collection_enrichment as ce
    from ...task_status import is_cloud_run

    cid = str(collection_id)
    if ce.get_plan(cid) is None:
        return jsonify({"error": "This collection has no enrichment plan. Save one first."}), 400

    activity_log.record(
        actor=_actor(), category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="collection.enrichment.tick", target=cid)

    if is_cloud_run():
        # What the supervisor's status file said BEFORE this dispatch. The tick
        # runs on the task-runner, so the only way the panel can report what it
        # decided is to poll until the status file's start_time is no longer
        # this one. Comparing against the previous value rather than against a
        # dispatch timestamp keeps that immune to clock skew between services.
        prev_start = (ce.last_tick() or {}).get("start_time")
        success, msg = start_process(
            "enrichment_supervisor",
            ENRICHMENT_SUPERVISOR_SCRIPT,
            args=["--collection-id", cid],
            task_args={"collection_id": cid},
            started_by=_actor(),
        )
        if success:
            return jsonify({"status": "started", "message": msg,
                            "prev_start_time": prev_start})
        return jsonify({"status": "error", "message": msg}), 409

    # Local mode: run the tick INLINE rather than as a subprocess. A worker the
    # tick starts must be spawned (and monitored) by this server process — a
    # short-lived supervisor subprocess would orphan it, killing its log/monitor
    # threads (same reason _run_local_downstream_pipeline runs in a server
    # thread). The tick itself is a couple of parquet reads, so synchronous is
    # fine, and the response can say what the tick actually did.
    from ...run_enrichment_supervisor import run_enrichment_supervisor

    class _InlineReporter:
        def __init__(self):
            self.lines: list[str] = []
            self.outcome: dict = {}

        def log(self, msg):
            self.lines.append(str(msg))
            print(f"[enrichment_supervisor] {msg}")

        def update_progress(self, pct, msg=""):
            pass

        def emit_data(self, data):
            if isinstance(data, dict):
                self.outcome.update(data)

    rep = _InlineReporter()
    try:
        run_enrichment_supervisor(rep, {"collection_id": cid})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc),
                        "log": rep.lines}), 500
    return jsonify({"status": "completed",
                    "action": rep.outcome.get("action"),
                    "outcome": rep.outcome,
                    "log": rep.lines})
