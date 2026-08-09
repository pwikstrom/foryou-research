"""Prompt A/B testing endpoints (/api/manage/ab-*)."""


from flask import jsonify, request
from flask_login import login_required


from ... import activity_log
from ...process_manager import (
    start_process,
)
from ...permissions import permission_required



from ...services.worker_status import (
    _actor,
    _is_worker_running,
)



import fyp.annotation_versioning as annotation_versioning

from ._blueprint import management_bp
from .contracts import (
    _annotation_contract_impact,
    _backend_target_info,
    candidate_version_descriptor,
)


# Reserved pseudo-candidate name for the shipped (baked) default contract.
# The Playground shows it as a permanent row so the default can be inspected,
# duplicated and graduated (= revert) like any candidate. A pre-existing
# STORED candidate with this name wins for back-compat reads, but new saves
# under the name are rejected.
DEFAULT_CANDIDATE = "default"




def _default_candidate_payload():
    """Return ``{name, text, contract, builtin}`` for the baked contract."""
    from fyp import annotation_contract as ac

    text = ac._read_baked_text()
    cand, errors = ac.parse_and_validate(text)
    if cand is None or errors:
        raise ValueError(f"baked contract does not validate: {'; '.join(errors or [])}")
    return {"name": DEFAULT_CANDIDATE, "text": text, "contract": cand, "builtin": True}




def _live_version_of(contract: dict) -> str | None:
    """The ``av_`` a contract would produce under the active backend, or None."""
    try:
        descriptor, _, _ = candidate_version_descriptor(contract)
        return descriptor["annotation_version"]
    except Exception:
        return None




@management_bp.route('/api/manage/ab-candidates', methods=['GET'])
@permission_required('tab.admin.ab_eval')
@login_required
def list_ab_candidates():
    """List stored contracts for the Playground's contracts table.

    Each candidate's ``version`` is computed LIVE (the stored
    ``candidate_version`` is a save-time snapshot that goes stale the moment
    the backend or model changes). Also returns summaries of the built-in
    ``default_contract`` and the ``active_contract``, which the table pins as
    rows so every contract in play is visible in one place.
    """
    try:
        from fyp import ab_eval
        from fyp import annotation_contract as ac

        candidates = []
        for meta in ab_eval.list_candidates():
            version = None
            try:
                version = _live_version_of(ab_eval.load_candidate(meta["name"])["contract"])
            except Exception:
                pass
            candidates.append({**meta, "version": version or meta.get("candidate_version")})

        default_summary = None
        try:
            payload = _default_candidate_payload()
            default_summary = {
                "name": DEFAULT_CANDIDATE,
                "version": _live_version_of(payload["contract"]),
                "n_fields": len(payload["contract"].get("fields", [])),
            }
        except Exception:
            pass

        active_summary = None
        try:
            status = ac.contract_status()
            active_contract = ac.load_contract()
            active_summary = {
                "version": annotation_versioning.active_annotation_version(),
                "n_fields": len(active_contract.get("fields", [])),
                "source": status.get("source"),
                "updated_at": status.get("updated_at"),
                "updated_by": status.get("updated_by"),
            }
        except Exception:
            pass

        return jsonify({"candidates": candidates,
                        "default_contract": default_summary,
                        "active_contract": active_summary})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-candidates', methods=['POST'])
@permission_required('tab.admin.ab_eval')
@login_required
def save_ab_candidate():
    """Create/overwrite a named candidate contract.

    Body: ``{name, text | contract, note?, overwrite?}`` — ``text`` is raw
    TOML; a ``contract`` dict is serialized server-side against the current
    effective text (the form editor's save-as-candidate path). The candidate
    is validated and stamped with its etag + predicted ``av_`` version.
    """
    try:
        from fyp import ab_eval
        from fyp import annotation_contract as ac

        body = request.get_json(silent=True) or {}
        name = str(body.get('name') or "").strip()
        if name == DEFAULT_CANDIDATE:
            return jsonify({"error": f"'{DEFAULT_CANDIDATE}' is reserved for the "
                                     f"shipped default contract — pick another name"}), 400
        text = body.get('text')
        if not text and isinstance(body.get('contract'), dict):
            try:
                text = ac.serialize_contract(body['contract'], base_text=ac.effective_contract_text())
            except ValueError as e:
                return jsonify({"valid": False, "errors": [str(e)]}), 400
        if not text or not str(text).strip():
            return jsonify({"error": "no contract text provided"}), 400

        cand, errors = ac.parse_and_validate(text)
        if errors:
            return jsonify({"valid": False, "errors": errors}), 400

        candidate_version = _annotation_contract_impact(cand).get("candidate_version")
        try:
            meta = ab_eval.save_candidate(
                name, text, actor=_actor(), note=str(body.get('note') or ""),
                overwrite=bool(body.get('overwrite')), candidate_version=candidate_version,
            )
        except FileExistsError:
            return jsonify({"error": f"candidate '{name}' exists — pass overwrite=true"}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        activity_log.record(actor=_actor(), category="admin",
                            action="ab_candidate.save", details={"name": name})
        return jsonify({"ok": True, "meta": meta})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-candidates/<name>', methods=['GET'])
@permission_required('tab.admin.ab_eval')
@login_required
def get_ab_candidate(name):
    """Return one candidate's text + parsed contract + metadata.

    The reserved ``default`` name serves the shipped baked contract when no
    stored candidate shadows it (pre-reservation back-compat).
    """
    try:
        from fyp import ab_eval

        try:
            return jsonify(ab_eval.load_candidate(name))
        except FileNotFoundError:
            if name == DEFAULT_CANDIDATE:
                return jsonify(_default_candidate_payload())
            return jsonify({"error": f"candidate '{name}' not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-candidates/<name>', methods=['DELETE'])
@permission_required('tab.admin.ab_eval')
@login_required
def delete_ab_candidate(name):
    """Delete a candidate contract."""
    try:
        from fyp import ab_eval

        removed = ab_eval.delete_candidate(name)
        if removed:
            activity_log.record(actor=_actor(), category="admin",
                                action="ab_candidate.delete", details={"name": name})
        return jsonify({"ok": removed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-candidates/<name>/activate', methods=['POST'])
@permission_required('tab.admin.ab_eval')
@login_required
def activate_ab_candidate(name):
    """Dry-run a candidate for graduation.

    Returns the candidate's TOML text + the standard version-impact report;
    the UI then drives the NORMAL contract-confirm POST with that text, so
    graduation is exactly the upload flow (etag guard, backup, versioning).
    Optional JSON body ``{"backend": "<selection>"}`` names the backend the
    candidate was tested on (from the run manifest) — the response's
    ``backend`` block then reports the target vs the active backend so the
    modal can offer the "also switch backend" checkbox, and the impact is
    computed against the backend the contract would actually run on.
    """
    try:
        from flask_login import current_user
        from fyp import ab_eval
        from fyp import annotation_contract as ac
        from fyp.annotation.backends import variants

        from ...permissions import user_has_permission

        builtin_default = False
        try:
            cand = ab_eval.load_candidate(name)
        except FileNotFoundError:
            if name == DEFAULT_CANDIDATE:
                cand = _default_candidate_payload()
                builtin_default = True
            else:
                return jsonify({"error": f"candidate '{name}' not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 422

        body = request.get_json(force=True, silent=True) or {}
        requested = str(body.get("backend") or "").strip() or None
        if requested and requested not in variants.selection_ids():
            return jsonify({"error": f"unknown backend selection: {requested}"}), 400

        binfo = _backend_target_info(requested)
        can_switch = user_has_permission(current_user, 'tab.admin.backends')
        # Impact reflects the backend the contract will actually run on:
        # the target only when the switch can really happen.
        switchable = binfo["mismatch"] and can_switch and binfo["target_available"]
        impact = _annotation_contract_impact(
            cand["contract"],
            target_backend=binfo["target"] if switchable or not binfo["mismatch"] else None,
        )
        return jsonify({
            "name": name,
            "text": cand["text"],
            "impact": impact,
            "backend": {**binfo, "can_switch_backend": can_switch},
            "current_etag": ac.contract_status().get("etag"),
            # The builtin default graduates via the revert endpoint (removes
            # the runtime override so future shipped updates apply), not via
            # a contract upload — the client branches on this flag.
            "builtin_default": builtin_default,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-sets', methods=['GET'])
@permission_required('tab.admin.ab_eval')
@login_required
def list_ab_eval_sets():
    """Return every named evaluation set plus the active one."""
    try:
        from fyp import ab_eval

        return jsonify(ab_eval.list_eval_sets())
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-sets', methods=['POST'])
@permission_required('tab.admin.ab_eval')
@login_required
def create_ab_eval_set():
    """Create a new (optionally cloned) evaluation set. Body: ``{name, copy_from?}``."""
    try:
        from fyp import ab_eval

        body = request.get_json(silent=True) or {}
        name = str(body.get('name') or "").strip()
        try:
            record = ab_eval.create_eval_set(
                name, copy_from=body.get('copy_from') or None, actor=_actor())
        except FileExistsError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        activity_log.record(actor=_actor(), category="admin",
                            action="ab_eval_set.create", details={"name": name})
        return jsonify({"ok": True, **record})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-sets/<name>/rename', methods=['POST'])
@permission_required('tab.admin.ab_eval')
@login_required
def rename_ab_eval_set(name):
    """Rename an evaluation set. Body: ``{new_name}``."""
    try:
        from fyp import ab_eval

        body = request.get_json(silent=True) or {}
        new_name = str(body.get('new_name') or "").strip()
        try:
            record = ab_eval.rename_eval_set(name, new_name)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        except FileExistsError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        activity_log.record(actor=_actor(), category="admin", action="ab_eval_set.rename",
                            details={"name": name, "new_name": new_name})
        return jsonify({"ok": True, **record})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-sets/<name>/activate', methods=['POST'])
@permission_required('tab.admin.ab_eval')
@login_required
def activate_ab_eval_set(name):
    """Make ``name`` the active evaluation set (the one a run uses)."""
    try:
        from fyp import ab_eval

        try:
            ab_eval.set_active_eval_set(name)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        stored = ab_eval.load_eval_set()
        return jsonify({
            **stored,
            "resolved": ab_eval.resolve_items(stored.get("item_ids", [])),
            "max_items": ab_eval.MAX_EVAL_ITEMS,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-sets/<name>', methods=['DELETE'])
@permission_required('tab.admin.ab_eval')
@login_required
def delete_ab_eval_set(name):
    """Delete an evaluation set (never the last remaining one)."""
    try:
        from fyp import ab_eval

        try:
            result = ab_eval.delete_eval_set(name)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        activity_log.record(actor=_actor(), category="admin",
                            action="ab_eval_set.delete", details={"name": name})
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-set', methods=['GET'])
@permission_required('tab.admin.ab_eval')
@login_required
def get_ab_eval_set():
    """Return one eval set (``?name=`` or the active one) with per-item flags."""
    try:
        from fyp import ab_eval

        stored = ab_eval.load_eval_set(request.args.get('name') or None)
        return jsonify({
            **stored,
            "resolved": ab_eval.resolve_items(stored.get("item_ids", [])),
            "max_items": ab_eval.MAX_EVAL_ITEMS,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-set', methods=['POST'])
@permission_required('tab.admin.ab_eval')
@login_required
def save_ab_eval_set():
    """Persist one eval set's items. Body: ``{item_ids, name?, note?}``. Capped."""
    try:
        from fyp import ab_eval

        body = request.get_json(silent=True) or {}
        item_ids = body.get('item_ids')
        if not isinstance(item_ids, list):
            return jsonify({"error": "body must include an 'item_ids' list"}), 400
        try:
            stored = ab_eval.save_eval_set(item_ids, actor=_actor(),
                                           note=str(body.get('note') or ""),
                                           name=body.get('name') or None)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        resolved = ab_eval.resolve_items(stored["item_ids"])
        not_downloaded = [r["item_id"] for r in resolved if r["downloaded"] is False]
        activity_log.record(actor=_actor(), category="admin", action="ab_eval_set.save",
                            details={"name": stored["name"],
                                     "n_items": len(stored["item_ids"])})
        return jsonify({**stored, "resolved": resolved, "not_downloaded": not_downloaded})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-set/sample', methods=['POST'])
@permission_required('tab.admin.ab_eval')
@login_required
def sample_ab_eval_set():
    """Sample N downloaded item ids (stratified by platform) WITHOUT persisting.

    Body: ``{n, platforms?, seed?}``. The UI merges/edits the returned ids and
    then saves the set explicitly.
    """
    try:
        from fyp import ab_eval

        body = request.get_json(silent=True) or {}
        try:
            n = int(body.get('n') or 10)
        except (TypeError, ValueError):
            return jsonify({"error": "'n' must be an integer"}), 400
        platforms = body.get('platforms') if isinstance(body.get('platforms'), list) else None
        seed = body.get('seed')
        item_ids = ab_eval.sample_items(n, platforms=platforms,
                                        seed=int(seed) if seed is not None else None)
        return jsonify({"item_ids": item_ids,
                        "resolved": ab_eval.resolve_items(item_ids)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval/estimate', methods=['POST'])
@permission_required('tab.admin.ab_eval')
@login_required
def estimate_ab_eval():
    """Estimate a run's annotation call count for the confirm dialog.

    Body: ``{n_arms}`` (preferred) or the legacy
    ``{candidate_names, include_live}``.
    """
    try:
        from fyp import ab_eval

        body = request.get_json(silent=True) or {}
        if body.get('n_arms') is not None:
            n_arms = max(0, int(body['n_arms']))
        else:
            names = body.get('candidate_names') or []
            n_arms = len(names) + (1 if body.get('include_live') else 0)
        stored = ab_eval.load_eval_set()
        n_items = len(stored.get("item_ids", []))
        return jsonify({
            "n_items": n_items,
            "n_arms": n_arms,
            "n_calls": n_items * n_arms,
            "eval_set": stored.get("name"),
            "max_items": ab_eval.MAX_EVAL_ITEMS,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




def _clean_arm_params(raw) -> tuple[dict, str | None]:
    """Validate the optional per-arm parameter overrides from the run form.

    Args:
        raw: ``{arm_name: {backend?, model?, temperature?}}`` or falsy.

    Returns:
        ``(cleaned, error)`` — cleaned dict (possibly empty) and a user-facing
        error string when validation fails.
    """
    if not raw:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "arm_params must be an object"
    from fyp.annotation.backends import variants

    known_backends = variants.selection_ids()
    cleaned: dict = {}
    for arm_name, params in raw.items():
        if not isinstance(params, dict):
            return {}, f"arm_params['{arm_name}'] must be an object"
        entry: dict = {}
        backend = params.get("backend")
        if backend not in (None, ""):
            if backend not in known_backends:
                return {}, f"arm '{arm_name}': unknown backend '{backend}'"
            entry["backend"] = backend
        model = params.get("model")
        if model not in (None, ""):
            if not isinstance(model, str) or not model.strip():
                return {}, f"arm '{arm_name}': model must be a non-empty string"
            entry["model"] = model.strip()
        temperature = params.get("temperature")
        if temperature not in (None, ""):
            if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
                return {}, f"arm '{arm_name}': temperature must be a number"
            if not 0.0 <= float(temperature) <= 2.0:
                return {}, f"arm '{arm_name}': temperature must be between 0.0 and 2.0"
            entry["temperature"] = float(temperature)
        if entry:
            cleaned[str(arm_name)] = entry
    return cleaned, None




def _clean_arms_spec(raw) -> tuple[list | None, str | None]:
    """Validate the explicit test-arm list from the run form.

    Args:
        raw: ``[{source: 'live'|'candidate', name?, label, backend?}, ...]``.
            The same contract may appear multiple times under distinct labels
            (e.g. once per backend).

    Returns:
        ``(cleaned, error)`` — the cleaned list or a user-facing error string.
    """
    from fyp import ab_eval
    from fyp.annotation.backends import variants

    known_backends = variants.selection_ids()
    if not isinstance(raw, list) or not raw:
        return None, "add at least one contract to the test"
    if len(raw) > 12:
        return None, "a test is capped at 12 contracts"
    cleaned: list = []
    labels: set = set()
    for entry in raw:
        if not isinstance(entry, dict):
            return None, "each test arm must be an object"
        source = entry.get("source")
        if source not in ("live", "candidate"):
            return None, f"unknown arm source {source!r}"
        label = str(entry.get("label") or "").strip()
        if not label or len(label) > 60:
            return None, f"invalid arm label {label!r}"
        if label in labels:
            return None, f"duplicate arm label '{label}'"
        labels.add(label)
        arm = {"source": source, "label": label}
        if source == "candidate":
            name = str(entry.get("name") or "")
            if not ab_eval.validate_candidate_name(name):
                return None, f"invalid candidate name '{name}'"
            arm["name"] = name
        backend = entry.get("backend")
        if backend not in (None, ""):
            if backend not in known_backends:
                return None, f"arm '{label}': unknown backend '{backend}'"
            arm["backend"] = backend
        cleaned.append(arm)
    return cleaned, None




@management_bp.route('/api/manage/ab-eval/run', methods=['POST'])
@permission_required('tab.admin.ab_eval')
@login_required
def start_ab_eval_run():
    """Start a test run as the ``ab_eval`` background task.

    Body: ``{arms_spec: [{source, name?, label, backend?}], eval_set?, name?}``
    (preferred — the same contract may appear as several arms under distinct
    labels, e.g. once per backend) or the legacy
    ``{candidate_names, include_live, arm_params}``. Mints the run id here so
    the UI can follow the run immediately; the worker snapshots each arm's
    contract text at start.
    """
    try:
        from fyp import ab_eval
        from fyp.fyp_config import AB_EVAL_SCRIPT

        # Explicit gate on top of start_process's own check: one A/B run at a
        # time (a second concurrent run would double the annotation spend and
        # race on the runs index).
        if _is_worker_running("ab_eval"):
            return jsonify({"status": "error",
                            "message": "A test run is already in progress."}), 409

        body = request.get_json(silent=True) or {}
        arms_spec = body.get('arms_spec')
        names: list = []
        include_live = False
        arm_params: dict = {}
        if arms_spec is not None:
            arms_spec, spec_error = _clean_arms_spec(arms_spec)
            if spec_error:
                return jsonify({"error": spec_error}), 400
        else:
            names = body.get('candidate_names') or []
            if isinstance(names, str):
                names = [n.strip() for n in names.split(",") if n.strip()]
            include_live = bool(body.get('include_live'))
            if not names and not include_live:
                return jsonify({"error": "add at least one contract to the test"}), 400
            for name in names:
                if not ab_eval.validate_candidate_name(name):
                    return jsonify({"error": f"invalid candidate name '{name}'"}), 400
            arm_params, param_error = _clean_arm_params(body.get('arm_params'))
            if param_error:
                return jsonify({"error": param_error}), 400
        stored = ab_eval.load_eval_set(body.get('eval_set') or None)
        item_ids = stored.get("item_ids", [])
        if not item_ids:
            return jsonify({"error": "the test set is empty — curate it first"}), 400

        run_id = ab_eval.new_run_id()
        run_name = str(body.get('name') or "").strip()[:60]
        task_args = {
            "run_id": run_id,
            "name": run_name,
            "candidate_names": names,
            "include_live": include_live,
            "arm_params": arm_params,
            "eval_set": stored.get("name"),
            "started_by": _actor(),
        }
        if arms_spec is not None:
            task_args["arms_spec"] = arms_spec
        success, msg = start_process("ab_eval", AB_EVAL_SCRIPT, task_args=task_args,
                                     started_by=_actor())
        if not success:
            return jsonify({"status": "error", "message": msg}), 409
        activity_log.record(actor=_actor(), category="admin", action="ab_eval.run",
                            details={"run_id": run_id, "candidates": names,
                                     "include_live": include_live,
                                     "arms_spec": arms_spec,
                                     "eval_set": stored.get("name"),
                                     "n_items": len(item_ids)})
        return jsonify({"status": "started", "run_id": run_id, "message": msg,
                        "eval_set": stored.get("name")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval/runs', methods=['GET'])
@permission_required('tab.admin.ab_eval')
@login_required
def list_ab_eval_runs():
    """Return the runs index (newest first)."""
    try:
        from fyp import ab_eval

        return jsonify({"runs": ab_eval.load_runs_index()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval/runs/<run_id>', methods=['GET'])
@permission_required('tab.admin.ab_eval')
@login_required
def get_ab_eval_run(run_id):
    """Return one run's manifest + comparison report + human-input block."""
    try:
        from fyp import ab_eval, human_eval

        run = ab_eval.load_run(run_id)
        if not run.get("manifest"):
            return jsonify({"error": f"run '{run_id}' not found"}), 404
        try:
            run["human"] = human_eval.load_human(run_id)
        except Exception:
            run["human"] = None
        return jsonify(run)
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval/runs/<run_id>/rows', methods=['GET'])
@permission_required('tab.admin.ab_eval')
@login_required
def get_ab_eval_run_rows(run_id):
    """Return one arm's refined rows (JSON-safe) for the side-by-side view.

    ``arm`` may also be ``human:<username>`` — a submitted coder of the run's
    coding task, served as rows so human input renders like any other arm.
    """
    try:
        from fyp import ab_eval, human_eval

        arm = str(request.args.get('arm') or "").strip()
        if not arm:
            return jsonify({"error": "pass ?arm=<arm name>"}), 400
        if arm.startswith("human:"):
            username = arm[len("human:"):]
            task = human_eval.load_task(run_id, "coding")
            if task is None or username not in task.get("coders", {}):
                return jsonify({"error": f"no coder '{username}' on run '{run_id}'"}), 404
            rows = human_eval.coder_rows(run_id, "coding", username)
        else:
            try:
                rows = ab_eval.load_run_rows(run_id, arm)
            except Exception:
                return jsonify({"error": f"no rows for run '{run_id}' arm '{arm}'"}), 404
        return jsonify({"run_id": run_id, "arm": arm, "rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval/runs/<run_id>', methods=['DELETE'])
@permission_required('tab.admin.ab_eval')
@login_required
def delete_ab_eval_run(run_id):
    """Delete a run's artifacts."""
    try:
        from fyp import ab_eval

        removed = ab_eval.delete_run(run_id)
        if removed:
            activity_log.record(actor=_actor(), category="admin",
                                action="ab_eval.run_delete", details={"run_id": run_id})
        return jsonify({"ok": removed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




