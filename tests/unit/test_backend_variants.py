"""Config-declared backend variants: registry, resolution, hash behavior.

The load-bearing guarantees: (1) declaring variants never moves the default
gemini ``av_`` hash, and (2) a gemini variant whose overrides equal the
defaults produces the *same* hash via the generic descriptor branch as the
legacy branch does — proving the two paths agree.
"""

import pytest

import fyp.annotation.backends as backends
import fyp.annotation_versioning as av
from fyp.annotation.backends import variants
from fyp.annotation.backends import settings as backend_settings
from fyp.fyp_config import get_config






@pytest.fixture
def variant_config(monkeypatch):
    """Declare test variants in the live config (auto-restored)."""

    def declare(blocks: dict):
        monkeypatch.setitem(get_config()["machine"], "variants", blocks)
        # Instances are cached per selection; drop any prior test's entries.
        for name in blocks:
            backends._instances.pop(name, None)

    yield declare
    for name in (get_config()["machine"].get("variants") or {}):
        backends._instances.pop(name, None)






def test_declared_variants_parse_and_skip_invalid(variant_config):
    variant_config({
        "gemini_35": {"backend": "gemini", "model": "gemini-3.5-flash",
                      "label": "Gemini 3.5 Flash"},
        "gemini": {"backend": "gemini", "model": "x"},          # id collision
        "Bad-Name": {"backend": "gemini"},                       # charset
        "no_backend": {"model": "x"},                            # missing backend
        "bad_backend": {"backend": "nope"},                      # unknown impl
    })
    declared = variants.declared_variants()
    assert list(declared) == ["gemini_35"]
    spec = declared["gemini_35"]
    assert spec.backend_id == "gemini"
    assert spec.overrides == {"model": "gemini-3.5-flash"}
    assert spec.label == "Gemini 3.5 Flash"
    assert variants.selection_ids() == backends.BACKEND_IDS + ("gemini_35",)






def test_resolve_implementation_and_variant(variant_config):
    variant_config({"qwen_hosted_plus": {"backend": "qwen_api",
                                         "model_id": "qwen4-omni"}})
    default = variants.resolve("gemini")
    assert default.backend_id == "gemini" and default.overrides == {}
    spec = variants.resolve("qwen_hosted_plus")
    assert spec.backend_id == "qwen_api"
    assert spec.overrides == {"model_id": "qwen4-omni"}
    with pytest.raises(ValueError, match="Unknown annotation backend"):
        variants.resolve("nope")






def test_get_backend_variant_instance(variant_config):
    variant_config({"gemini_35": {"backend": "gemini", "model": "gemini-3.5-flash"}})
    b = backends.get_backend("gemini_35")
    assert type(b).__name__ == "GeminiBackend"
    assert b.name == "gemini"
    assert b.selection == "gemini_35"
    assert b.overrides == {"model": "gemini-3.5-flash"}
    assert b.effective_model_id() == "gemini-3.5-flash"
    assert backends.get_backend("gemini_35") is b  # cached per selection

    default = backends.get_backend("gemini")
    assert default.overrides == {} and default.selection == "gemini"
    assert default.effective_model_id() == get_config()["machine"]["model"]






def test_active_backend_name_falls_back_on_removed_variant(monkeypatch):
    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "gone_variant"})
    assert backends.active_backend_name() == "gemini"






def test_active_backend_name_accepts_declared_variant(variant_config, monkeypatch):
    variant_config({"gemini_35": {"backend": "gemini", "model": "gemini-3.5-flash"}})
    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "gemini_35"})
    assert backends.active_backend_name() == "gemini_35"






def test_default_gemini_hash_unmoved_by_variant_declarations(variant_config, monkeypatch):
    monkeypatch.setattr(backend_settings, "_load_settings", lambda: {})
    baseline = av.current_version_descriptor(fresh=True)["annotation_version"]

    variant_config({"gemini_35": {"backend": "gemini", "model": "gemini-3.5-flash"}})
    assert av.current_version_descriptor(fresh=True)["annotation_version"] == baseline






def test_gemini_variant_identical_to_default_yields_same_hash(variant_config, monkeypatch):
    """Generic-branch descriptor for a no-op gemini variant == legacy branch."""
    monkeypatch.setattr(backend_settings, "_load_settings", lambda: {})
    baseline = av.current_version_descriptor(fresh=True)

    machine = get_config()["machine"]
    variant_config({"gemini_same": {
        "backend": "gemini", "model": machine["model"]}})
    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "gemini_same"})
    same = av.current_version_descriptor(fresh=True)
    assert same["annotation_version"] == baseline["annotation_version"]
    assert same.get("variant") == "gemini_same"  # provenance only
    assert "backend" not in same  # gemini implementation is normalized away






def test_gemini_variant_new_model_forks_the_version(variant_config, monkeypatch):
    monkeypatch.setattr(backend_settings, "_load_settings", lambda: {})
    baseline = av.current_version_descriptor(fresh=True)["annotation_version"]

    variant_config({"gemini_35": {"backend": "gemini", "model": "gemini-3.5-flash"}})
    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "gemini_35"})
    forked = av.current_version_descriptor(fresh=True)
    assert forked["annotation_version"] != baseline
    assert forked["model"] == "gemini-3.5-flash"
    assert forked["variant"] == "gemini_35"

    # Cache signature includes the overrides: same [machine] keys, different
    # variant override -> different cached descriptor without fresh=True.
    variant_config({"gemini_35": {"backend": "gemini", "model": "gemini-3.6-flash"}})
    assert av.current_version_descriptor()["model"] == "gemini-3.6-flash"






def test_variant_metadata_never_in_identity():
    gen_params = {"use_structured_output": True, "temperature": 0.0,
                  "thinking_budget": -1, "media_resolution": "",
                  "max_output_tokens": 65536}
    base = av.build_version_descriptor(
        model="m", prompt_text="p", schema_json=None, gen_params=gen_params)
    tagged = av.build_version_descriptor(
        model="m", prompt_text="p", schema_json=None, gen_params=gen_params,
        variant="gemini_35")
    assert tagged["annotation_version"] == base["annotation_version"]
    assert tagged["variant"] == "gemini_35"
    assert "variant" not in base






def test_runner_for_arm_merges_gemini_variant_overrides(variant_config):
    from fyp import ab_eval

    variant_config({"gemini_35": {"backend": "gemini", "model": "gemini-3.5-flash",
                                  "temperature": 0.3}})
    runner = ab_eval._runner_for_arm(
        {"backend": "gemini_35", "gen_overrides": {"temperature": 0.9}})
    assert isinstance(runner, ab_eval.SyncThreadedRunner)
    # Arm override wins over the variant's pin; the variant's model rides along.
    assert runner.gen_overrides == {"model": "gemini-3.5-flash", "temperature": 0.9}

    plain = ab_eval._runner_for_arm({"backend": "gemini"})
    assert isinstance(plain, ab_eval.SyncThreadedRunner)
    assert plain.gen_overrides is None






def test_runner_for_arm_variant_of_hosted_backend(variant_config):
    from fyp import ab_eval

    variant_config({"qwen_next": {"backend": "qwen_api", "model_id": "qwen4-omni"}})
    runner = ab_eval._runner_for_arm({"backend": "qwen_next"})
    assert isinstance(runner, ab_eval.BackendSequentialRunner)
    assert runner.backend.selection == "qwen_next"
    assert runner.backend.overrides == {"model_id": "qwen4-omni"}
    assert runner.backend.effective_model_id() == "qwen4-omni"






def test_estimate_seconds_classifies_gemini_variant(variant_config, monkeypatch):
    from web_interface import run_queue_annotator as rqa

    variant_config({"gemini_35": {"backend": "gemini", "model": "gemini-3.5-flash"}})
    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "gemini_35"})
    gemini_estimate = rqa._estimate_seconds(100)
    assert gemini_estimate == 100 * rqa._SECONDS_PER_VIDEO / rqa._WORKERS * rqa._SAFETY_MARGIN

    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "qwen_local"})
    assert rqa._estimate_seconds(100) > gemini_estimate
