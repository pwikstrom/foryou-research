"""Flask-Login wiring: the shared ``login_manager`` and its user loader.

The user store itself is ``accounts.user_manager``."""

from flask import jsonify, redirect, request, url_for
from flask_login import LoginManager

from web_interface.auth import accounts

# --- Auth Setup ---
login_manager = LoginManager()
login_manager.login_view = "auth_bp.login"  # Updated to point to blueprint view
login_manager.anonymous_user = accounts.AnonymousUser

# The app builds (and on the web service, bootstraps) the user store at boot,
# not on its first request.
accounts.get_user_manager()


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect(url_for("auth_bp.login"))


@login_manager.user_loader
def load_user(user_id):
    return accounts.user_manager.get_user(user_id)
