import os

from flask import jsonify, redirect, request, url_for
from flask_login import LoginManager

import web_interface.auth as auth

# --- Auth Setup ---
login_manager = LoginManager()
login_manager.login_view = 'auth_bp.login' # Updated to point to blueprint view
login_manager.anonymous_user = auth.AnonymousUser

# Neither service preloads the full user roster anymore — it is loaded lazily on
# first access (get_all_users), so cold start is O(1) in the number of users on
# both. The `bootstrap` flag only decides whether this instance runs the one-time
# legacy-data migration and ensures a default admin exists: the web service owns
# the user store (bootstrap=True); the task-runner serves only Cloud Tasks
# internal routes, never authenticates browser traffic, and does not own user
# data, so it skips both (bootstrap=False).
_K_SERVICE = os.environ.get("K_SERVICE", "")
_IS_TASK_RUNNER = _K_SERVICE == "fyp-task-runner"

# Always log the decision so "does this instance bootstrap the user store?" is
# provable from a single grep, independent of whether downstream prints buffer.
print(
    f"[AUTH] boot K_SERVICE={_K_SERVICE!r} "
    f"is_task_runner={_IS_TASK_RUNNER} "
    f"bootstrap={not _IS_TASK_RUNNER}",
    flush=True,
)

# Initialize User Manager
user_manager = auth.UserManager(
    storage_location="users",
    bootstrap=not _IS_TASK_RUNNER,
)

@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith('/api/'):
        return jsonify({"error": "unauthorized"}), 401
    return redirect(url_for('auth_bp.login'))


@login_manager.user_loader
def load_user(user_id):
    return user_manager.get_user(user_id)
