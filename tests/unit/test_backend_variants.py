"""Config-declared backend variants: registry, resolution, hash behavior.

The load-bearing guarantees: (1) declaring variants never moves the default
gemini ``av_`` hash, and (2) a gemini variant whose overrides equal the
defaults produces the *same* hash via the generic descriptor branch as the
legacy branch does — proving the two paths agree.

Variants are nested per backend (``[machine.<backend>.variants.<name>]``);
the fixture replaces every backend's variants table so the committed config's
real variants never leak into assertions.
"""

import pytest

import fyp.annotation.backends as backends
import fyp.annotation_versioning as av
from fyp.annotation.backends import settings as backend_settings
from fyp.annotation.backends import variants
from fyp.fyp_config import get_config






@pytest.fixture
def variant_config(monkeypatch):
    """Declare per-backend test variants in the live config (auto-restored).

    Accepts ``{backend_id: {name: block}}``; backends not mentioned get an
    empty variants table for the test's duration.
    """
    declared_names: set = set()

    def declare(per_backend: dict):
        machine = get_config()["machine"]
        for backend_id in backends.BACKEND_IDS:
            block = machine.setdefault(backend_id, {})
            monkeypatch.setitem(block, "variants", per_backend.get(backend_id, {}))
            for name in per_backend.get(backend_id, {}):
                declared_names.add(name)
                backends._instances.pop(name, None)

    yield declare
    for name in declared_names:
        backends._instances.pop(name, None)






def test_declared_variants_parse_and_skip_invalid(variant_config):
    variant_config({
        "gemini": {
            "gemini_35": {"model": "gemini-3.5-flash", "label": "Gemini 3.5 Flash",
                          "pricing": {"input": 0.3, "output": 2.5}},
            "gemini": {"model": "x"},                       # id collision
            "Bad-Name": {"model": "x"},                     # charset
        },
        "qwen_api": {
            "wrong_parent": {"backend": "gemini", "model_id": "x"},  # contradiction
        },
    })
    declared = variants.declared_variants()
    assert list(declared) == ["gemini_35"]
    spec = declared["gemini_35"]
    assert spec.backend_id == "gemini"
    assert spec.overrides == {"model": "gemini-3.5-flash"}  # label/pricing = metadata
    assert spec.label == "Gemini 3.5 Flash"
    assert spec.pricing == {"input": 0.3, "output": 2.5}
    assert variants.selection_ids() == backends.BACKEND_IDS + ("gemini_35",)






def test_resolve_implementation_and_variant(variant_config):
    variant_config({"qwen_api": {"qwen_hosted_plus": {"model_id": "qwen4-omni"}}})
    default = variants.resolve("gemini")
    assert default.backend_id == "gemini" and default.overrides == {}
    spec = variants.resolve("qwen_hosted_plus")
    assert spec.backend_id == "qwen_api"
    assert spec.overrides == {"model_id": "qwen4-omni"}
    with pytest.raises(ValueError, match="Unknown annotation backend"):
        variants.resolve("nope")






def test_redundant_backend_key_accepted_when_matching(variant_config):
    variant_config({"gemini": {"gemini_35": {"backend": "gemini",
                                             "model": "gemini-3.5-flash"}}})
    spec = variants.resolve("gemini_35")
    assert spec.backend_id == "gemini"
    assert spec.overrides == {"model": "gemini-3.5-flash"}  # backend key not an override






def test_get_backend_variant_instance(variant_config):
    variant_config({"gemini": {"gemini_35": {"model": "gemini-3.5-flash"}}})
    b = backends.get_backend("gemini_35")
    assert type(b).__name__ == "GeminiBackend"
    assert b.name == "gemini"
    assert b.selection == "gemini_35"
    assert b.overrides == {"model": "gemini-3.5-flash"}
    assert b.effective_model_id() == "gemini-3.5-flash"
    assert backends.get_backend("gemini_35") is b  # cached per selection

    default = backends.get_backend("gemini")
    assert default.overrides == {} and default.selection == "gemini"
    assert default.effective_model_id() == get_config()["machine"]["gemini"]["model"]






def test_active_backend_name_falls_back_on_removed_variant(monkeypatch):
    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "gone_variant"})
    assert backends.active_backend_name() == "gemini"






def test_active_backend_name_accepts_declared_variant(variant_config, monkeypatch):
    variant_config({"gemini": {"gemini_35": {"model": "gemini-3.5-flash"}}})
    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "gemini_35"})
    assert backends.active_backend_name() == "gemini_35"






def test_default_gemini_hash_unmoved_by_variant_declarations(variant_config, monkeypatch):
    monkeypatch.setattr(backend_settings, "_load_settings", lambda: {})
    variant_config({})
    baseline = av.active_version_descriptor(fresh=True)["annotation_version"]

    variant_config({"gemini": {"gemini_x": {"model": "gemini-x-flash"}}})
    assert av.active_version_descriptor(fresh=True)["annotation_version"] == baseline






def test_gemini_variant_identical_to_default_yields_same_hash(variant_config, monkeypatch):
    """Generic-branch descriptor for a no-op gemini variant == legacy branch."""
    monkeypatch.setattr(backend_settings, "_load_settings", lambda: {})
    variant_config({})
    baseline = av.active_version_descriptor(fresh=True)

    gemini_cf = get_config()["machine"]["gemini"]
    variant_config({"gemini": {"gemini_same": {"model": gemini_cf["model"]}}})
    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "gemini_same"})
    same = av.active_version_descriptor(fresh=True)
    assert same["annotation_version"] == baseline["annotation_version"]
    assert same.get("variant") == "gemini_same"  # provenance only
    assert "backend" not in same  # gemini implementation is normalized away






def test_gemini_variant_new_model_forks_the_version(variant_config, monkeypatch):
    monkeypatch.setattr(backend_settings, "_load_settings", lambda: {})
    variant_config({})
    baseline = av.active_version_descriptor(fresh=True)["annotation_version"]

    variant_config({"gemini": {"gemini_x": {"model": "gemini-x-flash"}}})
    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "gemini_x"})
    forked = av.active_version_descriptor(fresh=True)
    assert forked["annotation_version"] != baseline
    assert forked["model"] == "gemini-x-flash"
    assert forked["variant"] == "gemini_x"

    # Cache signature includes the overrides: same [machine.gemini] keys,
    # different variant override -> new descriptor without fresh=True.
    variant_config({"gemini": {"gemini_x": {"model": "gemini-y-flash"}}})
    backends._instances.pop("gemini_x", None)
    assert av.active_version_descriptor()["model"] == "gemini-y-flash"






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






def test_selection_pricing_precedence(variant_config, monkeypatch):
    monkeypatch.setitem(get_config()["machine"]["gemini"], "pricing",
                        {"input": 0.5, "output": 3.0})
    variant_config({"gemini": {
        "priced": {"model": "m1", "pricing": {"input": 0.3, "output": 2.5}},
        "unpriced": {"model": "m2"},
    }})
    assert variants.selection_pricing("priced") == {"input": 0.3, "output": 2.5}
    # No variant pricing -> inherits the backend block's.
    assert variants.selection_pricing("unpriced") == {"input": 0.5, "output": 3.0}
    assert variants.selection_pricing("gemini") == {"input": 0.5, "output": 3.0}
    # Local backends declare no pricing at all.
    assert variants.selection_pricing("qwen_local") is None






def test_runner_for_arm_merges_gemini_variant_overrides(variant_config):
    from fyp import ab_eval

    variant_config({"gemini": {"gemini_35": {"model": "gemini-3.5-flash",
                                             "temperature": 0.3}}})
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

    variant_config({"qwen_api": {"qwen_next": {"model_id": "qwen4-omni"}}})
    runner = ab_eval._runner_for_arm({"backend": "qwen_next"})
    assert isinstance(runner, ab_eval.BackendSequentialRunner)
    assert runner.backend.selection == "qwen_next"
    assert runner.backend.overrides == {"model_id": "qwen4-omni"}
    assert runner.backend.effective_model_id() == "qwen4-omni"






def test_estimate_seconds_classifies_gemini_variant(variant_config, monkeypatch):
    from web_interface import run_queue_annotator as rqa

    variant_config({"gemini": {"gemini_35": {"model": "gemini-3.5-flash"}}})
    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "gemini_35"})
    gemini_estimate = rqa._estimate_seconds(100)
    assert gemini_estimate == 100 * rqa._SECONDS_PER_VIDEO / rqa._WORKERS * rqa._SAFETY_MARGIN

    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "qwen_local"})
    assert rqa._estimate_seconds(100) > gemini_estimate
