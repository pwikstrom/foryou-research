"""Routes for human input on annotation A/B test runs.

Two route groups share the ``human_eval_bp`` blueprint:

* ``/api/manage/human-eval/...`` — admin task setup (create/delete tasks,
  invite coders, recompute results, export codings; for a rating task also
  the draft's traps and Start), gated on ``tab.admin.human_eval``.
* ``/api/human-eval/...`` — the coder-facing endpoints. These are only
  ``login_required``; access is *invitation-record-driven* (``is_invited``:
  the username is in the task's coder list, or the user is an admin), so
  later phases can invite non-admin users without any permission changes.

BLINDNESS RULE: the coder endpoints never include machine annotation values —
a coding task is coded fully blind. Machine values live only in the admin run
report and the admin codings export. A rating task necessarily shows its rater
one answer per variable, but never where it came from (model, human, trap).
"""

import re

from flask import Blueprint, Response, jsonify, request
from flask_login import current_user, login_required

import fyp.annotation.ab_eval as ab_eval
import fyp.annotation.human_eval as human_eval
import fyp.annotation.human_rating as human_rating

from ..auth.accounts import user_manager
from ..auth.permissions import permission_required
from ..integrations import mail_utils
from ..services import activity_log
from ..services.worker_status import current_actor

human_eval_bp = Blueprint("human_eval", __name__)


def _notify_coders(task: dict, usernames: list[str], inviter: str) -> None:
    """Send one invitation email per not-yet-notified coder (fire-and-forget).

    A non-email username (rare — usernames are emails) is skipped with a
    warning and keeps ``notified: False`` so the admin card flags it. Success
    flips the flag via a background-thread callback.
    """
    run_id, task_type = task["run_id"], task["task_type"]
    n_items = len(task.get("item_ids", []))
    n_variables = len(task.get("variables", []))
    for username in usernames:
        username = str(username)
        if (task.get("coders", {}).get(username) or {}).get("notified"):
            continue
        if not mail_utils.is_email(username):
            print(f"[human_eval] not an email address, skipping invite: {username}")
            continue
        mail_utils.send_invitation_email_async(
            username,
            run_id,
            task_type,
            inviter,
            n_items,
            n_variables,
            on_success=lambda u=username: human_eval.set_notified(run_id, task_type, u),
        )


def _is_admin() -> bool:
    """True when the acting user is an admin."""
    try:
        return bool(current_user.is_authenticated and current_user.is_admin())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Admin: task setup & monitoring.
# ---------------------------------------------------------------------------


@human_eval_bp.route("/api/manage/human-eval/runs", methods=["GET"])
@permission_required("tab.admin.human_eval")
def list_human_eval_runs():
    """Finished A/B runs, each flagged with the human tasks it already has."""
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


@human_eval_bp.route("/api/manage/human-eval/runs/<run_id>/variables", methods=["GET"])
@permission_required("tab.admin.human_eval")
def get_human_eval_variables(run_id):
    """The variables of a finished run a human task can cover."""
    return jsonify({"variables": human_eval.available_variables(run_id)})


@human_eval_bp.route("/api/manage/human-eval/users", methods=["GET"])
@permission_required("tab.admin.human_eval")
def list_human_eval_users():
    """Thin roster for the coder picker: approved users' names and roles."""
    users = [
        {"username": u.username, "role": u.role, "is_admin": u.is_admin()}
        for u in user_manager.get_all_users().values()
        if getattr(u, "approved", False)
    ]
    users.sort(key=lambda u: (not u["is_admin"], u["username"]))
    return jsonify({"users": users})


@human_eval_bp.route("/api/manage/human-eval/tasks", methods=["GET"])
@permission_required("tab.admin.human_eval")
def list_human_eval_tasks():
    """The global human-task index."""
    return jsonify({"tasks": human_eval.list_tasks()})


@human_eval_bp.route("/api/manage/human-eval/tasks", methods=["POST"])
@permission_required("tab.admin.human_eval")
def create_human_eval_task():
    """Create a human task on a finished run.

    Body: ``{run_id, task_type, variables: [...], coders: [...]}``, plus
    ``n_videos`` (coding: a random sample) and, for a rating task, ``arm``,
    ``coders_included``, ``n_practice``, ``ratings_per_human``,
    ``swapped_per_rater`` and ``exclusion_rule``. A rating task is created
    as a draft: its raters are invited only when it is started.
    """
    payload = request.get_json(silent=True) or {}
    run_id = str(payload.get("run_id") or "")
    task_type = str(payload.get("task_type") or "coding")
    n_videos = payload.get("n_videos") or None
    try:
        if task_type == "rating":
            task = human_rating.create_task(
                run_id=run_id,
                variables=list(payload.get("variables") or []),
                raters=[str(u) for u in (payload.get("coders") or [])],
                created_by=current_actor(),
                arm=payload.get("arm") or None,
                coders_included=[str(u) for u in (payload.get("coders_included") or [])],
                n_videos=int(n_videos) if n_videos else None,
                n_practice=int(payload.get("n_practice", 3) or 0),
                ratings_per_human=int(payload.get("ratings_per_human", 2) or 0),
                swapped_per_rater=int(payload.get("swapped_per_rater", 3) or 0),
                exclusion_rule=str(payload.get("exclusion_rule") or "none"),
            )
        else:
            task = human_eval.create_task(
                run_id=run_id,
                task_type=task_type,
                variables=list(payload.get("variables") or []),
                coders=[str(u) for u in (payload.get("coders") or [])],
                created_by=current_actor(),
                arms=list(payload.get("arms") or []) or None,
                n_videos=int(n_videos) if n_videos else None,
            )
        activity_log.record(
            actor=current_actor(),
            category="admin",
            action="human_eval.create_task",
            target=run_id,
            details={
                "task_type": task_type,
                "n_variables": len(task["variables"]),
                "coders": sorted(task["coders"]),
            },
        )
        if task.get("status") != "draft":
            _notify_coders(task, list(task["coders"]), current_actor())
        return jsonify({"task": task})
    except (TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


@human_eval_bp.route("/api/manage/human-eval/tasks/<run_id>/rating/traps", methods=["POST"])
@permission_required("tab.admin.human_eval")
def add_rating_trap(run_id):
    """Write a trap into a rating draft. Body: ``{item_id, variable, value, note}``."""
    payload = request.get_json(silent=True) or {}
    try:
        trap = human_rating.add_trap(
            run_id,
            item_id=str(payload.get("item_id") or ""),
            variable=str(payload.get("variable") or ""),
            value=payload.get("value"),
            note=str(payload.get("note") or ""),
            created_by=current_actor(),
        )
        return jsonify({"trap": trap})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@human_eval_bp.route(
    "/api/manage/human-eval/tasks/<run_id>/rating/traps/<trap_id>", methods=["DELETE"]
)
@permission_required("tab.admin.human_eval")
def delete_rating_trap(run_id, trap_id):
    """Remove a trap from a rating draft."""
    try:
        return jsonify({"removed": human_rating.delete_trap(run_id, trap_id)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@human_eval_bp.route("/api/manage/human-eval/tasks/<run_id>/rating/model-answers", methods=["GET"])
@permission_required("tab.admin.human_eval")
def rating_model_answers(run_id):
    """The contract's answers for one video (``?item_id=``), to copy into a trap."""
    try:
        answers = human_rating.model_answers_for(run_id, str(request.args.get("item_id") or ""))
        return jsonify({"answers": answers})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@human_eval_bp.route("/api/manage/human-eval/tasks/<run_id>/rating/start", methods=["POST"])
@permission_required("tab.admin.human_eval")
def start_rating_task(run_id):
    """Freeze a rating draft's answers, assign them, and invite the raters."""
    try:
        summary = human_rating.start(run_id, started_by=current_actor())
        task = human_eval.load_task(run_id, "rating")
        activity_log.record(
            actor=current_actor(),
            category="admin",
            action="human_eval.start_rating",
            target=run_id,
            details={"raters": sorted(task["coders"]), "by_source": summary["by_source"]},
        )
        _notify_coders(task, list(task["coders"]), current_actor())
        return jsonify({"summary": summary})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@human_eval_bp.route("/api/manage/human-eval/tasks/<run_id>/<task_type>", methods=["GET"])
@permission_required("tab.admin.human_eval")
def get_human_eval_task(run_id, task_type):
    """One task's definition plus derived per-coder progress."""
    try:
        task = human_eval.load_task(run_id, task_type)
        if task is None:
            return jsonify({"error": "task not found"}), 404
        return jsonify(
            {
                "task": task,
                "coder_status": human_eval.coder_status(run_id, task_type, task=task),
                "results": human_eval.load_results(run_id, task_type),
            }
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@human_eval_bp.route("/api/manage/human-eval/tasks/<run_id>/<task_type>", methods=["DELETE"])
@permission_required("tab.admin.human_eval")
def delete_human_eval_task(run_id, task_type):
    """Delete a task with its coder files and results."""
    try:
        removed = human_eval.delete_task(run_id, task_type)
        activity_log.record(
            actor=current_actor(),
            category="admin",
            action="human_eval.delete_task",
            target=run_id,
            details={"task_type": task_type, "removed": removed},
        )
        return jsonify({"removed": removed})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@human_eval_bp.route("/api/manage/human-eval/tasks/<run_id>/<task_type>/coders", methods=["POST"])
@permission_required("tab.admin.human_eval")
def add_human_eval_coders(run_id, task_type):
    """Invite additional coders. Body: ``{coders: [...]}``."""
    payload = request.get_json(silent=True) or {}
    coders = [str(u) for u in (payload.get("coders") or [])]
    if not coders:
        return jsonify({"error": "no coders given"}), 400
    try:
        before = set((human_eval.load_task(run_id, task_type) or {}).get("coders", {}))
        task = human_eval.add_coders(run_id, task_type, coders, invited_by=current_actor())
        activity_log.record(
            actor=current_actor(),
            category="admin",
            action="human_eval.add_coders",
            target=run_id,
            details={"task_type": task_type, "coders": coders},
        )
        newly_added = [u for u in coders if u not in before]
        if task.get("status") != "draft":  # a draft's raters are invited at Start
            _notify_coders(task, newly_added, current_actor())
        return jsonify({"task": task})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@human_eval_bp.route("/api/manage/human-eval/tasks/<run_id>/<task_type>/notify", methods=["POST"])
@permission_required("tab.admin.human_eval")
def resend_human_eval_invite(run_id, task_type):
    """(Re)send one coder's invitation email synchronously. Body: ``{username}``.

    Synchronous on purpose — the admin clicked Resend and wants immediate
    feedback (including the local-dev "MAIL_PASSWORD not set" case).
    """
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "")
    try:
        task = human_eval.load_task(run_id, task_type)
        if task is None:
            return jsonify({"error": "task not found"}), 404
        if task.get("status") == "draft":
            return jsonify({"error": "start the rating task before inviting"}), 400
        if username not in task.get("coders", {}):
            return jsonify({"error": f"{username} is not an invited coder"}), 400
        if not mail_utils.is_email(username):
            return jsonify({"error": f"{username} is not an email address"}), 400
        sent = mail_utils.send_invitation_email(
            username,
            run_id,
            task_type,
            current_actor(),
            len(task.get("item_ids", [])),
            len(task.get("variables", [])),
        )
        if sent:
            human_eval.set_notified(run_id, task_type, username)
        else:
            return jsonify({"error": "email not sent — is MAIL_PASSWORD configured?"}), 400
        activity_log.record(
            actor=current_actor(),
            category="admin",
            action="human_eval.resend_invite",
            target=run_id,
            details={"task_type": task_type, "username": username},
        )
        return jsonify({"sent": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@human_eval_bp.route(
    "/api/manage/human-eval/tasks/<run_id>/<task_type>/recompute", methods=["POST"]
)
@permission_required("tab.admin.human_eval")
def recompute_human_eval_results(run_id, task_type):
    """Recompute a task's ICR metrics from the submitted codings."""
    try:
        results = human_eval.compute_results(run_id, task_type)
        return jsonify({"results": results})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@human_eval_bp.route("/api/manage/human-eval/tasks/<run_id>/<task_type>/export", methods=["GET"])
@permission_required("tab.admin.human_eval")
def export_human_eval_codings(run_id, task_type):
    """Download a task's data as CSV: codings next to each contract's answer,
    or for a rating task every rating with the answer judged and its source."""
    try:
        if task_type == "rating":
            text = human_rating.export_csv(run_id)
        else:
            text = human_eval.export_codings_csv(run_id, task_type)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    prefix = "ratings" if task_type == "rating" else "codings"
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{prefix}_{run_id}.csv")
    return Response(
        text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
    if not human_eval.is_invited(task, current_actor(), is_admin=_is_admin()):
        return None, (jsonify({"error": "not invited to this task"}), 403)
    return task, None


@human_eval_bp.route("/api/human-eval/my-tasks", methods=["GET"])
@login_required
def my_human_eval_tasks():
    """The acting user's tasks with their own progress counts."""
    username = current_actor()
    entries = human_eval.tasks_for_user(username, is_admin=_is_admin())
    tasks = []
    for entry in entries:
        state = human_eval.load_coder_state(entry["run_id"], entry["task_type"], username)
        tasks.append(
            {
                **entry,
                "my_status": (
                    state.get("status")
                    if (state.get("responses") or state.get("status") == "submitted")
                    else "invited"
                ),
                "my_n_answered": len(state.get("responses") or {}),
            }
        )
    return jsonify({"tasks": tasks})


@human_eval_bp.route("/api/human-eval/tasks/<run_id>/<task_type>", methods=["GET"])
@login_required
def get_coder_task(run_id, task_type):
    """A coder's working payload: items, field specs, and their own responses.

    Deliberately excludes everything else on the task (other coders'
    identities/responses stay private). For a coding task machine values are
    not even stored on the task; for a vote task the machine values arrive as
    anonymous options in a per-(item, coder) randomized order — never with an
    arm name, etag or source — and saved choices come back as option letters.
    """
    task, error = _load_invited_task(run_id, task_type)
    if error:
        return error
    username = current_actor()
    state = human_eval.load_coder_state(run_id, task_type, username)
    payload = {
        "run_id": task["run_id"],
        "task_type": task["task_type"],
        "items": task["items"],
        "variables": task["variables"],
        "field_specs": task["field_specs"],
        "status": state.get("status", "in_progress"),
    }
    if task["task_type"] == "rating":
        payload.update(human_rating.rater_payload(task, username, state))
        payload["responses"] = {
            item_id: {"values": response.get("values", {}), "note": response.get("note", "")}
            for item_id, response in (state.get("responses") or {}).items()
        }
    elif task["task_type"] == "vote":
        payload["n_options"] = len(task.get("arms", []))
        payload["options"] = human_eval.vote_options_payload(task, username)
        payload["responses"] = {
            item_id: {
                "choice": human_eval.letter_for_arm(
                    task, item_id, username, (response.get("values") or {}).get("choice", "")
                ),
                "note": response.get("note", ""),
            }
            for item_id, response in (state.get("responses") or {}).items()
        }
    else:
        payload["responses"] = {
            item_id: {
                "values": response.get("values", {}),
                "note": response.get("note", ""),
            }
            for item_id, response in (state.get("responses") or {}).items()
        }
    return jsonify(payload)


@human_eval_bp.route("/api/human-eval/tasks/<run_id>/<task_type>/responses", methods=["POST"])
@login_required
def save_coder_response(run_id, task_type):
    """Autosave one item's values and/or note.

    Body: ``{item_id, values: {var: value}, note: "..."}`` — omit ``values``
    for a note-only save (keeps the item's recorded values), omit ``note`` to
    keep the existing note.
    """
    task, error = _load_invited_task(run_id, task_type)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    username = current_actor()
    try:
        # An admin coding without an explicit invitation self-registers as a
        # coder so progress/results derivation can find their response file.
        if username not in task.get("coders", {}):
            human_eval.add_coders(run_id, task_type, [username], invited_by=username)
        state = human_eval.save_response(
            run_id,
            task_type,
            username,
            item_id=str(payload.get("item_id") or ""),
            values=payload.get("values") if "values" in payload else None,
            note=payload.get("note") if "note" in payload else None,
        )
        return jsonify(
            {
                "saved": True,
                "n_answered": human_eval._n_answered(state),
            }
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@human_eval_bp.route("/api/human-eval/tasks/<run_id>/rating/guesses", methods=["POST"])
@login_required
def save_rating_guesses(run_id):
    """Save the closing "who wrote this?" guesses. Body: ``{guesses: {key: guess}}``."""
    task, error = _load_invited_task(run_id, "rating")
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        state = human_rating.save_guesses(run_id, current_actor(), payload.get("guesses") or {})
        return jsonify({"saved": True, "n_guesses": len(state.get("guesses") or {})})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@human_eval_bp.route("/api/human-eval/tasks/<run_id>/<task_type>/submit", methods=["POST"])
@login_required
def submit_coder_task(run_id, task_type):
    """Finalize the acting coder's work and trigger the results computation."""
    task, error = _load_invited_task(run_id, task_type)
    if error:
        return error
    try:
        outcome = human_eval.submit(run_id, task_type, current_actor())
        activity_log.record(
            actor=current_actor(),
            category="human_eval",
            action="human_eval.submit",
            target=run_id,
            details={"task_type": task_type, **outcome},
        )
        return jsonify(outcome)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
