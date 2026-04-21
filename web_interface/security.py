import os

from flask import jsonify, redirect, request, url_for
from flask_login import LoginManager

import web_interface.auth as auth

# --- Auth Setup ---
login_manager = LoginManager()
login_manager.login_view = 'auth_bp.login' # Updated to point to blueprint view
login_manager.anonymous_user = auth.AnonymousUser

# Skip the user-bulk-load at startup on services that don't authenticate
# browser traffic. The task-runner receives Cloud Tasks HTTP requests on
# /internal/run-task/<name> and never consults Flask-Login, so loading every
# user JSON at cold start is pure overhead that scales linearly with N.
# Web service (fyp-data-hub) keeps the eager load so sign-in latency stays
# low once the instance is warm.
_K_SERVICE = os.environ.get("K_SERVICE", "")
_IS_TASK_RUNNER = _K_SERVICE == "fyp-task-runner"

# Always log the decision so "is lazy mode actually engaged on this
# instance?" is provable from a single grep, independent of whether
# downstream prints get buffered.
print(
    f"[AUTH] boot K_SERVICE={_K_SERVICE!r} "
    f"is_task_runner={_IS_TASK_RUNNER} "
    f"bulk_load={not _IS_TASK_RUNNER}",
    flush=True,
)

# Initialize User Manager
user_manager = auth.UserManager(
    storage_location="users",
    bulk_load=not _IS_TASK_RUNNER,
)

@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith('/api/'):
        return jsonify({"error": "unauthorized"}), 401
    return redirect(url_for('auth_bp.login'))


@login_manager.user_loader
def load_user(user_id):
    return user_manager.get_user(user_id)
