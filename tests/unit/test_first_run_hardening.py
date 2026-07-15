"""Small first-run hardening guards: FLASK_DEBUG parsing and Gemini gating.

Companions to test_local_first_config.py for the public-release changes:
the dev server's debug mode is env-gated, and an unconfigured Vertex project
leaves the Gemini client None (with a warning) instead of touching the
network or billing a foreign GCP project.
"""

from __future__ import annotations

import pytest

import fyp.machine_annotation as machine_annotation
from web_interface.fyp_data_hub import _debug_enabled






@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("TRUE", True),
        (" yes ", True),
    ],
)
def test_debug_enabled_parsing(value, expected):
    """FLASK_DEBUG accepts the usual truthy spellings and defaults off."""
    assert _debug_enabled(value) is expected






def test_initialize_machine_unconfigured_project_stays_none(monkeypatch, caplog):
    """vertexai=true with an empty project: no client, no network, a warning."""
    machine = machine_annotation._cf()["machine"]
    monkeypatch.setitem(machine, "client", None)
    monkeypatch.setitem(machine, "vertexai", True)
    monkeypatch.setitem(machine, "project", "")
    monkeypatch.setattr(
        machine_annotation.fyp_utils, "online_ok",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not probe the network")),
    )

    with caplog.at_level("WARNING"):
        machine_annotation.initialize_machine()

    assert machine["client"] is None
    assert any("not configured" in r.message for r in caplog.records)






def test_generate_with_retry_raises_clearly_without_client(monkeypatch):
    """The retry wrapper fails with a configuration message, not AttributeError."""
    machine = machine_annotation._cf()["machine"]
    monkeypatch.setitem(machine, "client", None)

    with pytest.raises(RuntimeError, match="not configured"):
        machine_annotation._generate_with_retry(contents=[], gen_config=None)
