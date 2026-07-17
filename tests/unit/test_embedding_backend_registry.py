"""Embedding-backend registry: lookup, fallbacks and the settings gate."""

import pytest

import fyp.analysis.embedding_backends as embedding_backends






def test_gemini_backend_registers_and_caches():
    b1 = embedding_backends.get_backend("gemini")
    b2 = embedding_backends.get_backend("gemini")
    assert b1 is b2
    assert b1.name == "gemini"
    assert b1.cloud_run_capable is True
    assert b1.model_id() == "gemini-embedding-001"
    assert b1.dim() == 1536






def test_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown embedding backend"):
        embedding_backends.get_backend("nope")






def test_backend_ids_closed_set():
    assert embedding_backends.BACKEND_IDS == ("gemini", "qwen_local")
    assert set(embedding_backends._BACKEND_MODULES) == set(embedding_backends.BACKEND_IDS)






def test_active_backend_name_defaults_to_gemini(monkeypatch):
    from fyp.analysis.embedding_backends import settings as embed_settings

    monkeypatch.setattr(embed_settings, "get_embedding_backend", lambda: "gemini")
    assert embedding_backends.active_backend_name() == "gemini"






def test_active_backend_name_reads_setting(monkeypatch):
    from fyp.analysis.embedding_backends import settings as embed_settings

    monkeypatch.setattr(embed_settings, "get_embedding_backend", lambda: "qwen_local")
    assert embedding_backends.active_backend_name() == "qwen_local"






def test_active_backend_name_rejects_unknown_value(monkeypatch):
    from fyp.analysis.embedding_backends import settings as embed_settings

    monkeypatch.setattr(embed_settings, "get_embedding_backend", lambda: "qwen_api")
    assert embedding_backends.active_backend_name() == "gemini"






def test_get_embedding_backend_survives_missing_store(monkeypatch):
    """A fresh install (no settings file) must fall back to gemini, not raise."""
    from fyp.analysis.embedding_backends import settings as embed_settings

    monkeypatch.setattr(embed_settings.data_io, "exists", lambda **kw: False)
    assert embed_settings.get_embedding_backend() == "gemini"






def test_gemini_availability_shape():
    """availability() returns the (ok, reason, checks) contract."""
    result = embedding_backends.get_backend("gemini").availability()
    assert isinstance(result.ok, bool)
    assert isinstance(result.reason, str)
    assert isinstance(result.checks, list)
    assert all({"name", "ok", "detail", "fix"} <= set(c) for c in result.checks)






def test_admin_settings_validation_for_embedding_backend():
    from web_interface.admin_settings import DEFAULTS, SETTING_TYPES, validate_setting_value

    assert DEFAULTS["embedding_backend"] == "gemini"
    assert SETTING_TYPES["embedding_backend"] is str
    assert validate_setting_value("embedding_backend", "gemini") is None
    assert validate_setting_value("embedding_backend", "qwen_local") is None
    assert "Unknown embedding backend" in validate_setting_value("embedding_backend", "bogus")
