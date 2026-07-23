"""Regression test: a successful call_machine() must not report an error.

The output dict used to be initialised with ``"error": "unknown error"`` and
the field was only ever overwritten inside the exception handlers — so every
*successful* annotation still carried ``error="unknown error"`` in the raw
output rows and temp JSONs (see tests/golden/fixtures/raw_sample.json for the
historical evidence). Nothing downstream reads the field (``annotated_ok``
derives from ``type_of_story``), but it misleads humans debugging raw rows and
traps any future code that checks ``if out.get("error")``.

No Gemini API call — a fake client is injected into the config (which makes
``initialize_machine`` a no-op) and the media is a throwaway local file.

Usage:
    python tests/unit/test_call_machine_success_error_field.py
Exit 0 iff all checks pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fyp.machine_annotation as ma
from fyp.fyp_config import fyp_cf


class _FakeResponse:
    """Minimal stand-in for a successful generate_content response."""

    class _Candidate:
        finish_reason = "FinishReason.STOP"

    candidates = [_Candidate()]
    text = '{"type_of_story": "test"}'
    usage_metadata = None


class _FakeModels:
    def generate_content(self, *, model, config, contents):
        return _FakeResponse()


class _FakeClient:
    models = _FakeModels()


def test_successful_call_reports_no_error(tmp_path) -> None:
    video_id = "err_field_test_item"
    (tmp_path / f"{video_id}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

    saved_client = fyp_cf["machine"]["gemini"].get("client")
    fyp_cf["machine"]["gemini"]["client"] = _FakeClient()
    try:
        out = ma.call_machine(
            video_id=video_id,
            use_local_video_file=True,
            local_path=str(tmp_path),
        )
    finally:
        fyp_cf["machine"]["gemini"]["client"] = saved_client

    assert out["finish_reason"] == "FinishReason.STOP"
    assert out["response"] == _FakeResponse.text
    assert not out["error"], (
        f"successful call must return a falsy error, got {out['error']!r}"
    )


def _main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        try:
            test_successful_call_reports_no_error(Path(td))
            print("PASS  test_successful_call_reports_no_error")
            return 0
        except AssertionError as exc:
            print(f"FAIL  test_successful_call_reports_no_error: {exc}")
            return 1


if __name__ == "__main__":
    raise SystemExit(_main())
