from flask import Flask, render_template, redirect, url_for
from flask_login import login_required, current_user
import os
import sys
import numpy as np
import pandas as pd
import logging
from datetime import datetime

# --- Script Execution Support ---
from pathlib import Path
if __name__ == "__main__" and __package__ is None:
    file_path = Path(__file__).resolve()
    project_root = file_path.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    __package__ = "web_interface"

# Imports
from .process_manager import load_process_stats # Import load function
from .security import login_manager, user_manager # Import shared auth objects
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()

# Import Blueprints
from .routes.auth_routes import auth_bp
from .routes.process_routes import process_bp
from .routes.data_routes import data_bp
from .routes.ingest_routes import ingest_bp
from .data_service import study_cache # Re-export for tests

# Initialize stats
load_process_stats()

# Silence the noisy HTTP request logs from Flask/Werkzeug
#log = logging.getLogger('werkzeug')
#log.setLevel(logging.ERROR)

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
app.secret_key = os.environ.get("FLASK_SECRET_KEY")

# Init Auth
login_manager.init_app(app)
csrf.init_app(app)

# Register Blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(process_bp)
app.register_blueprint(data_bp)
app.register_blueprint(ingest_bp)


@app.route('/')
@login_required
def index():
    return render_template('index.html', user=current_user)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=True)
