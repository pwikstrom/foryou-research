"""Unit tests for Gemini mode resolution and client construction.

Pins the behaviour of ``fyp/core/gemini_client.py`` and the annotation gate it
backs:

* Mode resolution across the ``vertexai`` x ``project`` x ``key`` matrix,
  including the fallback that rescues the commonest user error — exporting
  ``GEMINI_API_KEY`` while ``vertexai = true`` (the default) and no project is
  set. Vertex cannot work without a project, so the key is used instead.
* ``make_client`` never passes ``project``/``location`` alongside ``api_key``:
  google-genai rejects the combination outright ("Project/location and API key
  are mutually exclusive in the client initializer"). A stale ``project`` left
  behind by someone switching Vertex -> API key must not resurface.
* ``annotation_configured`` agrees with ``gemini_mode``, so the pre-flight gate
  can never approve a config the worker then fails on, and additionally refuses
  API-key mode with GCS-stored media (handed to Gemini as a ``gs://`` URI that
  only Vertex can read).

No network and no API key — clients are constructed but never called, and
construction is offline in google-genai.

Usage:
    python tests/unit/test_gemini_client_modes.py
Exit 0 iff all checks pass.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fyp.core.gemini_client as gc
from fyp.annotation.machine_annotation import annotation_configured
from fyp.fyp_config import fyp_cf


@contextmanager
def _machine(vertexai: bool, project: str, key: str, use_gcs_for_media: bool = False):
    """Temporarily install a [machine]/[data_io] config combination.

    Also pins the active annotation backend to gemini for the duration — this
    module tests the GEMINI gate, and ``annotation_configured()`` dispatches to
    whatever backend the machine's real settings store selects.
    """
    from fyp.annotation.backends import settings as backend_settings

    machine = fyp_cf["machine"]["gemini"]
    data_io_cf = fyp_cf["data_io"]
    saved = (
        machine.get("vertexai"),
        machine.get("project"),
        machine.get("key"),
        data_io_cf.get("use_gcs_for_media"),
        gc._FALLBACK_WARNED,
    )
    saved_load_settings = backend_settings._load_settings
    backend_settings._load_settings = lambda: {"annotation_backend": "gemini"}
    machine["vertexai"] = vertexai
    machine["project"] = project
    machine["key"] = key
    data_io_cf["use_gcs_for_media"] = use_gcs_for_media
    try:
        yield
    finally:
        (
            machine["vertexai"],
            machine["project"],
            machine["key"],
            data_io_cf["use_gcs_for_media"],
            gc._FALLBACK_WARNED,
        ) = saved
        backend_settings._load_settings = saved_load_settings


def test_vertex_with_project_is_vertex_mode() -> None:
    with _machine(vertexai=True, project="some-proj", key=""):
        assert gc.gemini_mode() == (gc.MODE_VERTEX, "")


def test_api_key_mode_when_vertex_disabled() -> None:
    with _machine(vertexai=False, project="", key="k"):
        assert gc.gemini_mode() == (gc.MODE_API_KEY, "")


def test_vertex_without_project_falls_back_to_key() -> None:
    # The trap this whole module exists to close: GEMINI_API_KEY exported
    # against the default vertexai = true and no project.
    with _machine(vertexai=True, project="", key="k"):
        mode, reason = gc.gemini_mode()
        assert mode == gc.MODE_API_KEY, mode
        assert reason == ""


def test_vertex_without_project_or_key_is_unconfigured() -> None:
    with _machine(vertexai=True, project="", key=""):
        mode, reason = gc.gemini_mode()
        assert mode is None
        assert "no GCP project is set" in reason


def test_api_mode_without_key_is_unconfigured() -> None:
    with _machine(vertexai=False, project="", key=""):
        mode, reason = gc.gemini_mode()
        assert mode is None
        assert "GEMINI_API_KEY" in reason


def test_vertex_project_wins_over_a_stray_key() -> None:
    # A key sitting in the environment must not divert a working Vertex setup.
    with _machine(vertexai=True, project="some-proj", key="k"):
        assert gc.gemini_mode()[0] == gc.MODE_VERTEX


def test_make_client_vertex_shape() -> None:
    with _machine(vertexai=True, project="some-proj", key=""):
        client = gc.make_client()
        assert client._api_client.vertexai is True


def test_make_client_api_key_shape() -> None:
    with _machine(vertexai=False, project="", key="k"):
        client = gc.make_client()
        assert client._api_client.vertexai is False


def test_make_client_api_key_drops_stale_project() -> None:
    # Switching Vertex -> API key without clearing [machine].project must not
    # raise "Project/location and API key are mutually exclusive".
    with _machine(vertexai=False, project="leftover-proj", key="k"):
        client = gc.make_client(location="us-central1")
        assert client._api_client.vertexai is False


def test_api_key_client_drops_the_vertex_api_version() -> None:
    # Regression (caught only by a live call): [machine].http_options_api_version
    # is "v1", a *Vertex* API version. Forcing it on the plain Gemini API makes
    # every annotation fail with 400 "Unknown name systemInstruction" — that
    # surface serves system instructions / response schema / thinking config on
    # v1beta. The SDK default must win instead.
    from google.genai.types import HttpOptions

    with _machine(vertexai=False, project="", key="k"):
        client = gc.make_client(http_options=HttpOptions(api_version="v1", timeout=180000))
        assert client._api_client._http_options.api_version != "v1"
        # The rest of the caller's options must survive the strip.
        assert client._api_client._http_options.timeout == 180000


def test_vertex_client_keeps_the_configured_api_version() -> None:
    from google.genai.types import HttpOptions

    with _machine(vertexai=True, project="some-proj", key=""):
        client = gc.make_client(http_options=HttpOptions(api_version="v1", timeout=180000))
        assert client._api_client._http_options.api_version == "v1"


def test_strip_api_version_passes_none_through() -> None:
    assert gc._strip_api_version(None) is None


def test_make_client_raises_when_unconfigured() -> None:
    with _machine(vertexai=True, project="", key=""):
        try:
            gc.make_client()
        except gc.GeminiNotConfiguredError as exc:
            assert "not configured" in str(exc)
        else:
            raise AssertionError("make_client should raise when unconfigured")


def test_gate_agrees_with_mode_resolution() -> None:
    # The gate must never approve a config that has no usable mode, nor refuse
    # one that has — that divergence is what makes a worker fail every item.
    combos = [
        (True, "some-proj", ""),
        (True, "", "k"),
        (True, "", ""),
        (False, "", "k"),
        (False, "", ""),
    ]
    for vertexai, project, key in combos:
        with _machine(vertexai=vertexai, project=project, key=key):
            mode, _ = gc.gemini_mode()
            ok, _ = annotation_configured()
            assert ok == (mode is not None), (vertexai, project, key, mode, ok)


def test_gate_refuses_api_key_with_gcs_media() -> None:
    # gs:// URIs are Vertex-only; the plain API only sees inlined local bytes.
    with _machine(vertexai=False, project="", key="k", use_gcs_for_media=True):
        assert gc.gemini_mode()[0] == gc.MODE_API_KEY
        ok, reason = annotation_configured()
        assert ok is False
        assert "gs://" in reason


def test_gate_allows_vertex_with_gcs_media() -> None:
    with _machine(vertexai=True, project="some-proj", key="", use_gcs_for_media=True):
        assert annotation_configured() == (True, "")


def test_gate_allows_api_key_with_local_media() -> None:
    with _machine(vertexai=False, project="", key="k", use_gcs_for_media=False):
        assert annotation_configured() == (True, "")


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception:
            failures += 1
            import traceback

            print(f"ERROR {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
