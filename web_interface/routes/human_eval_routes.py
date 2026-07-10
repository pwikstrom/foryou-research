"""Routes for human input on annotation A/B test runs.

Two route groups share the ``human_eval_bp`` blueprint:

* ``/api/manage/human-eval/...`` — admin task setup (create/delete tasks,
  invite coders, recompute results), gated on ``tab.admin.human_eval``.
* ``/api/human-eval/...`` — the coder-facing endpoints. These are only
  ``login_required``; access is *invitation-record-driven* (``is_invited``:
  the username is in the task's coder list, or the user is an admin), so
  later phases can invite non-admin users without any permission changes.

BLINDNESS RULE: the coder endpoints never include machine annotation values —
a coding task is coded fully blind. Machine values live only in the admin run
report.
"""

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

import fyp.ab_eval as ab_eval
import fyp.human_eval as human_eval

from .. import activity_log
from ..permissions import permission_required
from ..security import user_manager

human_eval_bp = Blueprint('human_eval', __name__)




def _actor() -> str:
    """Return the username of the acting user, or empty string if unauthenticated."""
    try:
        return current_user.username if current_user.is_authenticated else ""
    except Exception:
        return ""




def _is_admin() -> bool:
    """True when the acting user is an admin."""
    try:
        return bool(current_user.is_authenticated and current_user.is_admin())
    except Exception:
        return False




# ---------------------------------------------------------------------------
# Admin: task setup & monitoring.
# ---------------------------------------------------------------------------


@human_eval_bp.route('/api/manage/human-eval/runs', methods=['GET'])
@permission_required('tab.admin.human_eval')
@login_required
def list_human_eval_runs():
    """Finished A/B runs, each flagged with the human tasks it already has."""
    try:
        tasks = human_eval.list_tasks()
        by_run: dict[str, list[str]] = {}
        for task in tasks:
            by_run.setdefault(task["run_id"], []).append(task["task_type"])
        runs = [
            {**run, "human_tasks": sorted(by_run.get(run.get("run_id"), []))}
            for run in ab_eval.load_runs_index()
            if run.get("status") == "complete"
        ]
        return jsonify({"runs": runs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@human_eval_bp.route('/api/manage/human-eval/runs/<run_id>/variables', methods=['GET'])
@permission_required('tab.admin.human_eval')
@login_required
def get_human_eval_variables(run_id):
    """The variables of a finished run a human task can cover."""
    try:
        return jsonify({"variables": human_eval.available_variables(run_id)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@human_eval_bp.route('/api/manage/human-eval/users', methods=['GET'])
@permission_required('tab.admin.human_eval')
@login_required
def list_human_eval_users():
    """Thin roster for the coder picker: approved users' names and roles."""
    try:
        users = [
            {"username": u.username, "role": u.role, "is_admin": u.is_admin()}
            for u in user_manager.get_all_users().values()
            if getattr(u, "approved", False)
        ]
        users.sort(key=lambda u: (not u["is_admin"], u["username"]))
        return jsonify({"users": users})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@human_eval_bp.route('/api/manage/human-eval/tasks', methods=['GET'])
@permission_required('tab.admin.human_eval')
@login_required
def list_human_eval_tasks():
    """The global human-task index."""
    try:
        return jsonify({"tasks": human_eval.list_tasks()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@human_eval_bp.route('/api/manage/human-eval/tasks', methods=['POST'])
@permission_required('tab.admin.human_eval')
@login_required
def create_human_eval_task():
    """Create a human task on a finished run.

    Body: ``{run_id, task_type, variables: [...], coders: [...]}``.
    """
    payload = request.get_json(silent=True) or {}
    run_id = str(payload.get("run_id") or "")
    task_type = str(payload.get("task_type") or "coding")
    try:
        task = human_eval.create_task(
            run_id=run_id,
            task_type=task_type,
            variables=list(payload.get("variables") or []),
            coders=[str(u) for u in (payload.get("coders") or [])],
            created_by=_actor(),
        )
        activity_log.record(
            actor=_actor(), category="admin", action="human_eval.create_task",
            target=run_id,
            details={"task_type": task_type,
                     "n_variables": len(task["variables"]),
                     "coders": sorted(task["coders"])},
        )
        return jsonify({"task": task})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@human_eval_bp.route('/api/manage/human-eval/tasks/<run_id>/<task_type>', methods=['GET'])
@permission_required('tab.admin.human_eval')
@login_required
def get_human_eval_task(run_id, task_type):
    """One task's definition plus derived per-coder progress."""
    try:
        task = human_eval.load_task(run_id, task_type)
        if task is None:
            return jsonify({"error": "task not found"}), 404
        return jsonify({
            "task": task,
            "coder_status": human_eval.coder_status(run_id, task_type, task=task),
            "results": human_eval.load_results(run_id, task_type),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@human_eval_bp.route('/api/manage/human-eval/tasks/<run_id>/<task_type>', methods=['DELETE'])
@permission_required('tab.admin.human_eval')
@login_required
def delete_human_eval_task(run_id, task_type):
    """Delete a task with its coder files and results."""
    try:
        removed = human_eval.delete_task(run_id, task_type)
        activity_log.record(
            actor=_actor(), category="admin", action="human_eval.delete_task",
            target=run_id, details={"task_type": task_type, "removed": removed},
        )
        return jsonify({"removed": removed})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@human_eval_bp.route('/api/manage/human-eval/tasks/<run_id>/<task_type>/coders', methods=['POST'])
@permission_required('tab.admin.human_eval')
@login_required
def add_human_eval_coders(run_id, task_type):
    """Invite additional coders. Body: ``{coders: [...]}``."""
    payload = request.get_json(silent=True) or {}
    coders = [str(u) for u in (payload.get("coders") or [])]
    if not coders:
        return jsonify({"error": "no coders given"}), 400
    try:
        task = human_eval.add_coders(run_id, task_type, coders, invited_by=_actor())
        activity_log.record(
            actor=_actor(), category="admin", action="human_eval.add_coders",
            target=run_id, details={"task_type": task_type, "coders": coders},
        )
        return jsonify({"task": task})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@human_eval_bp.route('/api/manage/human-eval/tasks/<run_id>/<task_type>/recompute', methods=['POST'])
@permission_required('tab.admin.human_eval')
@login_required
def recompute_human_eval_results(run_id, task_type):
    """Recompute a task's ICR metrics from the submitted codings."""
    try:
        results = human_eval.compute_results(run_id, task_type)
        return jsonify({"results": results})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500




# ---------------------------------------------------------------------------
# Coder-facing endpoints (invitation-gated, never expose machine values).
# ---------------------------------------------------------------------------


def _load_invited_task(run_id: str, task_type: str):
    """Load a task and enforce the invitation gate.

    Returns:
        ``(task, error_response)`` — exactly one is non-None.
    """
    try:
        task = human_eval.load_task(run_id, task_type)
    except ValueError as e:
        return None, (jsonify({"error": str(e)}), 400)
    if task is None:
        return None, (jsonify({"error": "task not found"}), 404)
    if not human_eval.is_invited(task, _actor(), is_admin=_is_admin()):
        return None, (jsonify({"error": "not invited to this task"}), 403)
    return task, None




@human_eval_bp.route('/api/human-eval/my-tasks', methods=['GET'])
@login_required
def my_human_eval_tasks():
    """The acting user's tasks with their own progress counts."""
    try:
        username = _actor()
        entries = human_eval.tasks_for_user(username, is_admin=_is_admin())
        tasks = []
        for entry in entries:
            state = human_eval.load_coder_state(
                entry["run_id"], entry["task_type"], username)
            tasks.append({
                **entry,
                "my_status": (state.get("status")
                              if (state.get("responses") or state.get("status") == "submitted")
                              else "invited"),
                "my_n_answered": len(state.get("responses") or {}),
            })
        return jsonify({"tasks": tasks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@human_eval_bp.route('/api/human-eval/tasks/<run_id>/<task_type>', methods=['GET'])
@login_required
def get_coder_task(run_id, task_type):
    """A coder's working payload: items, field specs, and their own responses.

    Deliberately excludes everything else on the task (machine values are not
    even stored on it, and other coders' identities/responses stay private).
    """
    task, error = _load_invited_task(run_id, task_type)
    if error:
        return error
    state = human_eval.load_coder_state(run_id, task_type, _actor())
    return jsonify({
        "run_id": task["run_id"],
        "task_type": task["task_type"],
        "items": task["items"],
        "variables": task["variables"],
        "field_specs": task["field_specs"],
        "status": state.get("status", "in_progress"),
        "responses": {
            item_id: response.get("values", {})
            for item_id, response in (state.get("responses") or {}).items()
        },
    })




@human_eval_bp.route('/api/human-eval/tasks/<run_id>/<task_type>/responses', methods=['POST'])
@login_required
def save_coder_response(run_id, task_type):
    """Autosave one item's values. Body: ``{item_id, values: {var: value}}``."""
    task, error = _load_invited_task(run_id, task_type)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    username = _actor()
    try:
        # An admin coding without an explicit invitation self-registers as a
        # coder so progress/results derivation can find their response file.
        if username not in task.get("coders", {}):
            human_eval.add_coders(run_id, task_type, [username], invited_by=username)
        state = human_eval.save_response(
            run_id, task_type, username,
            item_id=str(payload.get("item_id") or ""),
            values=payload.get("values") or {},
        )
        return jsonify({
            "saved": True,
            "n_answered": len(state.get("responses") or {}),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@human_eval_bp.route('/api/human-eval/tasks/<run_id>/<task_type>/submit', methods=['POST'])
@login_required
def submit_coder_task(run_id, task_type):
    """Finalize the acting coder's work and trigger the results computation."""
    task, error = _load_invited_task(run_id, task_type)
    if error:
        return error
    try:
        outcome = human_eval.submit(run_id, task_type, _actor())
        activity_log.record(
            actor=_actor(), category="human_eval", action="human_eval.submit",
            target=run_id, details={"task_type": task_type, **outcome},
        )
        return jsonify(outcome)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
