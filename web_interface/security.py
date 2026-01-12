from flask_login import LoginManager
from .hub_config import PROJECT_ROOT
import web_interface.auth as auth

# --- Auth Setup ---
login_manager = LoginManager()
login_manager.login_view = 'auth_bp.login' # Updated to point to blueprint view

# Initialize User Manager
USERS_FILE = PROJECT_ROOT / "config" / "users.json"
user_manager = auth.UserManager(USERS_FILE)

@login_manager.user_loader
def load_user(user_id):
    return user_manager.get_user(user_id)
