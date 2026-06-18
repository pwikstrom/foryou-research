"""Unit tests for the transient-failure retry policy in machine_annotation.

Pins the behaviour of ``_is_transient_error`` and ``_generate_with_retry``
(``fyp/machine_annotation.py``): transient errors (rate limits, 5xx,
deadline/timeout, dropped connections) are retried with bounded exponential
backoff; every other error fails fast on the first occurrence.

No Gemini API calls — a scripted fake client stands in for the SDK and
``time.sleep`` is replaced with a recorder so the suite is instant and free.

Usage:
    python tests/unit/test_call_machine_retry.py
Exit 0 iff all checks pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fyp.machine_annotation as ma
from fyp.fyp_config import fyp_cf


class _ApiError(Exception):
    """Stand-in for a google-genai error carrying an HTTP ``code``."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class _ScriptedModels:
    """Fake ``client.models`` whose generate_content follows a fixed script."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate_content(self, *, model, config, contents):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _ScriptedClient:
    def __init__(self, outcomes: list) -> None:
        self.models = _ScriptedModels(outcomes)


class _Harness:
    """Install a scripted client + recorded sleeps, restore config on exit."""

    _KEYS = ("client", "model", "max_retries", "retry_base_delay")

    def __init__(self, outcomes: list, max_retries: int = 2, base_delay: float = 2.0) -> None:
        self.outcomes = outcomes
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.sleeps: list[float] = []

    def __enter__(self) -> "_Harness":
        machine = fyp_cf["machine"]
        self._saved = {k: machine.get(k, _MISSING) for k in self._KEYS}
        self._saved_sleep = ma.time.sleep
        self.client = _ScriptedClient(self.outcomes)
        machine["client"] = self.client
        machine["model"] = "test-model"
        machine["max_retries"] = self.max_retries
        machine["retry_base_delay"] = self.base_delay
        ma.time.sleep = lambda seconds: self.sleeps.append(seconds)
        return self

    def __exit__(self, *exc_info) -> bool:
        machine = fyp_cf["machine"]
        for key, value in self._saved.items():
            if value is _MISSING:
                machine.pop(key, None)
            else:
                machine[key] = value
        ma.time.sleep = self._saved_sleep
        return False

    @property
    def calls(self) -> int:
        return self.client.models.calls


_MISSING = object()


# ---------------------------------------------------------------------------
# _is_transient_error
# ---------------------------------------------------------------------------

def test_is_transient_by_status_code() -> None:
    assert ma._is_transient_error(_ApiError("boom", code=503)) is True
    assert ma._is_transient_error(_ApiError("boom", code=429)) is True
    assert ma._is_transient_error(_ApiError("boom", code=500)) is True
    assert ma._is_transient_error(_ApiError("bad", code=400)) is False
    assert ma._is_transient_error(_ApiError("missing", code=404)) is False


def test_is_transient_by_message() -> None:
    assert ma._is_transient_error(Exception("504 DEADLINE_EXCEEDED")) is True
    assert ma._is_transient_error(Exception("503 Service Unavailable")) is True
    assert ma._is_transient_error(Exception("RESOURCE_EXHAUSTED: quota")) is True
    assert ma._is_transient_error(Exception("429 Too Many Requests")) is True
    assert ma._is_transient_error(Exception("INVALID_ARGUMENT: bad request")) is False
    assert ma._is_transient_error(Exception("PERMISSION_DENIED")) is False
    assert ma._is_transient_error(Exception("NOT_FOUND")) is False


def test_is_transient_builtin_types() -> None:
    assert ma._is_transient_error(TimeoutError()) is True
    assert ma._is_transient_error(ConnectionError("reset")) is True
    assert ma._is_transient_error(ValueError("nope")) is False


# ---------------------------------------------------------------------------
# _generate_with_retry
# ---------------------------------------------------------------------------

def test_retry_succeeds_after_transient() -> None:
    err = _ApiError("503 unavailable", code=503)
    with _Harness([err, err, "OK"], max_retries=2) as h:
        result = ma._generate_with_retry(["c"], "cfg")
    assert result == "OK"
    assert h.calls == 3
    assert len(h.sleeps) == 2


def test_retry_exhausted_raises() -> None:
    err = _ApiError("503 unavailable", code=503)
    raised = None
    with _Harness([err], max_retries=2) as h:
        try:
            ma._generate_with_retry(["c"], "cfg")
        except Exception as exc:  # noqa: BLE001 — we assert the type below
            raised = exc
    assert isinstance(raised, _ApiError)
    assert h.calls == 3          # 1 initial attempt + 2 retries
    assert len(h.sleeps) == 2


def test_no_retry_on_permanent_error() -> None:
    err = _ApiError("INVALID_ARGUMENT", code=400)
    raised = None
    with _Harness([err, "OK"], max_retries=2) as h:
        try:
            ma._generate_with_retry(["c"], "cfg")
        except Exception as exc:  # noqa: BLE001
            raised = exc
    assert isinstance(raised, _ApiError)
    assert h.calls == 1
    assert len(h.sleeps) == 0


def test_max_retries_zero_disables_retry() -> None:
    err = _ApiError("503 unavailable", code=503)
    raised = None
    with _Harness([err, "OK"], max_retries=0) as h:
        try:
            ma._generate_with_retry(["c"], "cfg")
        except Exception as exc:  # noqa: BLE001
            raised = exc
    assert isinstance(raised, _ApiError)
    assert h.calls == 1
    assert len(h.sleeps) == 0


def test_backoff_grows_exponentially() -> None:
    err = _ApiError("503", code=503)
    with _Harness([err, err, "OK"], max_retries=2, base_delay=2.0) as h:
        ma._generate_with_retry(["c"], "cfg")
    # delay = base * 2**attempt + jitter(in [0,1)): [2,3) then [4,5)
    assert 2.0 <= h.sleeps[0] < 3.0
    assert 4.0 <= h.sleeps[1] < 5.0


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
