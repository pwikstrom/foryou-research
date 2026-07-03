import logging
import os
import sys
from datetime import datetime

# --- Script Execution Support ---
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template, request
from flask_login import current_user, login_required

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

# Import Blueprints
from .routes.api_collections_routes import collections_bp
from .routes.api_correlations_routes import correlations_bp
from .routes.api_explorer_routes import explorer_bp
from .routes.api_semantic_space_routes import semantic_space_bp
from .routes.api_timelines_routes import timelines_bp
from .routes.api_viewer_routes import viewer_bp
from .routes.auth_routes import auth_bp
from .routes.management_routes import management_bp
from .routes.process_routes import internal_bp, process_bp
from .slack_service import get_recent_messages
from .static_content import HOME_CONTENT

# Initialize stats
load_process_stats()

# Silence the noisy HTTP request logs from Flask/Werkzeug
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# --- Custom JSON Provider for Numpy/Pandas ---
from flask.json.provider import DefaultJSONProvider


class CustomJSONProvider(DefaultJSONProvider):
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

app.json = CustomJSONProvider(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "local-dev-key")

# Init Auth
login_manager.init_app(app)
# Validate CSRF tokens against the session only, with no separate time limit.
# The default 3600s expiry silently breaks state-changing POSTs from tabs left
# open longer than an hour (e.g. while the enrichment queues run), surfacing as
# opaque HTTP 400s on Consolidate & Refresh, collection delete, etc. Tokens stay
# bound to the session secret, so CSRF protection is unchanged.
app.config["WTF_CSRF_TIME_LIMIT"] = None
csrf.init_app(app)

# Register Blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(process_bp)
app.register_blueprint(internal_bp)
app.register_blueprint(explorer_bp)
app.register_blueprint(viewer_bp)
app.register_blueprint(timelines_bp)
app.register_blueprint(correlations_bp)
app.register_blueprint(semantic_space_bp)
app.register_blueprint(collections_bp)
app.register_blueprint(management_bp)

# Exempt the Cloud Tasks internal blueprint from CSRF (authenticated via OIDC token)
csrf.exempt(internal_bp)


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
@login_required
def index():
    slack_configured = bool(os.environ.get("SLACK_BOT_TOKEN"))
    slack_messages = get_recent_messages() if slack_configured else []
    from .permissions import get_user_permissions
    user_perms = get_user_permissions(current_user)
    return render_template('index.html', user=current_user, user_perms=user_perms, slack_messages=slack_messages, slack_configured=slack_configured, content=HOME_CONTENT)


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5002))
    app.run(host='0.0.0.0', port=port, debug=True)
