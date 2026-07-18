"""Annotation-backend registry: lookup, fallbacks and the settings gate."""

import pytest

import fyp.annotation.backends as backends






def test_gemini_backend_registers_and_caches():
    b1 = backends.get_backend("gemini")
    b2 = backends.get_backend("gemini")
    assert b1 is b2
    assert b1.name == "gemini"
    assert b1.supports_batch_mode is True
    assert b1.cloud_run_capable is True






def test_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown annotation backend"):
        backends.get_backend("nope")






def test_reserved_qwen_api_id_not_accepted():
    """'qwen_api' is documented as reserved but must not resolve until built."""
    with pytest.raises(ValueError):
        backends.get_backend("qwen_api")






def test_active_backend_name_defaults_to_gemini(monkeypatch):
    from fyp.annotation.backends import settings as backend_settings

    monkeypatch.setattr(backend_settings, "_load_settings", lambda: {})
    assert backends.active_backend_name() == "gemini"






def test_active_backend_name_reads_setting(monkeypatch):
    from fyp.annotation.backends import settings as backend_settings

    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "qwen_local"})
    assert backends.active_backend_name() == "qwen_local"






def test_active_backend_name_rejects_unknown_value(monkeypatch):
    from fyp.annotation.backends import settings as backend_settings

    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "qwen_api"})
    assert backends.active_backend_name() == "gemini"






def test_gemini_availability_shape():
    """availability() returns the (ok, reason, checks) contract."""
    result = backends.get_backend("gemini").availability(deep=False)
    assert isinstance(result.ok, bool)
    assert isinstance(result.reason, str)
    assert isinstance(result.checks, list)
    for check in result.checks:
        assert {"name", "ok", "detail", "fix"} <= set(check)





def test_minicpm_id_registered(monkeypatch):
    """minicpm_local is a first-class id: in BACKEND_IDS and settings-valid."""
    from fyp.annotation.backends import settings as backend_settings

    assert "minicpm_local" in backends.BACKEND_IDS
    assert "minicpm_local" in backends._BACKEND_MODULES
    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "minicpm_local"})
    assert backends.active_backend_name() == "minicpm_local"
