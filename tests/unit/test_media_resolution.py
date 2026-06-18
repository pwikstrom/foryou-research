"""Unit tests for the media_resolution config lever in machine_annotation.

Pins ``_resolve_media_resolution``: empty/unset -> None (API default, i.e.
unchanged behaviour), level names map to the genai ``MediaResolution`` enum
(case-insensitive, bare or full name), and unknown values degrade to None.
No Gemini API calls.

Usage:
    python tests/unit/test_media_resolution.py
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import google.genai

import fyp.machine_annotation as ma
from fyp.fyp_config import fyp_cf

_MISSING = object()
_MR = google.genai.types.MediaResolution


@contextlib.contextmanager
def _set(value):
    machine = fyp_cf["machine"]
    saved = machine.get("media_resolution", _MISSING)
    if value is _MISSING:
        machine.pop("media_resolution", None)
    else:
        machine["media_resolution"] = value
    try:
        yield
    finally:
        if saved is _MISSING:
            machine.pop("media_resolution", None)
        else:
            machine["media_resolution"] = saved


def test_empty_or_unset_is_none() -> None:
    with _set(""):
        assert ma._resolve_media_resolution() is None
    with _set(_MISSING):
        assert ma._resolve_media_resolution() is None


def test_bare_level_maps_to_enum() -> None:
    with _set("LOW"):
        assert ma._resolve_media_resolution() == _MR.MEDIA_RESOLUTION_LOW
    with _set("MEDIUM"):
        assert ma._resolve_media_resolution() == _MR.MEDIA_RESOLUTION_MEDIUM


def test_full_enum_name_accepted() -> None:
    with _set("MEDIA_RESOLUTION_HIGH"):
        assert ma._resolve_media_resolution() == _MR.MEDIA_RESOLUTION_HIGH


def test_case_insensitive() -> None:
    with _set("low"):
        assert ma._resolve_media_resolution() == _MR.MEDIA_RESOLUTION_LOW


def test_unknown_value_is_none() -> None:
    with _set("banana"):
        assert ma._resolve_media_resolution() is None


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
