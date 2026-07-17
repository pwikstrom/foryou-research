"""Runtime [machine] overrides: apply/revert, cache invalidation, av_ fork.

The version-fork guarantee is the load-bearing behavior: an admin temperature
(or model) override must produce a DIFFERENT ``av_`` version id than the
config baseline, and clearing the override must return the ORIGINAL id —
that's what keeps mixed-parameter annotation rows separated in the active
view without any new provenance machinery.
"""

import pytest

import fyp.annotation_versioning as annotation_versioning
import fyp.machine_annotation as machine_annotation
from fyp.annotation.backends import settings as backend_settings
from fyp.fyp_config import get_config






@pytest.fixture
def clean_overrides(monkeypatch):
    """Reset the baseline snapshot and restore [machine] values after the test."""
    machine = get_config()["machine"]
    saved = {key: machine[key] for key in backend_settings.MACHINE_OVERRIDE_KEYS.values()}
    saved_caches = {key: machine.get(key) for key in ("client", "structured_generation_config")}
    monkeypatch.setattr(machine_annotation, "_MACHINE_BASE", None)
    yield machine
    machine.update(saved)
    for key, value in saved_caches.items():
        machine[key] = value






def _fake_settings(monkeypatch, stored: dict):
    monkeypatch.setattr(backend_settings, "_load_settings", lambda: stored)






def test_override_lands_and_revert_restores(clean_overrides, monkeypatch):
    machine = clean_overrides
    baseline_temperature = machine["temperature"]
    baseline_model = machine["model"]

    _fake_settings(monkeypatch, {"machine_temperature": 0.7, "machine_model": "gemini-x-test"})
    applied = machine_annotation.apply_admin_machine_overrides()
    assert applied == {"temperature": 0.7, "model": "gemini-x-test"}
    assert machine["temperature"] == 0.7
    assert machine["model"] == "gemini-x-test"

    _fake_settings(monkeypatch, {})
    applied = machine_annotation.apply_admin_machine_overrides()
    assert applied == {}
    assert machine["temperature"] == baseline_temperature
    assert machine["model"] == baseline_model






def test_apply_invalidates_generation_config_cache(clean_overrides, monkeypatch):
    machine = clean_overrides
    machine["structured_generation_config"] = object()
    machine["client"] = object()

    _fake_settings(monkeypatch, {"machine_temperature": 0.9})
    machine_annotation.apply_admin_machine_overrides()
    assert machine["structured_generation_config"] is None
    assert machine["client"] is None






def test_noop_apply_keeps_caches(clean_overrides, monkeypatch):
    """Re-applying identical values must not needlessly drop the caches."""
    machine = clean_overrides
    _fake_settings(monkeypatch, {})
    machine_annotation.apply_admin_machine_overrides()

    sentinel = object()
    machine["structured_generation_config"] = sentinel
    machine_annotation.apply_admin_machine_overrides()
    assert machine["structured_generation_config"] is sentinel






def test_temperature_override_forks_annotation_version(clean_overrides, monkeypatch):
    machine = clean_overrides
    _fake_settings(monkeypatch, {})
    machine_annotation.apply_admin_machine_overrides()
    baseline_id = annotation_versioning.current_version_descriptor(fresh=True)["annotation_version"]

    _fake_settings(monkeypatch, {"machine_temperature": float(machine["temperature"]) + 0.5})
    machine_annotation.apply_admin_machine_overrides()
    forked_id = annotation_versioning.current_version_descriptor(fresh=True)["annotation_version"]
    assert forked_id != baseline_id

    _fake_settings(monkeypatch, {})
    machine_annotation.apply_admin_machine_overrides()
    reverted_id = annotation_versioning.current_version_descriptor(fresh=True)["annotation_version"]
    assert reverted_id == baseline_id






def test_model_override_forks_annotation_version(clean_overrides, monkeypatch):
    _fake_settings(monkeypatch, {})
    machine_annotation.apply_admin_machine_overrides()
    baseline_id = annotation_versioning.current_version_descriptor(fresh=True)["annotation_version"]

    _fake_settings(monkeypatch, {"machine_model": "some-other-model"})
    machine_annotation.apply_admin_machine_overrides()
    forked = annotation_versioning.current_version_descriptor(fresh=True)
    assert forked["annotation_version"] != baseline_id
    assert forked["model"] == "some-other-model"






def test_get_machine_overrides_skips_cleared(monkeypatch):
    _fake_settings(monkeypatch, {"machine_temperature": "", "machine_model": None,
                                 "machine_max_output_tokens": 1024})
    assert backend_settings.get_machine_overrides() == {"max_output_tokens": 1024}
