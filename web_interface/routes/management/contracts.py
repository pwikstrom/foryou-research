"""Annotation contract + version registry endpoints (/api/manage/annotation-*)."""

from datetime import UTC, datetime

from flask import jsonify, request
from flask_login import login_required
from werkzeug.utils import secure_filename

import fyp.data_io as data_io
from fyp.fyp_config import (
    fyp_cf,
    load_var_schema,
)
import fyp.annotation_versioning as annotation_versioning
from fyp.machine_annotation import rebuild_active_annotations_from_archive

from ... import activity_log
from ...data_service import (
    study_cache,
)
from ...permissions import permission_required



from ...services.worker_status import (
    _actor,
)



from ._blueprint import management_bp
from .schema import _var_schema_admin_enabled


@management_bp.route('/api/manage/annotation-versions', methods=['GET'])
@permission_required('tab.admin.versions', 'tab.admin.ab_eval')
@login_required
def list_annotation_versions():
    """List recorded annotation versions and the active one."""
    try:
        return jsonify({
            "versions": annotation_versioning.list_versions(),
            "active": annotation_versioning.get_active_version(),
            "current": annotation_versioning.current_annotation_version(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@management_bp.route('/api/manage/annotation-versions/<version>', methods=['GET'])
@permission_required('tab.admin.versions', 'tab.admin.ab_eval')
@login_required
def get_annotation_version(version):
    """Return one version's full record, including its prompt + schema snapshot."""
    try:
        registry = annotation_versioning.load_registry()
        info = registry.get("versions", {}).get(version)
        if info is None:
            return jsonify({"error": "unknown version"}), 404
        # The legacy version predates per-version prompt snapshots; surface the
        # historical file-based prompt so "View" isn't empty for it.
        if version == annotation_versioning.LEGACY_VERSION and not info.get("prompt_text"):
            legacy_prompt = annotation_versioning.legacy_prompt_text()
            if legacy_prompt:
                info = {**info, "prompt_text": legacy_prompt}

        # Restore ("Make current") support: the record must carry its source
        # contract TOML, and the backend selection it ran under must still be
        # switchable-to for an exact restore. The target is the version's
        # variant/backend (gemini when unset).
        from flask_login import current_user
        from ...permissions import user_has_permission

        restorable = (bool(info.get("contract_text"))
                      and version != annotation_versioning.LEGACY_VERSION)
        target = info.get("variant") or info.get("backend") or "gemini"
        restore = {
            "restorable": restorable,
            "target": target,
            "backend": {
                **_backend_target_info(target),
                "can_switch_backend": user_has_permission(current_user, 'tab.admin.backends'),
            },
        }
        return jsonify({
            "version": version,
            "active": registry.get("active") == version,
            "current": annotation_versioning.current_annotation_version(),
            "record": info,
            "restore": restore,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@management_bp.route('/api/manage/annotation-versions/activate', methods=['POST'])
@permission_required('tab.admin.versions', 'tab.admin.ab_eval')
@login_required
def activate_annotation_version():
    """Activate a version and rebuild the global active dataset.

    Updates the registry, re-derives ``machine_annotations_recoded.parquet`` from
    the version archive (fast — no re-refinement), and clears the study RAM
    cache. Per-study datasets still need a study refresh to fully reflect the
    activation.
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        version = body.get("version")
        if not version:
            return jsonify({"error": "version is required"}), 400
        previous_version = annotation_versioning.get_active_version()
        try:
            annotation_versioning.promote_version(version)
        except KeyError:
            return jsonify({"error": f"unknown version: {version}"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        rebuilt = rebuild_active_annotations_from_archive(verbose=False)
        with study_cache.lock:
            study_cache.cache.clear()

        # Persist a promotion marker so the "studies need refresh" signal
        # survives page reloads; the staleness evaluator clears it once
        # recode_refresh_studies succeeds after this timestamp. Reload the
        # shared stats first (cross-service file — never clobber the runner).
        try:
            from ...process_manager import load_process_stats, process_stats, save_process_stats

            load_process_stats()
            entry = process_stats.setdefault("annotation_versions", {})
            entry["promotion_impact"] = {
                "timestamp": datetime.now(UTC).isoformat(),
                "version": version,
                "previous_version": previous_version,
            }
            save_process_stats()
        except Exception:
            pass

        activity_log.record(
            actor=_actor(),
            category="admin",
            action="annotation_version.activate",
            details={"version": version, "active_rows": rebuilt},
        )
        return jsonify({
            "ok": True,
            "active": version,
            "active_rows": rebuilt,
            "staleness": {"studies_stale": True},
            "note": "Global active annotations rebuilt. Refresh studies to apply to per-study datasets.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



def _annotation_contract_impact(cand_contract: dict, target_backend: str | None = None) -> dict:
    """Predict the version impact of activating ``cand_contract``.

    Renders the candidate prompt + response schema exactly the way the annotator
    would and compares the resulting ``av_`` descriptor to the current one, so
    the admin sees "metadata-only — no new version" vs "a new version will be
    minted" before confirming. Also reports the field-name delta.

    Args:
        cand_contract: The parsed candidate contract dict.
        target_backend: The backend selection the candidate would run under
            (default: the active selection). The candidate descriptor is built
            against it — mirroring ``current_version_descriptor``'s branching,
            including the byte-identical legacy path for plain ``gemini``.
    """
    from fyp import annotation_contract as ac
    from fyp import annotation_schema as sch
    from fyp.annotation.backends import active_backend_name, get_backend

    active_selection = active_backend_name()
    selection = target_backend or active_selection
    backend = None
    if selection != "gemini":
        try:
            backend = get_backend(selection)
        except Exception:
            # Unimportable backend (e.g. local-only deps missing) — same
            # fallback current_version_descriptor uses.
            backend, selection = None, "gemini"

    cand_prompt = sch.build_prompt(cand_contract)
    cand_schema = sch.get_annotation_json_schema(cand_contract)
    if backend is None:
        machine = fyp_cf["machine"]["gemini"]
        target_model = machine.get("model")
        gen_params = {k: machine.get(k) for k in annotation_versioning._VERSION_GEN_PARAM_KEYS}
        cand = annotation_versioning.build_version_descriptor(
            target_model, cand_prompt, cand_schema, gen_params)
    else:
        target_model = backend.effective_model_id()
        cand = annotation_versioning.build_version_descriptor(
            model=target_model,
            prompt_text=cand_prompt + backend.prompt_suffix(),
            schema_json=cand_schema,
            gen_params=backend.version_gen_params(),
            extra_params=backend.version_extra_params(),
            backend=backend.name,
            variant=selection if selection != backend.name else None,
        )

    cur = annotation_versioning.current_version_descriptor(fresh=True)
    cur_names = {f.get("name") for f in ac.load_contract().get("fields", [])}
    cand_names = {f.get("name") for f in cand_contract.get("fields", [])}
    version_changed = cand["annotation_version"] != cur.get("annotation_version")
    return {
        "current_version": cur.get("annotation_version"),
        "candidate_version": cand["annotation_version"],
        "prompt_changed": cand["prompt_hash"] != cur.get("prompt_hash"),
        "schema_changed": cand["schema_hash"] != cur.get("schema_hash"),
        "version_changed": version_changed,
        "metadata_only": not version_changed,
        "fields_added": sorted(n for n in (cand_names - cur_names) if n),
        "fields_removed": sorted(n for n in (cur_names - cand_names) if n),
        "use_generated_prompt": True,
        "use_structured_output": True,
        "target_backend": selection,
        "target_model": target_model,
        "active_backend": active_selection,
        "active_model": cur.get("model"),
        "backend_mismatch": selection != active_selection,
    }




def _backend_target_info(target: str | None) -> dict:
    """Resolve graduation-backend info for the dry-run modal and confirm guard.

    Args:
        target: The requested backend selection (a backend id or declared
            variant name), or ``None`` for the active selection.

    Returns:
        ``{active, active_model, target, target_model, mismatch,
        target_available, target_unavailable_reason}``. ``target_available``
        is False for an unknown/unimportable selection, a failing availability
        check, or a local-only backend on Cloud Run.
    """
    from fyp.annotation.backends import active_backend_name, get_backend
    from ...task_status import is_cloud_run

    active = active_backend_name()
    info = {
        "active": active,
        "active_model": None,
        "target": target or active,
        "target_model": None,
        "mismatch": bool(target) and target != active,
        "target_available": True,
        "target_unavailable_reason": "",
    }
    try:
        info["active_model"] = get_backend(active).effective_model_id()
    except Exception:
        pass
    try:
        backend = get_backend(info["target"])
        info["target_model"] = backend.effective_model_id()
    except ValueError as exc:
        info["target_available"] = False
        info["target_unavailable_reason"] = str(exc)
        return info
    # Gemini readiness is checked by the worker's config gate (mirrors
    # ab_eval._validate_arm_backend); other backends probe availability here.
    if backend.name != "gemini":
        result = backend.availability(deep=False)
        if not result.ok:
            info["target_available"] = False
            info["target_unavailable_reason"] = result.reason
            return info
    if is_cloud_run() and not backend.cloud_run_capable:
        info["target_available"] = False
        info["target_unavailable_reason"] = (
            f"the '{info['target']}' backend runs only on a local machine "
            f"and cannot be the active backend on Cloud Run")
    return info




@management_bp.route('/api/manage/annotation-contract', methods=['GET'])
@permission_required('tab.admin.versions', 'tab.admin.ab_eval')
@login_required
def get_annotation_contract():
    """Return the effective-contract status for the admin card."""
    try:
        from fyp import annotation_contract as ac

        status = ac.contract_status()
        return jsonify({
            **status,
            "current_version": annotation_versioning.current_annotation_version(),
            "runtime_filename": ac.RUNTIME_FILENAME,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/annotation-contract/download', methods=['GET'])
@permission_required('tab.admin.versions', 'tab.admin.ab_eval')
@login_required
def download_annotation_contract():
    """Download the effective contract (runtime file if present, else baked)."""
    try:
        from flask import Response
        from fyp import annotation_contract as ac

        text = ac.effective_contract_text()
        return Response(
            text,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{ac.RUNTIME_FILENAME}"'},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/annotation-contract/parsed', methods=['GET'])
@permission_required('tab.admin.versions', 'tab.admin.ab_eval')
@login_required
def get_annotation_contract_parsed():
    """Return the effective contract as a parsed dict, for form-editor hydration.

    The dict is exactly the parsed-TOML shape the pipeline consumes, so the
    editor's model can never diverge from what ``build_prompt`` /
    ``build_response_schema`` see. ``help`` carries the editor's per-input help
    texts (``config/annotation_contract_help.toml``).
    """
    try:
        from fyp import annotation_contract as ac

        text = ac.effective_contract_text()
        contract, errors = ac.parse_and_validate(text)
        if contract is None:
            return jsonify({"error": "effective contract does not parse", "errors": errors}), 500
        status = ac.contract_status()
        try:
            from fyp.recode_variables import VAR_SCHEMA_ROLES, VAR_SCHEMA_SCALES
            roles, scales = list(VAR_SCHEMA_ROLES), list(VAR_SCHEMA_SCALES)
        except Exception:
            roles, scales = [], []
        return jsonify({
            "contract": contract,
            "etag": status.get("etag"),
            "source": status.get("source"),
            "errors": errors,
            "help": ac.contract_help(),
            "roles": roles,
            "scales": scales,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/annotation-contract/rendered', methods=['GET'])
@permission_required('tab.admin.versions', 'tab.admin.ab_eval')
@login_required
def rendered_annotation_contract():
    """Render the LIVE contract's generated prompt + response schema.

    Lets the Versions page show what the next annotation run will send even
    before any version has been minted (versions are only registered when
    annotation actually runs).
    """
    try:
        from fyp import annotation_contract as ac
        from fyp import annotation_schema as sch

        text = ac.effective_contract_text()
        contract, errors = ac.parse_and_validate(text)
        if contract is None:
            return jsonify({"error": "effective contract does not parse", "errors": errors}), 500
        return jsonify({
            "version": annotation_versioning.current_annotation_version(),
            "prompt": sch.build_prompt(contract),
            "schema": sch.get_annotation_json_schema(contract),
            # Model + generation settings the next run would be stamped with —
            # lets the Versions page show settings for the not-yet-minted row.
            "descriptor": annotation_versioning.current_version_descriptor(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/annotation-contract/preview', methods=['POST'])
@permission_required('tab.admin.versions', 'tab.admin.ab_eval')
@login_required
def preview_annotation_contract():
    """Render a candidate contract's prompt + response schema, without side effects.

    Body: ``{"contract": {...}}`` (the parsed-dict shape). Returns
    ``{valid, prompt, schema}`` on success or ``{valid: False, errors}`` when
    the candidate fails validation — always HTTP 200, so the editor's
    debounced live preview can show errors inline without console noise.
    Never touches the live snapshot (explicit-contract rendering seam).
    """
    try:
        from fyp import annotation_contract as ac
        from fyp import annotation_schema as sch

        body = request.get_json(silent=True) or {}
        cand = body.get('contract')
        if not isinstance(cand, dict):
            return jsonify({"error": "body must include a 'contract' object"}), 400
        errors = ac.validate_contract(cand)
        if errors:
            return jsonify({"valid": False, "errors": errors})
        return jsonify({
            "valid": True,
            "prompt": sch.build_prompt(cand),
            "schema": sch.get_annotation_json_schema(cand),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/annotation-contract', methods=['POST'])
@permission_required('tab.admin.versions', 'tab.admin.ab_eval')
@login_required
def upload_annotation_contract():
    """Validate + (optionally confirm) an uploaded annotation contract.

    Two-step: without ``confirm`` this validates the TOML and returns a
    version-impact report (dry run); with ``confirm`` it etag-guards, backs up
    the previous runtime contract, persists the new one, refreshes the snapshot,
    and rebuilds the in-memory schema. The candidate arrives as a multipart
    ``file``, a ``text`` form/JSON field, or a JSON ``contract`` dict (the form
    editor) — the latter is serialized to TOML server-side against the current
    effective text so comments on untouched keys survive, then flows through
    the exact same validate → impact → confirm pipeline.
    """
    global fyp_cf
    if not _var_schema_admin_enabled():
        return jsonify({"error": "schema admin disabled"}), 503
    try:
        from fyp import annotation_contract as ac

        json_body = request.get_json(silent=True) or {}

        # 1. Candidate TOML text — multipart file wins, else a raw text field,
        #    else a parsed-dict 'contract' payload serialized server-side.
        text = None
        original_filename = None
        files = [f for f in (request.files.getlist('file') + request.files.getlist('files')) if f and f.filename]
        if files:
            original_filename = secure_filename(files[0].filename)
            try:
                text = files[0].read().decode('utf-8')
            except UnicodeDecodeError:
                return jsonify({"error": "file is not valid UTF-8 text"}), 400
        else:
            text = request.form.get('text') or json_body.get('text')
            if not text and isinstance(json_body.get('contract'), dict):
                try:
                    text = ac.serialize_contract(
                        json_body['contract'], base_text=ac.effective_contract_text()
                    )
                except ValueError as e:
                    return jsonify({"valid": False, "errors": [str(e)]}), 400
                original_filename = "(form editor)"
        if not text or not text.strip():
            return jsonify({"error": "no contract text provided"}), 400

        # 2. Validate before doing anything else.
        cand, errors = ac.parse_and_validate(text)
        if errors:
            return jsonify({"valid": False, "errors": errors}), 400

        # 3. Optional backend switch (the Playground graduation path) — resolve
        #    and validate BEFORE any write, and compute the impact against the
        #    backend the contract will actually run on.
        switch_backend = (request.form.get('switch_backend')
                          or json_body.get('switch_backend') or "").strip() or None
        if switch_backend:
            from flask_login import current_user
            from ...admin_settings import validate_setting_value
            from ...permissions import user_has_permission
            from fyp.annotation.backends.settings import ANNOTATION_BACKEND_KEY

            if not user_has_permission(current_user, 'tab.admin.backends'):
                return jsonify({
                    "error": "forbidden",
                    "message": "Switching the annotation backend requires the Backends admin permission.",
                }), 403
            err = validate_setting_value(ANNOTATION_BACKEND_KEY, switch_backend)
            if err:
                return jsonify({"error": err}), 400
            binfo = _backend_target_info(switch_backend)
            if not binfo["target_available"]:
                return jsonify({
                    "error": f"backend '{switch_backend}' cannot be activated: "
                             f"{binfo['target_unavailable_reason']}",
                }), 400

        # Version-impact dry-run report (against the target backend).
        impact = _annotation_contract_impact(cand, target_backend=switch_backend)

        def _flag(v) -> bool:
            return str(v).strip().lower() in ('1', 'true', 'yes')

        confirm = _flag(request.form.get('confirm', '')) or bool(json_body.get('confirm'))
        if not confirm:
            return jsonify({"valid": True, "confirm_required": True, "impact": impact})

        # 4. Confirm: etag guard against a concurrent change.
        expected_etag = request.form.get('expected_etag') or json_body.get('expected_etag')
        current_etag = ac.contract_status().get("etag")
        if expected_etag and current_etag and expected_etag != current_etag:
            return jsonify({
                "error": "conflict",
                "message": "The contract changed since you loaded it. Reload and retry.",
                "etag": current_etag,
            }), 409

        # 5. Back up the existing runtime contract (if any) before overwriting.
        backup_name = None
        if data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME):
            prev = data_io.load_text(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
            if prev is not None:
                ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                backup_name = f"{ac.BACKUP_PREFIX}{ts}.toml"
                data_io.save_text(prev, storage_location=ac.RUNTIME_LOCATION, filename=backup_name)

        # 6. Persist the new contract + audit metadata.
        data_io.save_text(text, storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
        data_io.save_json(
            data={
                "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "updated_by": _actor(),
                "original_filename": original_filename,
            },
            storage_location=ac.RUNTIME_LOCATION,
            filename=ac.RUNTIME_META_FILENAME,
        )

        # 6b. Apply the requested backend switch (validated above) alongside
        #     the contract write, so the deployed contract × backend pair
        #     matches what was tested in the Playground.
        backend_switched = None
        if switch_backend:
            from fyp.annotation.backends import active_backend_name
            from fyp.annotation.backends.settings import ANNOTATION_BACKEND_KEY
            from ...admin_settings import load_admin_settings, save_admin_settings

            prev_backend = active_backend_name()
            if switch_backend != prev_backend:
                settings = load_admin_settings()
                settings[ANNOTATION_BACKEND_KEY] = switch_backend
                save_admin_settings(settings)
                backend_switched = {"from": prev_backend, "to": switch_backend}

        # 7. Refresh the snapshot + rebuild the schema so overlays pick up new
        #    metadata; clear the study RAM cache (recode/metadata may change).
        ac.refresh_runtime_contract()
        fyp_cf = load_var_schema(fyp_cf, verbose=False)
        with study_cache.lock:
            study_cache.cache.clear()

        # 8. Mint the new version eagerly so it appears on the Versions page
        #    (and can be preferred) without waiting for the first annotation
        #    run. The descriptor cache self-busts on the new contract etag /
        #    backend; ensure_current_version_registered is idempotent and
        #    never raises.
        minted_version = annotation_versioning.ensure_current_version_registered()

        activity_log.record(
            actor=_actor(),
            category="admin",
            action="annotation_contract.upload",
            details={
                "impact": impact,
                "backup": backup_name,
                "original_filename": original_filename,
                **({"switch_backend": backend_switched} if backend_switched else {}),
            },
        )
        new_status = ac.contract_status()
        switch_note = (
            f" Annotation backend switched to '{backend_switched['to']}'."
            if backend_switched else ""
        )
        return jsonify({
            "ok": True,
            "source": new_status.get("source"),
            "etag": new_status.get("etag"),
            "impact": impact,
            "backup": backup_name,
            "backend_switched": backend_switched,
            "minted_version": minted_version,
            "note": (
                f"Contract activated. New annotation version {minted_version} has been "
                f"registered — make it preferred under Versions when ready.{switch_note}"
                if impact.get("version_changed")
                else f"Contract activated (metadata-only change — no new annotation version).{switch_note}"
            ),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/annotation-contract/revert', methods=['POST'])
@permission_required('tab.admin.versions', 'tab.admin.ab_eval')
@login_required
def revert_annotation_contract():
    """Revert to the baked contract by archiving + removing the runtime file."""
    global fyp_cf
    if not _var_schema_admin_enabled():
        return jsonify({"error": "schema admin disabled"}), 503
    try:
        from fyp import annotation_contract as ac

        if not data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME):
            return jsonify({"ok": True, "source": "baked", "note": "Already on the baked contract."})

        backup_name = None
        prev = data_io.load_text(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
        if prev is not None:
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_name = f"{ac.BACKUP_PREFIX}{ts}.toml"
            data_io.save_text(prev, storage_location=ac.RUNTIME_LOCATION, filename=backup_name)

        data_io.remove(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
        if data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_META_FILENAME):
            data_io.remove(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_META_FILENAME)

        ac.refresh_runtime_contract()
        fyp_cf = load_var_schema(fyp_cf, verbose=False)
        with study_cache.lock:
            study_cache.cache.clear()

        # Mint the (restored) baked contract's version eagerly — same
        # rationale as the upload path.
        annotation_versioning.ensure_current_version_registered()

        activity_log.record(
            actor=_actor(),
            category="admin",
            action="annotation_contract.revert",
            details={"backup": backup_name},
        )
        return jsonify({
            "ok": True,
            "source": ac.contract_status().get("source"),
            "backup": backup_name,
            "note": "Reverted to the baked contract.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




# ---------------------------------------------------------------------------
# A/B contract evaluation (candidates, eval set, runs). See fyp/ab_eval.py.
# All results live in the isolated 'ab_eval' storage location — never in the
# machine-annotation archive or studies.
# ---------------------------------------------------------------------------


