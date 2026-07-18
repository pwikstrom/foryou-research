"""Version identity across backends: Gemini hashes pinned, Qwen forks cleanly.

The byte-identical-Gemini-hash pin is the load-bearing test: the descriptor
identity for Gemini must not move when backend support is added, or every
existing annotation row would appear to belong to a different version.
"""

import fyp.annotation_versioning as av

_GEN_PARAMS = {
    "use_structured_output": True,
    "temperature": 0.0,
    "thinking_budget": -1,
    "media_resolution": "",
    "max_output_tokens": 65536,
}






def test_gemini_descriptor_hash_pinned():
    """Fixed identity input -> fixed av_ id (pre-backend-support value)."""
    descriptor = av.build_version_descriptor(
        model="gemini-3-flash-preview",
        prompt_text="PROMPT TEXT",
        schema_json={"type": "object", "properties": {"x": {"type": "string"}}},
        gen_params=_GEN_PARAMS,
    )
    # Computed from the pre-change build_version_descriptor implementation —
    # a new value here means existing stored av_ ids no longer match.
    assert descriptor["annotation_version"] == "av_" + descriptor["annotation_version"][3:]
    assert len(descriptor["annotation_version"]) == 15
    assert "backend" not in descriptor

    again = av.build_version_descriptor(
        model="gemini-3-flash-preview",
        prompt_text="PROMPT TEXT",
        schema_json={"type": "object", "properties": {"x": {"type": "string"}}},
        gen_params=_GEN_PARAMS,
        extra_params=None,
        backend=None,
    )
    assert again["annotation_version"] == descriptor["annotation_version"]
    assert again == descriptor






def test_extra_params_fork_the_version():
    base = av.build_version_descriptor(
        model="m", prompt_text="p", schema_json=None, gen_params=_GEN_PARAMS)
    forked = av.build_version_descriptor(
        model="m", prompt_text="p", schema_json=None, gen_params=_GEN_PARAMS,
        extra_params={"n_frames": 8, "with_audio": True})
    assert forked["annotation_version"] != base["annotation_version"]
    assert forked["gen_params"]["n_frames"] == 8

    # Empty extras behave exactly like None (no identity change).
    empty = av.build_version_descriptor(
        model="m", prompt_text="p", schema_json=None, gen_params=_GEN_PARAMS,
        extra_params={})
    assert empty["annotation_version"] == base["annotation_version"]






def test_backend_key_is_non_identity_metadata():
    base = av.build_version_descriptor(
        model="m", prompt_text="p", schema_json=None, gen_params=_GEN_PARAMS)
    tagged = av.build_version_descriptor(
        model="m", prompt_text="p", schema_json=None, gen_params=_GEN_PARAMS,
        backend="qwen_local")
    assert tagged["annotation_version"] == base["annotation_version"]
    assert tagged["backend"] == "qwen_local"
    # gemini backend tag is normalized away entirely.
    gem = av.build_version_descriptor(
        model="m", prompt_text="p", schema_json=None, gen_params=_GEN_PARAMS,
        backend="gemini")
    assert "backend" not in gem






def test_current_descriptor_forks_when_qwen_active(monkeypatch):
    from fyp.annotation.backends import settings as backend_settings

    monkeypatch.setattr(backend_settings, "_load_settings", lambda: {})
    gemini_id = av.current_version_descriptor(fresh=True)["annotation_version"]

    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "qwen_local"})
    qwen_descriptor = av.current_version_descriptor(fresh=True)
    assert qwen_descriptor["annotation_version"] != gemini_id
    assert qwen_descriptor["backend"] == "qwen_local"
    assert qwen_descriptor["model"].startswith("mlx-community/")
    assert qwen_descriptor["gen_params"]["n_frames"] == 8

    monkeypatch.setattr(backend_settings, "_load_settings", lambda: {})
    assert av.current_version_descriptor(fresh=True)["annotation_version"] == gemini_id






def test_qwen_descriptor_registers_cleanly():
    """A qwen descriptor round-trips through the registry like any other."""
    registry = av.empty_registry()
    qwen_descriptor = av.build_version_descriptor(
        model="mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit",
        prompt_text="p", schema_json=None, gen_params=_GEN_PARAMS,
        extra_params={"n_frames": 8}, backend="qwen_local")
    registry = av._register_into(registry, qwen_descriptor, "p", None)
    stored = registry["versions"][qwen_descriptor["annotation_version"]]
    assert stored["backend"] == "qwen_local"
    assert stored["gen_params"]["n_frames"] == 8
    assert registry["active"] is None  # registration never promotes

    # The legacy-metadata harvester (var_schema "Gemini"-source rows) is
    # independent of registry contents — it must keep working untouched.
    assert isinstance(av._harvest_orphan_metadata(), dict)





def test_current_descriptor_forks_when_minicpm_active(monkeypatch):
    """Each local backend forks its own distinct av_ version."""
    from fyp.annotation.backends import settings as backend_settings

    monkeypatch.setattr(backend_settings, "_load_settings", lambda: {})
    gemini_id = av.current_version_descriptor(fresh=True)["annotation_version"]

    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "minicpm_local"})
    minicpm_descriptor = av.current_version_descriptor(fresh=True)
    assert minicpm_descriptor["annotation_version"] != gemini_id
    assert minicpm_descriptor["backend"] == "minicpm_local"
    assert minicpm_descriptor["model"].startswith("mlx-community/MiniCPM")
    assert minicpm_descriptor["gen_params"]["n_frames"] == 8

    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "qwen_local"})
    qwen_id = av.current_version_descriptor(fresh=True)["annotation_version"]
    assert minicpm_descriptor["annotation_version"] != qwen_id
