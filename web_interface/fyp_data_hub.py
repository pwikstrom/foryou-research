import logging
import os
import sys
from datetime import datetime

# --- Script Execution Support ---
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template, request
from flask_login import current_user

if __name__ == "__main__" and __package__ is None:
    file_path = Path(__file__).resolve()
    project_root = file_path.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    __package__ = "web_interface"

# Imports
from flask_wtf.csrf import CSRFProtect

from .process_manager import load_process_stats  # Import load function
from .security import login_manager  # Import shared auth objects

csrf = CSRFProtect()

# Initialize stats
load_process_stats()

# Silence the noisy HTTP request logs from Flask/Werkzeug
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# The task-runner service serves only the Cloud Tasks internal blueprint. Gating
# the web-UI blueprint IMPORTS (not just their registration) on this flag keeps
# the heavy web-only route modules — management_routes alone is ~3.6k lines — out
# of every task-runner cold start.
_IS_TASK_RUNNER = os.environ.get("K_SERVICE") == "fyp-task-runner"


# --- Custom JSON Provider for Numpy/Pandas ---
from flask.json.provider import DefaultJSONProvider


class CustomJSONProvider(DefaultJSONProvider):
    # Preserve dict insertion order in responses (Flask's default alphabetizes).
    # The annotation contract's enum tables encode their canonical value order
    # as key order — sorting them in /parsed would silently reorder the prompt.
    sort_keys = False

    def default(self, obj):
        if isinstance(obj, (np.integer, int)):
            return int(obj)
        if isinstance(obj, (np.floating, float)):
            # Check for NaN/Inf
            if np.isnan(obj) or np.isinf(obj):
                return None
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.bool_, bool)):
            return bool(obj)
        if pd.isna(obj): # Handles pd.NA, np.nan, pd.NaT
            return None
        if isinstance(obj, (pd.Timestamp, datetime)):
            return obj.isoformat()

        return super().default(obj)


def _register_web_ui(app):
    """Import and register the full web UI onto ``app``.

    The blueprint imports live inside this function (not at module scope) so the
    task-runner — which serves only the Cloud Tasks internal route — never pays
    the import cost of the web-only route modules.

    Args:
        app: The Flask application to register the web blueprints and routes on.
    """
    from .routes.api_correlations_routes import correlations_bp
    from .routes.api_explorer_routes import explorer_bp
    from .routes.api_semantic_space_routes import semantic_space_bp
    from .routes.api_timelines_routes import timelines_bp
    from .routes.api_viewer_routes import viewer_bp
    from .routes.auth_routes import auth_bp
    from .routes.human_eval_routes import human_eval_bp
    from .routes.management_routes import management_bp
    from .routes.process_routes import process_bp
    from .routes.public_routes import public_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(process_bp)
    app.register_blueprint(explorer_bp)
    app.register_blueprint(viewer_bp)
    app.register_blueprint(timelines_bp)
    app.register_blueprint(correlations_bp)
    app.register_blueprint(semantic_space_bp)
    app.register_blueprint(management_bp)
    app.register_blueprint(human_eval_bp)

    @app.context_processor
    def inject_scrape_platforms():
        """Expose the registered scrape platforms to every template.

        The enrichment sub-page renders one scraper block per platform; a context
        processor reaches nested includes without threading the value through
        every render_template call.
        """
        import fyp.scrape_queues as scrape_queues
        try:
            return {"scrape_platforms": scrape_queues.registered_platforms()}
        except Exception:
            return {"scrape_platforms": ["tiktok"]}

    @app.context_processor
    def inject_contact_email():
        """Expose the instance operator's contact email to every template.

        Comes from [site].contact_email (config.local.toml, or the
        FYP_CONTACT_EMAIL env var which overrides it at config load);
        templates render their contact/feedback passages only when it is
        set, so a third-party install shows no foreign address.
        """
        from fyp.fyp_config import get_config
        try:
            site = get_config().get("site", {}) or {}
            return {"contact_email": str(site.get("contact_email", "") or "").strip()}
        except Exception:
            return {"contact_email": ""}

    @app.errorhandler(403)
    def handle_forbidden(error):
        """Return JSON for API routes so client-side ``res.json()`` doesn't choke.

        ``permission_required`` and ``role_required`` raise ``abort(403)``, which
        by default renders an HTML error page. A ``fetch().then(res => res.json())``
        on that page throws "Unexpected token '<'". Mirror the 401 handler in
        ``security.py``: send JSON for ``/api/`` paths, keep the default HTML page
        for regular page navigation.

        Args:
            error: The 403 ``HTTPException`` raised by the aborted request.

        Returns:
            A JSON 403 response for API paths, otherwise the original error.
        """
        if request.path.startswith('/api/'):
            return jsonify({"error": "forbidden"}), 403
        return error

    @app.route('/')
    def index():
        # Anonymous visitors get the public landing page; authenticated users
        # get the app shell. One rule keeps every url_for('index') call site
        # (login redirects, the unauthorized handler) working unchanged.
        if not current_user.is_authenticated:
            return render_template('public/landing.html', active_page='landing')

        from fyp.fyp_config import get_config

        from .permissions import get_user_permissions
        from .slack_service import get_recent_messages
        slack_configured = bool(os.environ.get("SLACK_BOT_TOKEN"))
        slack_messages = get_recent_messages() if slack_configured else []
        user_perms = get_user_permissions(current_user)
        # The async (batch) annotator needs media as gs:// URIs, so its card is
        # only meaningful when media is GCS-backed (same flag resolve_media uses).
        media_on_gcs = bool(get_config().get("data_io", {}).get("use_gcs_for_media"))
        return render_template('index.html', user=current_user, user_perms=user_perms, slack_messages=slack_messages, slack_configured=slack_configured, media_on_gcs=media_on_gcs)


def create_app():
    """Build the Flask application.

    On the web service the full UI is registered. On the task-runner
    (``K_SERVICE == "fyp-task-runner"``) only the Cloud Tasks internal blueprint
    is imported and registered, so the web-only route modules never load — the
    task-runner's cold start no longer pays for the web UI it never serves.

    Returns:
        The configured Flask application.
    """
    app = Flask(__name__)

    app.json = CustomJSONProvider(app)
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "local-dev-key")
    if app.secret_key == "local-dev-key" and os.environ.get("K_SERVICE"):
        # Deployed without a real session secret: sessions are forgeable.
        # Warn loudly (but keep booting) so the misconfiguration is visible
        # in the Cloud Run logs without taking the service down.
        print(
            "WARNING: FLASK_SECRET_KEY is not set on a Cloud Run service - "
            "falling back to the dev-only secret. Set it in the service "
            "configuration immediately.",
            flush=True,
        )

    # Init Auth
    login_manager.init_app(app)
    # Validate CSRF tokens against the session only, with no separate time limit.
    # The default 3600s expiry silently breaks state-changing POSTs from tabs left
    # open longer than an hour (e.g. while the enrichment queues run), surfacing as
    # opaque HTTP 400s on Consolidate & Refresh, collection delete, etc. Tokens stay
    # bound to the session secret, so CSRF protection is unchanged.
    app.config["WTF_CSRF_TIME_LIMIT"] = None
    csrf.init_app(app)

    # The Cloud Tasks internal blueprint is the only route the task-runner
    # serves. On Cloud Run it is registered ONLY on the task-runner: dispatches
    # always target CLOUD_RUN_SERVICE_URL (the task-runner), and the task-runner
    # is the one service whose platform IAM restricts invocation to the Cloud
    # Tasks service account — the public data-hub must not expose a task
    # execution endpoint. Locally (no K_SERVICE) it stays registered for parity.
    # Exempt from CSRF: authenticated by Cloud Run's IAM invoker check.
    if _IS_TASK_RUNNER or not os.environ.get("K_SERVICE"):
        from .routes.process_routes import internal_bp
        app.register_blueprint(internal_bp)
        csrf.exempt(internal_bp)

    if not _IS_TASK_RUNNER:
        _register_web_ui(app)

    return app


app = create_app()


# Boot-time system-health check: spawns a daemon thread (or skips while the
# persisted result is still fresh) — never blocks startup. Triggered only in
# processes that actually serve the web UI: the Cloud Run web service (gunicorn
# imports this module with K_SERVICE set) and the local dev server (the
# reloader child below). Plain imports (tests, scripts) must not spawn probes.
if not _IS_TASK_RUNNER and os.environ.get("K_SERVICE"):
    from .services import system_health
    system_health.maybe_start_boot_check()


def _debug_enabled(value: str | None) -> bool:
    """Interpret the ``FLASK_DEBUG`` environment value as a boolean.

    Args:
        value: The raw environment value, or ``None`` when unset.

    Returns:
        True when the value is a truthy flag ("1", "true", "yes"), else False.
    """
    return (value or "").strip().lower() in ("1", "true", "yes")






if __name__ == '__main__':
    debug = _debug_enabled(os.environ.get("FLASK_DEBUG"))
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        # Under the debug reloader only the child serves requests (the parent
        # just watches files), so the boot probe runs in the child; without
        # debug there is no reloader and this process is the server.
        from web_interface.services import system_health
        system_health.maybe_start_boot_check()
    port = int(os.environ.get("PORT", 5002))
    app.run(host='0.0.0.0', port=port, debug=debug)
