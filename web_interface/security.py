from flask_login import LoginManager
from fyp.fyp_config import PROJECT_ROOT, fyp_cf
import web_interface.auth as auth

# --- Auth Setup ---
login_manager = LoginManager()
login_manager.login_view = 'auth_bp.login' # Updated to point to blueprint view
login_manager.anonymous_user = auth.AnonymousUser

# Initialize User Manager
user_manager = auth.UserManager(storage_location="users")

@login_manager.user_loader
def load_user(user_id):
    return user_manager.get_user(user_id)
