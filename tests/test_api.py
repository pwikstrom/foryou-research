from web_interface.fyp_data_hub import app
from web_interface.routes.data_routes import api_persona_stats
from flask_login import current_user
import json
import traceback

class DummyUser:
    @property
    def is_authenticated(self): return True
    @property
    def username(self): return 'admin'
    @property
    def role(self): return 'admin'
    def is_admin(self): return True

with app.app_context():
    app.login_manager._login_disabled = True
    with app.test_request_context():
        # Mock current_user
        from flask import _request_ctx_stack
        
        try:
            res = api_persona_stats()
            print("Status:", res.status_code)
            data = res.json
            print("Length:", len(data) if data else 0)
            if data and len(data) > 0:
                print("First record keys:", list(data[0].keys())[:10])
                # check if there are any issues with metrics
                for k in ["collection_id", "total_events", "chattiness", "annotation_tags"]:
                    print(f"Sample {k}:", type(data[0].get(k)), data[0].get(k))
        except Exception as e:
            print("ERROR:")
            traceback.print_exc()
