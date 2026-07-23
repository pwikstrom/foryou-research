#!/usr/bin/env python3
"""API tests for GET/PUT /api/admin/irrelevant_words.

Uses the Flask test client with an in-memory admin user (patched via
``flask_login.utils._get_user`` — nothing is persisted to the user store) and
a stubbed ``fyp.irrelevant_words`` data_io so no real storage is touched.

Usage:
    PYTHONPATH=. python tests/unit/test_irrelevant_words_api.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import flask_login.utils as fl_utils

from fyp import irrelevant_words as iw
from web_interface.auth import User




class _StubDataIO:
    """In-memory JSON store; never touches real storage."""

    def __init__(self, payload=None):
        self.payload = payload

    def exists(self, storage_location=None, filename=None):
        return self.payload is not None

    def load_json(self, storage_location=None, filename=None):
        return self.payload

    def save_json(self, data=None, storage_location=None, filename=None):
        self.payload = data




def _make_app():
    from web_interface.fyp_data_hub import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app




def run():
    app = _make_app()
    admin = User("api-test-admin", "admin", password_hash="x", approved=True)
    viewer = User("api-test-viewer", "viewer", password_hash="x", approved=True)

    stub = _StubDataIO()
    original_io = iw._data_io
    original_cf = iw._cf
    iw._data_io = lambda: stub
    iw._cf = lambda: {"labels": {"IRRELEVANT_WORDS": ["fyp", "fypp", "viral"]}}
    original_get_user = fl_utils._get_user
    try:
        client = app.test_client()

        # Unauthenticated: redirected to login, no payload leak.
        resp = client.get("/api/admin/irrelevant_words")
        assert resp.status_code in (302, 401), resp.status_code
        print("PASS: anonymous rejected")

        # Non-admin without tab.admin.stoplist: 403.
        fl_utils._get_user = lambda: viewer
        resp = client.get("/api/admin/irrelevant_words")
        assert resp.status_code == 403, resp.status_code
        print("PASS: viewer forbidden")

        # Admin GET: seeds from config (deduped) and returns etag.
        fl_utils._get_user = lambda: admin
        resp = client.get("/api/admin/irrelevant_words")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        data = resp.get_json()
        assert data["words"] == ["fyp", "viral"]
        assert data["count"] == 2
        etag = data["etag"]
        assert etag and etag != "missing"
        print("PASS: GET seeds + returns state")

        # PUT with fresh etag: saves, dedupes, audits.
        resp = client.put(
            "/api/admin/irrelevant_words",
            json={"words": ["fyp", "fyyyyp", "trending*", "dance"], "etag": etag},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        data = resp.get_json()
        assert data["words"] == ["dance", "fyp", "trending*"]
        new_etag = data["etag"]
        print("PASS: PUT saves + dedupes")

        # PUT with the stale etag: 409 + current server state.
        resp = client.put(
            "/api/admin/irrelevant_words",
            json={"words": ["only-mine"], "etag": etag},
        )
        assert resp.status_code == 409, resp.status_code
        data = resp.get_json()
        assert data["etag"] == new_etag
        assert data["words"] == ["dance", "fyp", "trending*"]
        print("PASS: stale etag 409")

        # PUT with invalid entries: 400.
        resp = client.put(
            "/api/admin/irrelevant_words",
            json={"words": ["ok", "f*"], "etag": new_etag},
        )
        assert resp.status_code == 400, resp.status_code
        print("PASS: invalid entry 400")

        # PUT without a words list: 400.
        resp = client.put("/api/admin/irrelevant_words", json={"nope": 1})
        assert resp.status_code == 400, resp.status_code
        print("PASS: malformed body 400")
    finally:
        fl_utils._get_user = original_get_user
        iw._data_io = original_io
        iw._cf = original_cf

    print("All irrelevant-words API tests passed.")




if __name__ == "__main__":
    run()
