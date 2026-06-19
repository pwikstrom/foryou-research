"""Unit tests for the ``use_generated_prompt`` flag wiring (Workstream E, E2).

Pins ``annotation_versioning.active_prompt_text`` / ``active_prompt_label`` and
the version descriptor's flag sensitivity:

  * flag OFF (default): the prompt is read from the configured file and the
    ``prompt_fn`` label is that file's basename — zero behaviour change.
  * flag ON: the prompt is generated from the declarative contract
    (``annotation_schema.build_prompt``) and labelled ``annotation_contract.toml``.
  * flipping the flag changes the ``annotation_version`` (the prompt text feeds the
    hash); flipping back restores the original version.

No Gemini API calls.

Usage:
    python tests/unit/test_generated_prompt_flag.py
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fyp.annotation_schema as schema
import fyp.annotation_versioning as av
from fyp.fyp_config import fyp_cf

_MISSING = object()


@contextlib.contextmanager
def _flag(value):
    machine = fyp_cf["machine"]
    saved = machine.get("use_generated_prompt", _MISSING)
    machine["use_generated_prompt"] = value
    try:
        yield
    finally:
        if saved is _MISSING:
            machine.pop("use_generated_prompt", None)
        else:
            machine["use_generated_prompt"] = saved
        av.current_annotation_version(fresh=True)


def test_flag_off_reads_file() -> None:
    with _flag(False):
        on_disk = open(fyp_cf["machine"]["prompt"]).read()
        assert av.active_prompt_text() == on_disk
        assert av.active_prompt_label() == os.path.basename(fyp_cf["machine"]["prompt"])


def test_flag_on_uses_generated_contract() -> None:
    with _flag(True):
        assert av.active_prompt_text() == schema.build_prompt()
        assert av.active_prompt_label() == "annotation_contract.toml"


def test_flag_changes_annotation_version() -> None:
    with _flag(False):
        off_version = av.current_annotation_version(fresh=True)
    with _flag(True):
        on_version = av.current_annotation_version(fresh=True)
    assert off_version != on_version, "flipping use_generated_prompt must change the version"


def test_flag_roundtrip_restores_version() -> None:
    with _flag(False):
        before = av.current_annotation_version(fresh=True)
    with _flag(True):
        av.current_annotation_version(fresh=True)
    with _flag(False):
        after = av.current_annotation_version(fresh=True)
    assert before == after, "flag off version must be stable across a flip round-trip"


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
