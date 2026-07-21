"""Hosted Qwen (DashScope) backend: config, availability, parsing, retry."""

import json

import pytest

import fyp.annotation.backends.qwen_api as qwen_api
from fyp.annotation.backends.qwen_api import QwenApiBackend, _schema_suffix, _strip_fences






def test_defaults_and_config_overlay(monkeypatch):
    cf = qwen_api._api_cf()
    assert cf["model_id"] == "qwen3.5-omni-flash"
    assert cf["base_url"].startswith("https://dashscope-intl.")
    assert cf["max_workers"] == 4






def test_availability_without_key(monkeypatch):
    monkeypatch.delenv(qwen_api.API_KEY_ENV, raising=False)
    result = QwenApiBackend().availability()
    assert result.ok is False
    assert qwen_api.API_KEY_ENV in result.reason
    assert result.checks and result.checks[0]["ok"] is False






def test_availability_with_key_shallow(monkeypatch):
    monkeypatch.setenv(qwen_api.API_KEY_ENV, "sk-test")
    result = QwenApiBackend().availability(deep=False)
    assert result.ok is True
    assert result.checks[0]["ok"] is True






def test_strip_fences_variants():
    obj = '{"a": 1}'
    assert _strip_fences(obj) == obj
    assert _strip_fences(f"```json\n{obj}\n```") == obj
    # The pilot-observed failure mode: a stray CLOSING fence only.
    assert _strip_fences(f"\n{obj}\n```") == obj






def test_schema_suffix_carries_schema():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    suffix = _schema_suffix(schema)
    assert json.dumps(schema) in suffix
    assert "JSON object" in suffix






def test_version_params_are_stable():
    backend = QwenApiBackend()
    extra = backend.version_extra_params()
    assert extra == {"api": "dashscope", "transport": "video_base64",
                     "response_format": "json_object"}
    gen = backend.version_gen_params()
    assert gen["use_structured_output"] is True
    assert gen["max_output_tokens"] == 8000






def test_annotate_one_without_key_is_in_band(monkeypatch):
    monkeypatch.delenv(qwen_api.API_KEY_ENV, raising=False)
    row = QwenApiBackend().annotate_one("123", platform="tiktok")
    assert row["error"]
    assert row["finish_reason"].startswith("DNF")
    assert row["item_id"] == "123"
    assert row["model"] == "qwen3.5-omni-flash"






class _FakeStream:
    """Minimal SSE response: chunks -> lines like requests' iter_lines."""

    status_code = 200

    def __init__(self, chunks):
        self._chunks = chunks

    def iter_lines(self, decode_unicode=True):
        for chunk in self._chunks:
            yield "data: " + json.dumps(chunk)
        yield "data: [DONE]"






def test_read_stream_aggregates_content_usage_finish():
    chunks = [
        {"choices": [{"delta": {"content": '{"a":'}}]},
        {"choices": [{"delta": {"content": " 1}"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                  "total_tokens": 15}},
    ]
    text, usage, finish = QwenApiBackend._read_stream(_FakeStream(chunks))
    assert text == '{"a": 1}'
    assert usage["total_tokens"] == 15
    assert finish == "stop"






def test_call_with_retry_backs_off_on_429(monkeypatch):
    monkeypatch.setattr(qwen_api.time, "sleep", lambda s: None)
    attempts = []

    class _R429:
        status_code = 429
        text = '{"error": "quota"}'

    ok_stream = _FakeStream([
        {"choices": [{"delta": {"content": '{"ok": true}'},
                      "finish_reason": "stop"}]},
    ])

    def fake_post(url, **kwargs):
        attempts.append(url)
        return _R429() if len(attempts) < 3 else ok_stream

    monkeypatch.setattr(qwen_api.requests, "post", fake_post)
    backend = QwenApiBackend()
    text, usage, finish = backend._call_with_retry(
        "sk-test", qwen_api._api_cf(), "prompt", "data:video/mp4;base64,AAAA")
    assert text == '{"ok": true}'
    assert len(attempts) == 3






def test_call_with_retry_raises_on_permanent_error(monkeypatch):
    class _R400:
        status_code = 400
        text = "bad request"

    monkeypatch.setattr(qwen_api.requests, "post", lambda url, **kw: _R400())
    with pytest.raises(RuntimeError, match="HTTP 400"):
        QwenApiBackend()._call_with_retry(
            "sk-test", qwen_api._api_cf(), "prompt", "data:video/mp4;base64,AAAA")
