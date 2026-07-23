#!/usr/bin/env python3
"""API tests for POST /api/admin/irrelevant_words/apply (start endpoint).

Flask test client with an in-memory admin/viewer (patched via
flask_login.utils._get_user) and stubbed process-manager helpers so nothing is
actually started.

Usage:
    PYTHONPATH=. python tests/unit/test_retokenise_hashtags_api.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import flask_login.utils as fl_utils

from web_interface.auth import User




def _make_app():
    from web_interface.fyp_data_hub import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app




def run():
    app = _make_app()
    admin = User("apply-test-admin", "admin", password_hash="x", approved=True)
    viewer = User("apply-test-viewer", "viewer", password_hash="x", approved=True)

    import web_interface.process_manager as pm
    from web_interface.routes import management_routes as mr

    started = {"calls": []}
    orig_start = pm.start_process
    orig_running = mr._is_worker_running
    orig_blocking = mr._workers_blocking_consolidate
    orig_get_user = fl_utils._get_user

    pm.start_process = lambda name, script, *a, **k: (started["calls"].append(name), (True, "started"))[1]
    mr._is_worker_running = lambda name: False
    mr._workers_blocking_consolidate = lambda: []

    try:
        client = app.test_client()

        # Anonymous → redirect/401.
        resp = client.post("/api/admin/irrelevant_words/apply")
        assert resp.status_code in (302, 401), resp.status_code
        print("PASS: anonymous rejected")

        # Viewer lacking tab.admin.stoplist → 403.
        fl_utils._get_user = lambda: viewer
        resp = client.post("/api/admin/irrelevant_words/apply")
        assert resp.status_code == 403, resp.status_code
        print("PASS: viewer forbidden")

        # Admin, nothing blocking → started.
        fl_utils._get_user = lambda: admin
        resp = client.post("/api/admin/irrelevant_words/apply")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["status"] == "started"
        assert started["calls"] == ["retokenise_hashtags"]
        print("PASS: admin starts the job")

        # Already running → 409, no second start.
        mr._is_worker_running = lambda name: name == "retokenise_hashtags"
        resp = client.post("/api/admin/irrelevant_words/apply")
        assert resp.status_code == 409, resp.status_code
        assert started["calls"] == ["retokenise_hashtags"]
        print("PASS: refuses when already running")

        # A blocking worker (scraper) running → 409.
        mr._is_worker_running = lambda name: False
        mr._workers_blocking_consolidate = lambda: ["queue_scraper"]
        resp = client.post("/api/admin/irrelevant_words/apply")
        assert resp.status_code == 409, resp.status_code
        assert "queue_scraper" in resp.get_json()["message"]
        print("PASS: refuses while a scraper runs")
    finally:
        pm.start_process = orig_start
        mr._is_worker_running = orig_running
        mr._workers_blocking_consolidate = orig_blocking
        fl_utils._get_user = orig_get_user

    print("All retokenise-hashtags API tests passed.")




if __name__ == "__main__":
    run()
