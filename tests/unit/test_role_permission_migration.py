#!/usr/bin/env python3
"""Tests for the boot-time roles.json permission-key migration.

Covers the 2026-07 Admin-tab restructure: roles holding an old umbrella key
(``tab.admin.general`` / ``tab.admin.schema``) must receive the split-out
page keys via ``PERMISSION_KEY_IMPLIED_GRANTS``, while the new Scrapers page
stays admin-only. Uses an in-memory data_io stub — no real storage is touched.

Usage:
    PYTHONPATH=. python tests/unit/test_role_permission_migration.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))




class _StubDataIO:
    """In-memory roles.json store; records whether a save happened."""

    def __init__(self, payload):
        self.payload = payload
        self.saved = None

    def exists(self, storage_location=None, filename=None):
        return True

    def load_json(self, storage_location=None, filename=None):
        return self.payload

    def save_json(self, data=None, storage_location=None, filename=None):
        self.saved = data




def _make_role_manager(payload):
    import web_interface.auth as auth_mod

    stub = _StubDataIO(payload)
    original = auth_mod.data_io
    auth_mod.data_io = stub
    try:
        rm = auth_mod.RoleManager()
    finally:
        auth_mod.data_io = original
    return rm, stub




def test_umbrella_keys_imply_split_out_pages():
    rm, stub = _make_role_manager({
        "custom": {"permissions": ["tab.admin.general", "tab.admin.schema", "tab.explore"]},
        "admin": {"permissions": ["*"]},
    })
    perms = set(rm.roles["custom"]["permissions"])
    assert {"tab.admin.backends", "tab.admin.stoplist",
            "tab.admin.versions", "tab.admin.ab_eval"} <= perms
    # New functionality is not implied — granted explicitly or via admin.
    assert "tab.admin.scrapers" not in perms
    # The umbrella keys themselves survive.
    assert {"tab.admin.general", "tab.admin.schema"} <= perms
    assert stub.saved is not None
    print("PASS: umbrella keys imply split-out pages")




def test_migration_is_idempotent_and_skips_wildcard():
    already = {
        "custom": {"permissions": [
            "tab.admin.general", "tab.admin.backends", "tab.admin.stoplist",
            # GRANT_ALL keys must already be present or the older My-stuff
            # migration path saves and muddies the idempotency assertion.
            "tab.my_stuff.tasks", "tab.my_stuff.preferences",
            "tab.my_stuff.video_tags", "tab.my_stuff.profile",
        ]},
        "admin": {"permissions": ["*"]},
        # _ensure_defaults adds a missing viewer role (and saves) — include it
        # so the only possible save is the migration under test.
        "viewer": {"permissions": [
            "tab.my_stuff.tasks", "tab.my_stuff.preferences",
            "tab.my_stuff.video_tags", "tab.my_stuff.profile",
        ]},
    }
    rm, stub = _make_role_manager(already)
    assert stub.saved is None, "no-op migration must not save"
    assert rm.roles["admin"]["permissions"] == ["*"]
    print("PASS: idempotent, wildcard untouched")




def test_enrichment_key_implies_scrape_and_annotation():
    # 2026-07 Data Management restructure: "Scrape & Annotate" split into
    # Scrape + Annotation pages; roles holding the old key gain both.
    rm, stub = _make_role_manager({
        "custom": {"permissions": ["tab.data_management.enrichment"]},
        "admin": {"permissions": ["*"]},
    })
    perms = set(rm.roles["custom"]["permissions"])
    assert {"tab.data_management.scrape", "tab.data_management.annotation"} <= perms
    # The stale umbrella key survives (harmless — no longer in the catalog).
    assert "tab.data_management.enrichment" in perms
    assert stub.saved is not None
    print("PASS: enrichment key implies scrape + annotation")




def run():
    test_umbrella_keys_imply_split_out_pages()
    test_migration_is_idempotent_and_skips_wildcard()
    test_enrichment_key_implies_scrape_and_annotation()




if __name__ == "__main__":
    run()
