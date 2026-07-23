"""Hosted Qwen omni annotation backend (Alibaba Model Studio / DashScope).

Productizes the validated API pilot from ``scripts/adhoc/api_backend_smoke.py``
/ ``scripts/adhoc/qwen_eval/06_annotate_qwen_api.py`` (2026-07-21, 20/20 valid;
enum agreement 0.83 vs the Gemini reference — above both local backends):

* The whole mp4 is passed base64 as a ``video_url`` data URL to DashScope's
  OpenAI-compatible endpoint (international/Singapore region by default) — the
  omni models consume native video including its audio track, so there is no
  frame/audio sampling on our side.
* Omni models only support streaming responses; the SSE stream is aggregated
  server-side style into one text.
* Output is enforced with ``response_format json_object`` plus the JSON schema
  embedded in the prompt (DashScope's json_object mode takes no schema). The
  models habitually append a stray closing code fence, which is stripped before
  the ``structured`` flatten path parses the object.
* Account-level rate limits (2026-07: 60 RPM / 100k tokens-per-minute for the
  omni models) — not thread count — bound throughput, so ``max_workers`` stays
  small and 429s are retried with backoff inside ``annotate_one``.

Config lives in ``[machine.qwen_api]``; the API key comes from the
``DASHSCOPE_API_KEY`` environment variable (same pattern as ``GEMINI_API_KEY``).
The schema-adherence prompt suffix is part of the version identity by design.
"""

import base64
import datetime as _dt
import json
import os
import re
import time

import requests

from fyp.annotation.backends.base import AnnotationBackend, BackendAvailability
from fyp.annotation.backends.qwen_local import _default_platform, _fetch_media
from fyp.fyp_config import get_config
from fyp.logging_setup import get_logger

logger = get_logger(__name__)

API_KEY_ENV = "DASHSCOPE_API_KEY"

# Retryable outcomes: rate limit, server-side errors, and the occasional
# unparseable response (pilot: ~1/20 items, clean on retry).
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)






def _api_cf() -> dict:
    """The ``[machine.qwen_api]`` config block with pilot-tuned defaults."""
    stored = get_config()["machine"].get("qwen_api", {}) or {}
    defaults = {
        "model_id": "qwen3.5-omni-flash",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "temperature": 0.0,
        "max_tokens": 8000,
        "max_workers": 4,
        "max_attempts": 5,
        "request_timeout": 600,
        "max_video_mb": 90,
    }
    return {**defaults, **stored}






def _schema_suffix(schema: dict) -> str:
    """The schema-adherence prompt addendum for one response schema."""
    return (
        "\n\nRespond with ONLY a single JSON object (no markdown fences, no "
        "prose) that conforms exactly to this JSON schema:\n"
        f"{json.dumps(schema)}"
    )






def _strip_fences(text: str) -> str:
    """Remove markdown code fences the omni models sometimes wrap around JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    return re.sub(r"\s*```$", "", text)






class QwenApiBackend(AnnotationBackend):
    """Qwen omni models via the DashScope OpenAI-compatible API."""

    name = "qwen_api"
    max_workers = 4
    supports_batch_mode = False
    cloud_run_capable = True


    def __init__(self, overrides: dict | None = None, selection: str | None = None):
        super().__init__(overrides=overrides, selection=selection)
        self.max_workers = int(self._effective_cf()["max_workers"])


    def _effective_cf(self) -> dict:
        """The ``[machine.qwen_api]`` config with variant overrides applied."""
        return {**_api_cf(), **self.overrides}


    def availability(self, deep: bool = False) -> BackendAvailability:
        """API-key (and optionally live-endpoint) readiness.

        Args:
            deep: When True, additionally list models on the configured
                endpoint to prove the key/region/network.

        Returns:
            The availability result with per-check detail rows.
        """
        checks: list[dict] = []
        key = os.environ.get(API_KEY_ENV, "")
        checks.append({
            "name": "api key", "ok": bool(key),
            "detail": f"{API_KEY_ENV} is set" if key else f"{API_KEY_ENV} is not set",
            "fix": "" if key else (
                f"Create a Model Studio API key (international region) and set "
                f"the {API_KEY_ENV} environment variable. "
                "See docs/installation.md#enabling-hosted-qwen-annotation."),
        })
        if not key:
            return BackendAvailability(
                ok=False,
                reason=(f"Hosted Qwen annotation is not configured: the "
                        f"{API_KEY_ENV} environment variable is not set. "
                        "See docs/installation.md#enabling-hosted-qwen-annotation."),
                checks=checks)

        if deep:
            ping = self._ping(key)
            checks.append(ping)
            if not ping["ok"]:
                return BackendAvailability(ok=False, reason=ping["detail"], checks=checks)

        return BackendAvailability(ok=True, reason="", checks=checks)


    def _ping(self, key: str) -> dict:
        """List models on the configured endpoint; return a check row."""
        api_cf = self._effective_cf()
        model_id = api_cf["model_id"]
        try:
            r = requests.get(f"{api_cf['base_url']}/models",
                             headers={"Authorization": f"Bearer {key}"}, timeout=30)
        except requests.RequestException as exc:
            return {"name": "api ping", "ok": False,
                    "detail": f"endpoint unreachable: {exc}",
                    "fix": "Check network access to DashScope."}
        if r.status_code != 200:
            return {"name": "api ping", "ok": False,
                    "detail": f"models list failed: HTTP {r.status_code}: {r.text[:200]}",
                    "fix": "Check the API key and its region (international vs Beijing)."}
        ids = {m.get("id") for m in (r.json().get("data") or [])}
        if model_id not in ids:
            return {"name": "api ping", "ok": False,
                    "detail": f"model {model_id!r} not offered on this endpoint",
                    "fix": "Check [machine.qwen_api].model_id against the Model Studio catalog."}
        return {"name": "api ping", "ok": True, "detail": f"{model_id} available", "fix": ""}


    def prompt_suffix(self) -> str:
        """The schema-adherence addendum for the active contract's schema.

        Part of the version identity: it changes with the response schema,
        which is output-affecting for json_object mode (the schema rides in
        the prompt, not in the request's response_format).
        """
        from fyp.annotation_schema import get_annotation_json_schema

        return _schema_suffix(get_annotation_json_schema())


    def effective_model_id(self) -> str:
        """The configured hosted model id."""
        return self._effective_cf()["model_id"]


    def version_gen_params(self) -> dict:
        """The standard generation params as this backend runs them."""
        api_cf = self._effective_cf()
        return {
            "use_structured_output": True,
            "temperature": api_cf["temperature"],
            "thinking_budget": None,
            "media_resolution": None,
            "max_output_tokens": api_cf["max_tokens"],
        }


    def version_extra_params(self) -> dict:
        """Transport/format parameters (output-affecting → identity)."""
        return {
            "api": "dashscope",
            "transport": "video_base64",
            "response_format": "json_object",
        }


    def annotate_one(self, item_id: str, platform: str | None = None,
                     gen_overrides: dict | None = None,
                     prompt_text: str | None = None,
                     response_schema=None) -> dict:
        """Annotate one item via the hosted API; returns the raw-row dict.

        Args:
            item_id: The item to annotate.
            platform: The item's source platform (media resolution).
            gen_overrides: Optional overrides (``model_id`` / ``temperature`` /
                ``max_tokens``), used by the A/B eval harness.
            prompt_text: Optional explicit prompt (A/B arm); the schema
                suffix is appended either way. None = active prompt.
            response_schema: Optional portable JSON schema dict matching
                ``prompt_text``. None = the active contract's schema.

        Returns:
            The raw-row dict (failures in-band, DNF finish_reasons).
        """
        import fyp.annotation_versioning as annotation_versioning
        from fyp.annotation_schema import get_annotation_json_schema

        api_cf = {**self._effective_cf(),
                  **{k: v for k, v in (gen_overrides or {}).items() if v is not None}}
        now = _dt.datetime.now()
        row: dict = {
            "item_id": item_id,
            "source_platform": platform or _default_platform(),
            "inference_ts": int(now.timestamp()),
            "inference_duration": -1,
            "model": api_cf["model_id"],
            "prompt_fn": annotation_versioning.active_prompt_label(),
            "annotation_version": annotation_versioning.current_annotation_version(),
            "structured": True,
            "usage": {},
            "error": "unknown error",
            "finish_reason": "did not even start",
            "response": "",
        }

        key = os.environ.get(API_KEY_ENV, "")
        if not key:
            row["error"] = f"{API_KEY_ENV} is not set"
            row["finish_reason"] = "DNF - not configured"
            return row

        if prompt_text is None:
            prompt_text = annotation_versioning.active_prompt_text()
        if response_schema is None:
            response_schema = get_annotation_json_schema()
        if not isinstance(response_schema, dict):
            row["error"] = "qwen_api needs a portable JSON-schema dict (got a non-dict schema)"
            row["finish_reason"] = "DNF - bad schema"
            return row
        full_prompt = prompt_text + _schema_suffix(response_schema)

        local_video, cleanup_video = None, None
        try:
            local_video, cleanup_video = _fetch_media(item_id, platform)
            if local_video is None:
                row["error"] = f"media not found for {platform or '?'}/{item_id}"
                row["finish_reason"] = "DNF - media not found"
                return row
            size_mb = os.path.getsize(local_video) / 1e6
            if size_mb > api_cf["max_video_mb"]:
                row["error"] = (f"video is {size_mb:.0f} MB, over the "
                                f"{api_cf['max_video_mb']} MB request limit")
                row["finish_reason"] = "DNF - media too large"
                return row

            video_b64 = base64.b64encode(open(local_video, "rb").read()).decode()
            start = _dt.datetime.now()
            text, usage, finish = self._call_with_retry(
                key, api_cf, full_prompt, f"data:video/mp4;base64,{video_b64}")
            row["inference_duration"] = (_dt.datetime.now() - start).total_seconds()

            row["response"] = _strip_fences(text)
            row["usage"] = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "candidates_tokens": usage.get("completion_tokens"),
                "thoughts_tokens": 0,
                "total_tokens": usage.get("total_tokens"),
            }
            parsed = json.loads(row["response"] or "null")
            if not isinstance(parsed, dict):
                raise ValueError("response is not a JSON object")
            row["error"] = ""
            row["finish_reason"] = finish or "STOP"
        except json.JSONDecodeError as exc:
            row["error"] = f"parse: {exc}"
            row["finish_reason"] = "DNF - unparseable response"
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["finish_reason"] = "DNF - see error"
        finally:
            if cleanup_video:
                cleanup_video()
        return row


    def _call_with_retry(self, key: str, api_cf: dict, prompt: str,
                         video_url: str) -> tuple[str, dict, str]:
        """One annotation call with backoff on retryable failures.

        Returns:
            ``(text, usage, finish_reason)`` from the first successful,
            non-empty attempt.

        Raises:
            RuntimeError: When all attempts fail (message carries the last
                HTTP status/body).
        """
        payload = {
            "model": api_cf["model_id"],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": video_url}},
                    {"type": "text", "text": prompt},
                ],
            }],
            "modalities": ["text"],
            "temperature": api_cf["temperature"],
            "max_tokens": api_cf["max_tokens"],
            "response_format": {"type": "json_object"},
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        last_error = "no attempts made"
        attempts = int(api_cf["max_attempts"])
        for attempt in range(attempts):
            if attempt:
                # 429s are the account-level RPM/TPM window — wait it out.
                delay = min(60.0, 5.0 * (2 ** (attempt - 1)))
                logger.info(f"qwen_api retry {attempt}/{attempts - 1} in {delay:.0f}s "
                            f"({last_error[:120]})")
                time.sleep(delay)
            try:
                r = requests.post(
                    f"{api_cf['base_url']}/chat/completions",
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    json=payload, timeout=api_cf["request_timeout"], stream=True)
            except requests.RequestException as exc:
                last_error = f"request failed: {exc}"
                continue
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}: {r.text[:500]}"
                if r.status_code in _RETRYABLE_STATUS:
                    continue
                raise RuntimeError(last_error)
            text, usage, finish = self._read_stream(r)
            if text.strip():
                return text, usage, finish
            last_error = "empty streamed response"
        raise RuntimeError(f"all {attempts} attempts failed; last: {last_error}")


    @staticmethod
    def _read_stream(r) -> tuple[str, dict, str]:
        """Aggregate one SSE chat-completions stream.

        Returns:
            ``(content, usage, finish_reason)``.
        """
        content: list[str] = []
        usage: dict = {}
        finish = ""
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        return "".join(content), usage, finish
