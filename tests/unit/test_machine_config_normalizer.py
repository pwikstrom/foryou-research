"""Legacy flat [machine] → nested [machine.gemini] normalization at load.

The normalizer is what lets pre-restructure configs and config.local.toml
overlays keep working: flat Gemini keys hoist into [machine.gemini] (flat
wins — a flat key is explicit old-overlay intent), flat [machine.variants]
blocks hoist under their backend, and the loaded config never carries the
flat keys afterwards (single in-memory location, no split-brain).
"""

from fyp.core.fyp_config import _LEGACY_GEMINI_KEYS, _normalize_machine_config
from fyp.fyp_config import get_config






def test_flat_keys_hoist_and_win_over_nested():
    cf = {"machine": {
        "model": "flat-model",            # old overlay's explicit value
        "temperature": 0.7,
        "gemini": {"model": "nested-model", "vertexai": True},
    }}
    _normalize_machine_config(cf)
    gemini = cf["machine"]["gemini"]
    assert gemini["model"] == "flat-model"      # flat wins
    assert gemini["temperature"] == 0.7
    assert gemini["vertexai"] is True           # nested-only key survives
    for key in _LEGACY_GEMINI_KEYS:
        assert key not in cf["machine"]         # no split-brain






def test_fully_flat_legacy_config_hoists_completely():
    cf = {"machine": {
        "key": "", "model": "gemini-3-flash-preview", "vertexai": True,
        "project": "p", "location": "global", "temperature": 0.0,
        "max_output_tokens": 65536, "thinking_budget": -1,
        "media_resolution": "", "max_retries": 2, "retry_base_delay": 2.0,
        "http_options_api_version": "v1", "http_options_timeout": 180000,
        "max_duration_for_annotation": 300,
    }}
    _normalize_machine_config(cf)
    gemini = cf["machine"]["gemini"]
    assert gemini["model"] == "gemini-3-flash-preview"
    assert gemini["project"] == "p"
    # Generic annotation settings stay flat.
    assert cf["machine"]["max_duration_for_annotation"] == 300
    assert "model" not in cf["machine"]






def test_legacy_flat_variants_hoist_under_their_backend():
    cf = {"machine": {
        "gemini": {"model": "m"},
        "qwen_api": {"model_id": "q"},
        "variants": {
            "gemini_35": {"backend": "gemini", "model": "gemini-3.5-flash"},
            "qwen_next": {"backend": "qwen_api", "model_id": "qwen4"},
            "implied_gemini": {"model": "x"},   # no backend key -> gemini
        },
    }}
    _normalize_machine_config(cf)
    assert cf["machine"]["gemini"]["variants"]["gemini_35"] == {"model": "gemini-3.5-flash"}
    assert cf["machine"]["gemini"]["variants"]["implied_gemini"] == {"model": "x"}
    assert cf["machine"]["qwen_api"]["variants"]["qwen_next"] == {"model_id": "qwen4"}
    assert "variants" not in cf["machine"]






def test_legacy_pricing_table_is_dropped():
    cf = {"machine": {"gemini": {"model": "m"},
                      "pricing": {"gemini-3-flash-preview": {"input": 0.5}}}}
    _normalize_machine_config(cf)
    assert "pricing" not in cf["machine"]
    assert "pricing" not in cf["machine"]["gemini"]  # never misattributed






def test_loaded_config_has_no_flat_gemini_keys():
    """The guard: the real loaded config must never regrow flat keys."""
    machine = get_config()["machine"]
    for key in _LEGACY_GEMINI_KEYS:
        assert key not in machine, f"flat [machine].{key} leaked past the normalizer"
    assert "variants" not in machine
    assert "gemini" in machine and "model" in machine["gemini"]






def test_descriptor_identical_for_flat_and_nested_config(monkeypatch):
    """av_ stability: relocation must not move the hash (value-derived)."""
    import fyp.annotation_versioning as av

    nested = av.current_version_descriptor(fresh=True)

    # Simulate the pre-restructure layout: copy the gemini values back to
    # flat keys, re-normalize, and confirm the descriptor is byte-identical.
    machine = get_config()["machine"]
    legacy = {**{k: v for k, v in machine.items() if k != "gemini"},
              **{k: v for k, v in machine["gemini"].items()
                 if k in _LEGACY_GEMINI_KEYS}}
    cf = {"machine": legacy}
    _normalize_machine_config(cf)
    for key in ("model", "temperature", "thinking_budget", "media_resolution",
                "max_output_tokens", "version_label"):
        assert cf["machine"]["gemini"].get(key) == machine["gemini"].get(key)

    again = av.current_version_descriptor(fresh=True)
    assert again == nested
