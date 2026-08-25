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
    from .routes.api_sessions_routes import sessions_bp
    from .routes.api_timelines_routes import timelines_bp
    from .routes.api_viewer_routes import viewer_bp
    from .routes.auth_routes import auth_bp
    from .routes.human_eval_routes import human_eval_bp
    from .routes.management_routes import management_bp
    from .routes.my_collections_routes import my_collections_bp
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
    app.register_blueprint(sessions_bp)
    app.register_blueprint(management_bp)
    app.register_blueprint(human_eval_bp)
    app.register_blueprint(my_collections_bp)

    @app.before_request
    def canonicalise_host():
        """Send ``www.`` traffic to the canonical host with a 301.

        Two hostnames serving the same page with a 200 is what cost
        foryouresearch.net its entire index in 2026-08: Google treated the apex
        and ``www.`` as duplicates, kept ``www.`` — which still carried the old
        Wix ``noindex`` — and dropped the real site with it. The redirect makes
        the choice explicit instead of leaving it to a crawler. See
        ``web_interface.seo`` for why it is scoped to the ``www.`` twin only.
        """
        from web_interface import seo
        return seo.canonical_host_redirect()

    @app.context_processor
    def inject_seo():
        """Expose the current page's search and social metadata as ``seo``.

        A context processor rather than a per-route argument because the tags
        live in the shared public layout: a new public page then needs no
        template work at all, only an entry in ``seo.PUBLIC_PAGES``.
        """
        from web_interface import seo
        try:
            return {"seo": seo.page_meta()}
        except Exception:
            # Metadata is never worth a 500. A page with no canonical link
            # still renders; the next crawl picks it up once this is fixed.
            logging.warning("Could not build SEO metadata for this request.", exc_info=True)
            return {"seo": {"indexable": False, "title": seo.DEFAULT_TITLE, "description": ""}}

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
    def inject_site_links():
        """Expose the instance's contact email and source repository to templates.

        Both come from [site] (config.local.toml, or the FYP_CONTACT_EMAIL /
        FYP_REPO_URL env vars which override it at config load).

        contact_email: templates render their contact/feedback passages only
        when it is set, so a third-party install shows no foreign address.

        repo_url: the public pages point bug reports, feature requests, the
        installation guide and the licence at the source repository. It
        defaults to the canonical repo, so those links work out of the box;
        a fork can repoint it, and an empty value hides them. repo_issues_url
        is derived so templates don't each rebuild it.
        """
        from fyp.fyp_config import get_config
        try:
            site = get_config().get("site", {}) or {}
            contact_email = str(site.get("contact_email", "") or "").strip()
            repo_url = str(site.get("repo_url", "") or "").strip().rstrip("/")
        except Exception:
            contact_email, repo_url = "", ""
        return {
            "contact_email": contact_email,
            "repo_url": repo_url,
            "repo_issues_url": f"{repo_url}/issues" if repo_url else "",
        }

    @app.context_processor
    def inject_citation():
        """Expose the software citation (from CITATION.cff) to every template.

        The footer's "How to cite" box rides on every page — public and, since
        the home pane adopted the public footer, inside the app shell too — so
        a context processor is the only way to reach all of them without
        threading the value through every render_template call.
        """
        from web_interface.citation import get_citation
        try:
            return {"citation": get_citation()}
        except Exception:
            return {"citation": {"available": False}}

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
        from fyp.ingest import platform_url_templates

        from .permissions import get_user_permissions, visible_pipeline_steps
        from .slack_service import get_recent_messages
        slack_configured = bool(os.environ.get("SLACK_BOT_TOKEN"))
        slack_messages = get_recent_messages() if slack_configured else []
        user_perms = get_user_permissions(current_user)
        # The async (batch) annotator needs media as gs:// URIs, so its card is
        # only meaningful when media is GCS-backed (same flag resolve_media uses).
        media_on_gcs = bool(get_config().get("data_io", {}).get("use_gcs_for_media"))
        # Lets client-side "open on platform" links resolve per platform from the
        # same registry the viewer API uses (see fypPlatformUrl in main.js).
        # The home pane is a user guide: it walks only the pipeline stages this
        # user can actually reach (see permissions.visible_pipeline_steps).
        pipeline_steps = visible_pipeline_steps(current_user)
        # The site-wide default study (Admin -> Site Settings), or "" when the
        # operator has not picked one. study_state.js opens on it for users
        # who have not chosen a study themselves.
        from .admin_settings import get_default_study
        default_study = get_default_study()
        return render_template('index.html', user=current_user, user_perms=user_perms, slack_messages=slack_messages, slack_configured=slack_configured, media_on_gcs=media_on_gcs, platform_url_templates=platform_url_templates(), pipeline_steps=pipeline_steps, default_study=default_study)


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




def _migrate_study_access_defaults():
    """Backfill explicit USER_ACCESS grants after the empty-means-none flip.

    Runs only in processes that actually SERVE the web UI (the Cloud Run hub
    and the local dev server) — never on a plain import (tests, scripts) and
    never on the task-runner. It writes ``studies.json``, so an import-time
    call could clobber real definitions with whatever a test happened to have
    monkeypatched into the config. Grants the role names that exist at
    migration time, excluding admin (bypasses access checks) and the
    restricted roles (student) whose exclusion is the point of the flip.
    Idempotent; a storage failure is logged and never blocks boot.
    """
    from .auth import ROLE_ADMIN, role_manager
    from .permissions import PERMISSION_MIGRATION_SKIP_ROLES

    try:
        from fyp.studies import migrate_user_access_defaults

        grant_roles = [
            name for name in role_manager.get_roles()
            if name != ROLE_ADMIN and name not in PERMISSION_MIGRATION_SKIP_ROLES
        ]
        migrated = migrate_user_access_defaults(grant_roles)
        if migrated:
            print(f"USER_ACCESS migration: backfilled {migrated} studies.", flush=True)
    except Exception as e:
        print(f"USER_ACCESS migration failed (will retry next boot): {e}", flush=True)


app = create_app()


# Boot-time system-health check: spawns a daemon thread (or skips while the
# persisted result is still fresh) — never blocks startup. Triggered only in
# processes that actually serve the web UI: the Cloud Run web service (gunicorn
# imports this module with K_SERVICE set) and the local dev server (the
# reloader child below). Plain imports (tests, scripts) must not spawn probes.
if not _IS_TASK_RUNNER and os.environ.get("K_SERVICE"):
    from .services import system_health
    system_health.maybe_start_boot_check()
    _migrate_study_access_defaults()


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
        _migrate_study_access_defaults()
    port = int(os.environ.get("PORT", 5002))
    app.run(host='0.0.0.0', port=port, debug=debug)
