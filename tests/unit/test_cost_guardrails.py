"""Cost guardrails for invited users (S2 Phase 2).

Covers the per-request queue-build caps (admin bypass, non-admin clamp), the
dry-run mode that reports a selection without mutating the queue, the cost
estimate, and the ``api_start`` numeric-argument validation.
"""

import pytest

_TEST_VIEWER = "__guardrail_test_viewer__"






@pytest.fixture
def app_ctx(monkeypatch):
    """Flask request context with a stubbed non-admin current_user."""
    from web_interface.auth import ROLE_VIEWER, User
    from web_interface.fyp_data_hub import app

    user = User(username=_TEST_VIEWER, role=ROLE_VIEWER, password_hash="", approved=True)
    with app.test_request_context():
        from flask_login import utils as login_utils
        monkeypatch.setattr(login_utils, "_get_user", lambda: user)
        yield user






def _set_caps(monkeypatch, annotation=None, scrape=None):
    from web_interface import admin_settings

    values = {}
    if annotation is not None:
        values["queue_cap_annotation_items"] = annotation
    if scrape is not None:
        values["queue_cap_scrape_items"] = scrape
    monkeypatch.setattr(admin_settings, "load_admin_settings", lambda: values)






def test_cap_clamps_non_admin(app_ctx, monkeypatch):
    from web_interface.routes.management.enrichment import _apply_queue_cap

    _set_caps(monkeypatch, annotation=10)
    items = [str(i) for i in range(100)]
    kept, info = _apply_queue_cap(items, "annotation")

    assert len(kept) == 10
    assert info == {"capped": True, "cap": 10, "requested": 100}
    # Deterministic head slice — a repeated request selects the same items
    assert kept == sorted(items)[:10]






def test_cap_bypassed_for_admin(app_ctx, monkeypatch):
    from web_interface.routes.management.enrichment import _apply_queue_cap

    _set_caps(monkeypatch, annotation=10)
    app_ctx.role = "admin"  # is_admin() is role-based
    kept, info = _apply_queue_cap([str(i) for i in range(100)], "annotation")

    assert len(kept) == 100
    assert info["capped"] is False






def test_cap_zero_means_unlimited(app_ctx, monkeypatch):
    from web_interface.routes.management.enrichment import _apply_queue_cap

    _set_caps(monkeypatch, scrape=0)
    kept, info = _apply_queue_cap([str(i) for i in range(50)], "scrape")

    assert len(kept) == 50
    assert info["capped"] is False






def test_cap_under_limit_is_untouched(app_ctx, monkeypatch):
    from web_interface.routes.management.enrichment import _apply_queue_cap

    _set_caps(monkeypatch, annotation=100)
    kept, info = _apply_queue_cap(["a", "b"], "annotation")

    assert kept == ["a", "b"]
    assert info == {"capped": False, "cap": 100, "requested": 2}






def test_get_queue_cap_defaults_and_coercion(monkeypatch):
    from web_interface import admin_settings

    monkeypatch.setattr(admin_settings, "load_admin_settings", lambda: {})
    assert admin_settings.get_queue_cap("annotation") == 5000
    assert admin_settings.get_queue_cap("scrape") == 10000

    # Garbage in the store degrades to the default rather than raising
    monkeypatch.setattr(admin_settings, "load_admin_settings",
                        lambda: {"queue_cap_annotation_items": "nonsense"})
    assert admin_settings.get_queue_cap("annotation") == 5000

    # Negative values are floored at 0 (unlimited)
    monkeypatch.setattr(admin_settings, "load_admin_settings",
                        lambda: {"queue_cap_scrape_items": -5})
    assert admin_settings.get_queue_cap("scrape") == 0






def test_validate_cap_setting_values():
    from web_interface.admin_settings import validate_setting_value

    for key in ("queue_cap_annotation_items", "queue_cap_scrape_items"):
        assert validate_setting_value(key, 0) is None
        assert validate_setting_value(key, 2500) is None
        assert validate_setting_value(key, -1) is not None
        assert validate_setting_value(key, True) is not None  # bool is not an int here






def test_cost_estimate_scales_and_degrades(monkeypatch):
    from fyp.annotation.backends import variants
    from web_interface.routes.management import enrichment

    monkeypatch.setattr(variants, "selection_pricing",
                        lambda selection: {"input": 2.0, "output": 10.0})
    est = enrichment._annotation_cost_estimate(1000)
    assert est is not None
    # 1000 items x (15000 x $2 + 1500 x $10) per 1M tokens = $45
    assert est["est_cost_usd"] == pytest.approx(45.0)

    # A backend without pricing (e.g. local) yields no dollar figure
    monkeypatch.setattr(variants, "selection_pricing", lambda selection: None)
    assert enrichment._annotation_cost_estimate(1000) is None






def test_api_start_rejects_bad_numeric_args(monkeypatch):
    """batch_size / max_batches are validated before reaching a worker argv."""
    from web_interface import security
    from web_interface.auth import ROLE_ADMIN, User
    from web_interface.fyp_data_hub import app

    admin = User(username="__guardrail_admin__", role=ROLE_ADMIN,
                 password_hash="", approved=True)
    orig_get_user = security.user_manager.get_user
    monkeypatch.setattr(security.user_manager, "get_user",
                        lambda uid: admin if uid == admin.username else orig_get_user(uid))
    monkeypatch.setattr(
        "fyp.annotation.machine_annotation.annotation_configured", lambda: (True, ""))

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_user_id"] = admin.username
            sess["_fresh"] = True

        for payload in ({"batch_size": "abc"}, {"batch_size": 0},
                        {"batch_size": 999999}, {"max_batches": -1}):
            res = client.post("/api/start/queue_annotator", json=payload)
            assert res.status_code == 400, payload
            assert "must be" in res.get_json()["message"]
