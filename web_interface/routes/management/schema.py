"""Variable schema / presentation endpoints (/api/manage/schema*, /api/manage/presentation)."""


import pandas as pd
from flask import jsonify, request
from flask_login import login_required

from fyp.fyp_config import (
    fyp_cf,
    load_var_schema,
)
from fyp.recode_variables import (
    SEMANTIC_COLUMNS,
    VAR_SCHEMA_ROLES,
    VAR_SCHEMA_SCALES,
    compute_var_schema_hash,
)

from ... import activity_log
from ...permissions import permission_required



from ...services.worker_status import (
    _actor,
)



from ._blueprint import management_bp


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """Convert the schema DataFrame to a list of plain-dict records,
    coercing nulls to empty strings so the JSON payload is stable shape.
    """
    out: list[dict] = []
    for _, row in df.iterrows():
        rec = {}
        for col in df.columns:
            val = row[col]
            try:
                if pd.isna(val):
                    rec[col] = ""
                    continue
            except (TypeError, ValueError):
                pass
            rec[col] = "" if val is None else str(val)
        out.append(rec)
    return out



def _var_schema_admin_enabled() -> bool:
    """Off-switch for the schema admin UI.

    Defaults to True; set ``[features].var_schema_admin = false`` in
    ``config.toml`` to disable without redeploying.  Permission gate
    (``tab.admin.schema``) is still required on top of this.
    """
    features = fyp_cf.get("features") or {}
    return bool(features.get("var_schema_admin", True))



def _contract_locked_map(df) -> dict:
    """Return ``{variable_name: {metadata, section}}`` for contract-owned cells.

    ``metadata`` is True when a contract owns the row's role/scale/display_name/
    description — the annotation contract's flattened Gemini columns, or the
    scrape / activity / derived contracts' canonical columns. ``section`` is True
    for every Gemini-origin row (all forced under "AI Annotations") and for every
    scrape / activity / derived contract column (whose section those contracts
    own). The admin editor renders these cells read-only. Degrades to ``{}`` if no
    contract can be loaded, so the editor never breaks on a contract error.
    """
    annotation_cols: set = set()
    scrape_cols: set = set()
    activity_cols: set = set()
    derived_cols: set = set()
    try:
        from fyp import annotation_contract as ac

        annotation_cols = set(ac.contract_column_metadata(ac.load_contract()).keys())
    except Exception:
        pass
    try:
        from fyp import scrape_contract as sc

        scrape_cols = set(sc.contract_column_metadata(sc.load_contract()).keys())
    except Exception:
        pass
    try:
        from fyp import activity_contract as acy

        activity_cols = set(acy.contract_column_metadata(acy.load_contract()).keys())
    except Exception:
        pass
    try:
        from fyp import derived_contract as dc

        derived_cols = set(dc.contract_column_metadata(dc.load_contract()).keys())
    except Exception:
        pass
    # Legacy fields owned by past-version registry snapshots (e.g. trend /
    # australian_relevance for annotation; any future retired scrape/activity
    # field) — contract-owned/read-only, and badged "legacy" in the editor. A
    # field a CURRENT contract still owns is NOT legacy.
    legacy_cols: set = set()
    try:
        from fyp import annotation_versioning as av

        legacy_cols |= set(av.union_field_metadata().keys()) - annotation_cols
    except Exception:
        pass
    try:
        from fyp import scrape_versioning as sv

        legacy_scrape = set(sv.union_field_metadata().keys()) - scrape_cols
        legacy_cols |= legacy_scrape
        scrape_cols |= legacy_scrape
    except Exception:
        pass
    try:
        from fyp import activity_versioning as av_act

        legacy_activity = set(av_act.union_field_metadata().keys()) - activity_cols
        legacy_cols |= legacy_activity
        activity_cols |= legacy_activity
    except Exception:
        pass
    if not (annotation_cols or scrape_cols or activity_cols or derived_cols or legacy_cols):
        return {}
    section_owned_cols = scrape_cols | activity_cols | derived_cols
    locked: dict = {}
    for _, row in df.iterrows():
        vn = str(row.get("variable_name", ""))
        src = str(row.get("source", "")).strip()
        is_gemini = src == "Gemini" or src.startswith("derived: Gemini")
        section_owned = vn in section_owned_cols
        is_legacy = vn in legacy_cols
        meta_owned = (
            vn in annotation_cols or vn in scrape_cols
            or vn in activity_cols or vn in derived_cols or is_legacy
        )
        if meta_owned or is_gemini:
            entry = {"metadata": meta_owned, "section": is_gemini or section_owned}
            if is_legacy:
                entry["legacy"] = True
            locked[vn] = entry
    return locked




@management_bp.route('/api/manage/schema', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def get_schema():
    if not _var_schema_admin_enabled():
        return jsonify({"error": "schema admin disabled"}), 503
    """Return the current schema for the admin editor.

    ``?force_reload=1`` re-reads ``var_schema.csv`` from disk/GCS before
    responding so the editor's Reload button picks up direct edits made
    outside the UI (e.g. ``gsutil cp``).  The initial tab load and the
    post-save refresh omit the flag — they only need in-memory state.
    """
    try:
        from fyp import var_presentation as vp
        from fyp import annotation_contract as ac

        if request.args.get("force_reload") in ("1", "true", "yes"):
            global fyp_cf
            fyp_cf = load_var_schema(fyp_cf, verbose=False)
        df = fyp_cf["var_schema"]
        presentation = vp.load_presentation() or vp.empty_presentation()
        # The annotation contract can be edited at runtime; reflect its live
        # source so the read-only tooltips point at the right place.
        ac_source = ac.contract_status().get("source")
        contract_path = (
            f"{ac.RUNTIME_FILENAME} (runtime)" if ac_source == "runtime"
            else "config/annotation_contract.toml (baked)"
        )
        return jsonify({
            "rows": _df_to_records(df),
            "columns": list(df.columns),
            "semantic_columns": list(SEMANTIC_COLUMNS),
            "enums": {
                "role": sorted(VAR_SCHEMA_ROLES),
                "scale": sorted(VAR_SCHEMA_SCALES),
            },
            "contract_locked": _contract_locked_map(df),
            "contract_path": contract_path,
            "scrape_contract_path": "config/scrape_contract.toml",
            # The presentation store is the only admin-editable payload left
            # (the metadata is contract-owned); its etag guards saves.
            "presentation": presentation.get("surfaces", {}),
            "prio_columns": dict(vp.SURFACE_TO_PRIO_COLUMN),
            "etag": vp.compute_presentation_etag(presentation),
            "current_hash": compute_var_schema_hash(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@management_bp.route('/api/manage/schema/validate', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def validate_schema_endpoint():
    """Retired: metadata is contract-owned; only presentation flags are editable."""
    return jsonify({
        "error": "retired",
        "message": "var_schema metadata is contract-owned; edit the contract TOMLs. "
                   "Presentation flags save via POST /api/manage/presentation.",
    }), 410



@management_bp.route('/api/manage/schema', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def save_schema_endpoint():
    """Retired: metadata is contract-owned; only presentation flags are editable."""
    return jsonify({
        "error": "retired",
        "message": "var_schema metadata is contract-owned; edit the contract TOMLs. "
                   "Presentation flags save via POST /api/manage/presentation.",
    }), 410



@management_bp.route('/api/manage/presentation', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def save_presentation_endpoint():
    """Persist the global web-surface membership flags (the admin defaults).

    Body: ``{"surfaces": {filter|timeline|viz|display: [variable_name, ...]},
    "etag": <presentation etag from GET /api/manage/schema>}``. Refuses on a
    stale etag (409) or unknown variable names (400). Presentation edits can
    never change the study hash — asserted server-side as a guard.
    """
    global fyp_cf
    if not _var_schema_admin_enabled():
        return jsonify({"error": "schema admin disabled"}), 503
    try:
        from fyp import var_presentation as vp

        body = request.get_json(force=True, silent=False) or {}
        surfaces = body.get("surfaces")
        etag = body.get("etag")
        if not isinstance(surfaces, dict):
            return jsonify({"error": "surfaces must be an object"}), 400
        known = set(fyp_cf["var_schema"]["variable_name"].astype("string"))
        unknown = sorted({
            n for names in surfaces.values() if isinstance(names, list)
            for n in names if n not in known
        })
        if unknown:
            return jsonify({"error": "unknown variables", "unknown": unknown}), 400

        old_hash = compute_var_schema_hash()
        try:
            result = vp.save_presentation(surfaces, expected_etag=etag, updated_by=_actor())
        except vp.PresentationConflict as e:
            return jsonify({
                "error": "conflict",
                "message": str(e),
                "etag": vp.compute_presentation_etag(),
            }), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        fyp_cf = load_var_schema(fyp_cf, verbose=False)
        new_hash = compute_var_schema_hash()
        hash_changed = new_hash != old_hash
        if hash_changed:
            # Presentation flags are excluded from the hash by design; a change
            # here means something else drifted — surface it loudly.
            print(f"WARNING: presentation save changed the schema hash ({old_hash[:16]} -> {new_hash[:16]}).")
        activity_log.record(
            actor=_actor(),
            category="admin",
            action="var_presentation.save",
            details={"hash_changed": hash_changed},
        )
        return jsonify({"etag": result["etag"], "hash_changed": hash_changed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


