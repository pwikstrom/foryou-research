"""Live structured-output annotator (Phase 2 spike).

Calls Gemini with a constrained ``response_schema`` (from
``fyp.annotation_schema``) instead of free-text JSON. Reuses the EXISTING prompt
as the system instruction and the existing client/config plumbing, so the only
things that change versus production are:

  * ``response_schema`` is attached (decoding is constrained -> always valid JSON)
  * the repetition penalties are off by default (constrained + thinking models do
    not loop the way free-text decoding can)
  * token usage is captured (prompt / candidates / thoughts / total)

This is on-demand spike code — it is NOT wired into the production queue. Use it
from the A/B harness or the smoke script. Each call costs money.

Returns a dict per video with the parsed structured response, the raw text, the
finish reason, token usage, and timing.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import google.genai.types as gt

import fyp.annotation_versioning as annotation_versioning
from fyp.annotation_schema import build_response_schema
from fyp.fyp_config import fyp_cf
from fyp.machine_annotation import initialize_machine

_STRUCTURED_CONFIG: gt.GenerateContentConfig | None = None


def _resolve_media_resolution(level: str | None):
    """Map a level name ('LOW' / 'MEDIA_RESOLUTION_LOW') to a genai enum, or None."""
    if not level:
        return None
    name = str(level).strip().upper()
    if not name.startswith("MEDIA_RESOLUTION_"):
        name = f"MEDIA_RESOLUTION_{name}"
    return getattr(gt.MediaResolution, name, None)


def build_structured_config(
    use_penalties: bool = False,
    thinking_budget: int | None = None,
    media_resolution: str | None = None,
    temperature: float | None = None,
    prompt_override: str | None = None,
) -> gt.GenerateContentConfig:
    """Build the structured-output generation config.

    Reuses the existing prompt file as the system instruction and the existing
    temperature / token settings, but attaches the response schema and (by
    default) drops the repetition penalties.

    Args:
        use_penalties: keep the production presence/frequency penalties.
        thinking_budget: override the config thinking budget (e.g. cap it to
            leave room for the structured output so it cannot truncate
            mid-JSON). ``None`` uses the config value (``-1`` = dynamic).
        media_resolution: override the video frame resolution ('LOW' / 'MEDIUM'
            / 'HIGH'). ``None`` leaves it at the API default. Used by the
            media_resolution A/B harness.

    Returns:
        A configured ``GenerateContentConfig`` with a response schema.
    """
    global _STRUCTURED_CONFIG
    is_default = (
        not use_penalties and thinking_budget is None
        and media_resolution is None and temperature is None
        and prompt_override is None
    )
    if _STRUCTURED_CONFIG is not None and is_default:
        return _STRUCTURED_CONFIG

    # An explicit prompt_override wins (used by the prompt A/B). Otherwise honors
    # [machine] use_generated_prompt: file prompt by default, generated from the
    # contract when the flag is on.
    machine_prompt = (
        prompt_override if prompt_override is not None
        else annotation_versioning.active_prompt_text()
    )

    budget = thinking_budget if thinking_budget is not None else fyp_cf["machine"]["thinking_budget"]
    temp = temperature if temperature is not None else fyp_cf["machine"]["temperature"]
    config = gt.GenerateContentConfig(
        system_instruction=machine_prompt,
        temperature=temp,
        max_output_tokens=fyp_cf["machine"]["max_output_tokens"],
        response_mime_type="application/json",
        response_schema=build_response_schema(),
        presence_penalty=fyp_cf["machine"]["presence_penalty"] if use_penalties else None,
        frequency_penalty=fyp_cf["machine"]["frequency_penalty"] if use_penalties else None,
        media_resolution=_resolve_media_resolution(media_resolution),
        thinking_config=gt.ThinkingConfig(thinking_budget=budget),
    )
    if is_default:
        _STRUCTURED_CONFIG = config
    return config


def _build_contents(video_id: str, use_local_video_file: bool, local_path: str | None) -> list:
    """Build the model ``contents`` (local bytes or GCS URI), mirroring call_machine."""
    effective_local = use_local_video_file or not fyp_cf["data_io"]["use_gcs_for_media"]
    effective_local_dir = local_path or fyp_cf["paths"]["media"]
    if effective_local:
        with open(os.path.join(effective_local_dir, f"{video_id}.mp4"), "rb") as f:
            video_bytes = f.read()
        return [
            gt.Part(inline_data=gt.Blob(data=video_bytes, mime_type="video/mp4")),
            gt.Part.from_text(text="Analyze this video"),
        ]
    return [
        gt.Part.from_uri(
            file_uri=(
                f"gs://{fyp_cf['data_io']['GCS_bucket_name']}/"
                f"{fyp_cf['data_io']['gcs_media_prefix']}/{video_id}.mp4"
            ),
            mime_type="video/mp4",
        ),
        gt.Part.from_text(text="Analyze this video"),
    ]


def annotate_structured(
    video_id: str,
    use_local_video_file: bool = False,
    local_path: str | None = None,
    use_penalties: bool = False,
    thinking_budget: int | None = None,
    media_resolution: str | None = None,
    temperature: float | None = None,
    prompt_override: str | None = None,
    verbose: bool = False,
) -> dict:
    """Annotate one video with constrained structured output.

    Args:
        video_id: TikTok item id (the ``{id}.mp4`` basename).
        use_local_video_file: force reading the local mp4 instead of GCS.
        local_path: override the local media directory.
        use_penalties: keep the production repetition penalties.
        verbose: print progress.

    Returns:
        A dict with ``item_id``, ``parsed`` (dict or None), ``response`` (raw
        text), ``finish_reason``, ``usage`` (token counts), ``inference_
        duration``, ``model`` and ``error``.
    """
    initialize_machine()
    config = build_structured_config(
        use_penalties=use_penalties,
        thinking_budget=thinking_budget,
        media_resolution=media_resolution,
        temperature=temperature,
        prompt_override=prompt_override,
    )

    out: dict = {
        "item_id": video_id,
        "model": fyp_cf["machine"]["model"],
        "parsed": None,
        "response": "",
        "finish_reason": "did not start",
        "usage": {},
        "inference_duration": -1.0,
        "error": "",
    }

    try:
        contents = _build_contents(video_id, use_local_video_file, local_path)
    except Exception as exc:
        out["error"] = f"contents: {exc}"
        out["finish_reason"] = "DNF - video not found"
        return out

    start = _dt.datetime.now()
    try:
        resp = fyp_cf["machine"]["client"].models.generate_content(
            model=fyp_cf["machine"]["model"],
            config=config,
            contents=contents,
        )
    except Exception as exc:
        out["error"] = f"generate: {exc}"
        out["finish_reason"] = "DNF - see error"
        out["inference_duration"] = (_dt.datetime.now() - start).total_seconds()
        return out

    out["inference_duration"] = (_dt.datetime.now() - start).total_seconds()
    try:
        out["finish_reason"] = str(resp.candidates[0].finish_reason)
    except (IndexError, AttributeError):
        out["finish_reason"] = "unknown"

    usage = getattr(resp, "usage_metadata", None)
    if usage is not None:
        out["usage"] = {
            "prompt_tokens": getattr(usage, "prompt_token_count", None),
            "candidates_tokens": getattr(usage, "candidates_token_count", None),
            "thoughts_tokens": getattr(usage, "thoughts_token_count", None),
            "total_tokens": getattr(usage, "total_token_count", None),
        }

    try:
        out["response"] = resp.text or ""
        out["parsed"] = json.loads(out["response"]) if out["response"] else None
    except Exception as exc:
        out["error"] = f"parse: {exc}"

    if verbose:
        toks = out["usage"].get("total_tokens")
        print(
            f"  {video_id}: {out['finish_reason']} "
            f"({out['inference_duration']:.1f}s, {toks} tok, "
            f"parsed={'ok' if isinstance(out['parsed'], dict) else 'FAIL'})"
        )

    return out
