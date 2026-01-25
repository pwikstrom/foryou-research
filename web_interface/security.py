from flask_login import LoginManager
from .fyp_config import PROJECT_ROOT, fyp_cf
import web_interface.auth as auth

# --- Auth Setup ---
login_manager = LoginManager()
login_manager.login_view = 'auth_bp.login' # Updated to point to blueprint view

# Initialize User Manager
USERS_FILE = PROJECT_ROOT / "config" / "users.json"
GCS_BUCKET = fyp_cf['data_io'].get('bucket')
GCS_PATH = "config/users.json" # Stored in bucket root/config/

user_manager = auth.UserManager(USERS_FILE, gcs_bucket=GCS_BUCKET, gcs_path=GCS_PATH)

@login_manager.user_loader
def load_user(user_id):
    return user_manager.get_user(user_id)
