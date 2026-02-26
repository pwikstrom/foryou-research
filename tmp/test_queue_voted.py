import sys
import os

# Set up environment to run Flask context
sys.path.insert(0, os.path.abspath('.'))

from web_interface.app import create_app
from web_interface.auth import RoleManager, UserManager

# Manually load fyp config before initializing app
import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf

# initialize fyp env manually since it usually relies on cli
import fyp.init
app = create_app(testing=True)

with app.test_client() as client:
    # 1. Login
    print("Logging in...")
    login_res = client.post('/login', data={"username": "info@foryouresearch.net", "password": "kelvingrove"})
    
    # 2. Call the new endpoint
    print("Calling /api/manage/enrichment/queue_voted...")
    res = client.post('/api/manage/enrichment/queue_voted')
    print("Status:", res.status_code)
    print("Response payload:", res.get_data(as_text=True))
