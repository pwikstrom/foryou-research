#!/usr/bin/env python3
"""Gemini Batch API path for annotation (async, ~50% cheaper).

A separate submit -> poll path from the synchronous queue annotator. It reuses
the existing response schema, prompt, and — crucially — the marker-driven
refinement pipeline: batch output is written into ``machine_annotations_raw`` in
the EXACT shape :func:`fyp.machine_annotation.call_machine` produces (``item_id``,
``structured=True``, ``annotation_version``, ``usage``, ``finish_reason``,
``response``), so ``refine_one_raw_annotation_batch`` handles it unchanged.

Constraints:
  * Batch requires GCS (or BigQuery) URIs for video parts — no inline bytes —
    and a GCS input/output location. It therefore only runs where
    ``data_io.use_gcs_for_media`` is true and a bucket is configured.
  * Turnaround is async (minutes to ~24h), so this is for bulk / non-urgent
    annotation; the synchronous path remains the urgent fallback.

The pure builders (:func:`build_request_dict`, :func:`ingest_output_record`)
encode the JSONL request contract and the output->raw-shape mapping and are unit
tested offline. The thin GCS / batch-API wrappers are SPIKE-GATED: the exact
JSONL key casing and output record shape for ``gemini-3-flash-preview`` batch
must be confirmed by a small live job before a large run (see the runbook in
``run_queue_annotator_batch.py``).
"""

import datetime as _dt
import json
import os

import google.genai

import fyp.annotation_versioning as annotation_versioning
import fyp.data_io as data_io
from fyp.annotation_schema import build_response_schema
from fyp.fyp_config import fyp_cf
from fyp.machine_annotation import MACHINE_ANNOTATIONS_LABEL, initialize_machine

# Prefixes (relative to the GCS data prefix) for batch input/output.
BATCH_INPUT_PREFIX = "machine_annotations_batch_input"
BATCH_OUTPUT_PREFIX = "machine_annotations_batch_output"

# Terminal batch-job states.
_TERMINAL_OK = "JOB_STATE_SUCCEEDED"
_TERMINAL_PARTIAL = "JOB_STATE_PARTIALLY_SUCCEEDED"
_TERMINAL_FAIL = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}




def current_batch_gen_params() -> dict:
    """Return the output-affecting generation params from config for batch."""
    machine = fyp_cf["machine"]
    return {
        "temperature": machine.get("temperature"),
        "max_output_tokens": machine.get("max_output_tokens"),
        "thinking_budget": machine.get("thinking_budget"),
        "media_resolution": (str(machine.get("media_resolution", "") or "").strip().upper() or None),
    }




def build_request_dict(
    video_id: str,
    *,
    bucket: str,
    media_prefix: str,
    system_instruction: str,
    schema_json: dict,
    gen_params: dict,
) -> dict:
    """Build one JSONL batch-request line for a video (pure).

    Mirrors the synchronous request: a GCS video part + "Analyze this video",
    the prompt as system instruction, and a structured-output generation config.
    Uses the documented Vertex camelCase keys (``generationConfig``,
    ``responseSchema``, ``systemInstruction``, ``mediaResolution``) — confirm
    against a live spike before bulk use.

    Args:
        video_id: The TikTok item id (the ``<id>.mp4`` in GCS).
        bucket: GCS bucket name.
        media_prefix: GCS media prefix (e.g. ``"media"``).
        system_instruction: The prompt text.
        schema_json: The portable response-schema dict.
        gen_params: temperature / max_output_tokens / thinking_budget /
            media_resolution.

    Returns:
        A dict with a single ``"request"`` key, ready to JSON-serialise as one
        JSONL line.
    """
    generation_config: dict = {
        "temperature": gen_params.get("temperature"),
        "maxOutputTokens": gen_params.get("max_output_tokens"),
        "responseMimeType": "application/json",
        "responseSchema": schema_json,
        "thinkingConfig": {"thinkingBudget": gen_params.get("thinking_budget")},
    }
    media_resolution = gen_params.get("media_resolution")
    if media_resolution:
        if not media_resolution.startswith("MEDIA_RESOLUTION_"):
            media_resolution = f"MEDIA_RESOLUTION_{media_resolution}"
        generation_config["mediaResolution"] = media_resolution

    return {
        "request": {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "Analyze this video"},
                        {
                            "fileData": {
                                "fileUri": f"gs://{bucket}/{media_prefix}/{video_id}.mp4",
                                "mimeType": "video/mp4",
                            }
                        },
                    ],
                }
            ],
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "generationConfig": generation_config,
        }
    }




def _find_file_uri(obj) -> str | None:
    """Recursively find the first ``fileUri`` (camel or snake) in a structure."""
    if isinstance(obj, dict):
        for key in ("fileUri", "file_uri"):
            if isinstance(obj.get(key), str):
                return obj[key]
        for value in obj.values():
            found = _find_file_uri(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_file_uri(value)
            if found:
                return found
    return None




def item_id_from_uri(file_uri: str | None) -> str | None:
    """Extract the ``<id>`` from a ``gs://.../<id>.mp4`` URI."""
    if not file_uri:
        return None
    base = os.path.basename(file_uri)
    return base[:-4] if base.endswith(".mp4") else base




def _dig(obj, *path, default=None):
    """Safely walk a nested dict/list path, returning ``default`` on any miss."""
    cur = obj
    for key in path:
        if isinstance(key, int):
            if isinstance(cur, list) and -len(cur) <= key < len(cur):
                cur = cur[key]
            else:
                return default
        elif isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur




def ingest_output_record(
    record: dict,
    *,
    model: str,
    prompt_fn: str,
    annotation_version: str,
    inference_ts: int | None = None,
) -> dict:
    """Map one batch output record to the ``call_machine`` raw-output shape (pure).

    Handles both the success shape (a ``response`` with candidates + usage) and
    error/empty records (recorded as a DNF, mirroring the synchronous timeout
    DNF). The returned dict feeds the unchanged marker-driven refinement.

    Args:
        record: One parsed line from the batch output JSONL.
        model: The model id.
        prompt_fn: The prompt file basename.
        annotation_version: The version the batch was submitted under.
        inference_ts: Optional timestamp to stamp.

    Returns:
        A raw-output dict (``item_id`` may be ``None`` if unresolvable).
    """
    request = record.get("request", record)
    item_id = item_id_from_uri(_find_file_uri(request))

    out = {
        "item_id": item_id,
        "inference_ts": inference_ts,
        "inference_duration": -1,
        "model": model,
        "prompt_fn": prompt_fn,
        "annotation_version": annotation_version,
        "structured": True,
        "usage": {},
        "error": "",
        "finish_reason": "did not even start",
        "response": "",
    }

    # Error record (Vertex puts a `status` / `error` on failed lines).
    status = record.get("status") or record.get("error")
    response = record.get("response")
    if response is None and status is not None:
        out["error"] = json.dumps(status) if not isinstance(status, str) else status
        out["finish_reason"] = "DNF - batch error"
        return out
    if response is None:
        out["error"] = "no response in batch output record"
        out["finish_reason"] = "DNF - batch error"
        return out

    text = _dig(response, "candidates", 0, "content", "parts", 0, "text", default="")
    finish_reason = _dig(response, "candidates", 0, "finishReason", default=None) or \
        _dig(response, "candidates", 0, "finish_reason", default="STOP")
    usage = response.get("usageMetadata") or response.get("usage_metadata") or {}

    out["response"] = text or ""
    out["finish_reason"] = str(finish_reason)
    out["usage"] = {
        "prompt_tokens": usage.get("promptTokenCount") or usage.get("prompt_token_count"),
        "candidates_tokens": usage.get("candidatesTokenCount") or usage.get("candidates_token_count"),
        "thoughts_tokens": usage.get("thoughtsTokenCount") or usage.get("thoughts_token_count"),
        "total_tokens": usage.get("totalTokenCount") or usage.get("total_token_count"),
    }
    if not text:
        out["error"] = "empty candidate text"
        out["finish_reason"] = f"DNF - {out['finish_reason']}"
    return out




def ingest_records_to_raw(
    records: list,
    submitted_ids: list,
    *,
    model: str,
    prompt_fn: str,
    annotation_version: str,
) -> dict:
    """Map all output records to a raw dict keyed by index; DNF for missing ids.

    Any ``submitted_id`` absent from the output is synthesised as a DNF entry so
    the queue prune removes it (mirrors the synchronous worker-timeout DNF).

    Returns:
        ``{idx: raw_output_dict}`` shaped like a machine_annotations_raw file.
    """
    now_ts = int(_dt.datetime.now().timestamp())
    by_item: dict[str, dict] = {}
    for record in records:
        mapped = ingest_output_record(
            record, model=model, prompt_fn=prompt_fn,
            annotation_version=annotation_version, inference_ts=now_ts,
        )
        if mapped.get("item_id"):
            by_item[str(mapped["item_id"])] = mapped

    raw: dict[str, dict] = {}
    idx = 0
    for item_id in submitted_ids:
        sid = str(item_id)
        if sid in by_item:
            raw[str(idx)] = by_item[sid]
        else:
            raw[str(idx)] = {
                "item_id": sid,
                "inference_ts": now_ts,
                "inference_duration": -1,
                "model": model,
                "prompt_fn": prompt_fn,
                "annotation_version": annotation_version,
                "structured": True,
                "usage": {},
                "error": "missing from batch output",
                "finish_reason": "DNF - missing from batch output",
                "response": "",
            }
        idx += 1
    return raw




# ---------------------------------------------------------------------------
# Thin GCS / batch-API wrappers (SPIKE-GATED — confirm casing/shape live first).
# ---------------------------------------------------------------------------


def _gcs_bucket():
    """Return the configured GCS bucket client, or raise if unavailable."""
    bucket = fyp_cf["data_io"].get("bucket")
    if bucket is None:
        raise RuntimeError(
            "Batch annotation requires GCS, but no bucket is configured "
            "(set FYP_GCS_BUCKET_NAME and use_gcs_for_media=true)."
        )
    return bucket




def build_and_upload_jsonl(video_ids: list, ts_label: str) -> tuple[str, list]:
    """Build the batch JSONL and upload it to GCS. Returns (gcs_uri, submitted_ids)."""
    initialize_machine()
    bucket_name = fyp_cf["data_io"]["GCS_bucket_name"]
    media_prefix = fyp_cf["data_io"]["gcs_media_prefix"]
    data_prefix = fyp_cf["data_io"].get("gcs_data_prefix", "data")
    with open(fyp_cf["machine"]["prompt"]) as handle:
        system_instruction = handle.read()
    # Batch needs the genai PROTO schema (type:"STRING"/"OBJECT", propertyOrdering),
    # NOT the OpenAPI dict (type:"string") that get_annotation_json_schema emits:
    # the interactive endpoint tolerates the OpenAPI form, but the batch endpoint's
    # raw-JSON proto parser rejects it (confirmed by the live spike).
    schema_json = build_response_schema().model_dump(mode="json", by_alias=True, exclude_none=True)
    # Vertex's batch endpoint mis-converts 2-value string enums (e.g. ["Yes","No"])
    # into a boolean enum and then rejects its own output. Drop the enum constraint
    # on such fields for the BATCH request only — the prompt still asks for the value
    # and the recode pipeline tolerates free strings. (Live-spike-confirmed Vertex
    # quirk; the synchronous structured path keeps the enums.)
    for _prop in schema_json.get("properties", {}).values():
        if isinstance(_prop, dict) and isinstance(_prop.get("enum"), list) and len(_prop["enum"]) == 2:
            _prop.pop("enum", None)
    gen_params = current_batch_gen_params()

    lines = []
    submitted_ids = []
    for video_id in video_ids:
        lines.append(json.dumps(build_request_dict(
            video_id, bucket=bucket_name, media_prefix=media_prefix,
            system_instruction=system_instruction, schema_json=schema_json,
            gen_params=gen_params,
        )))
        submitted_ids.append(str(video_id))

    blob_path = f"{data_prefix}/{BATCH_INPUT_PREFIX}/{ts_label}.jsonl"
    _gcs_bucket().blob(blob_path).upload_from_string("\n".join(lines))
    return f"gs://{bucket_name}/{blob_path}", submitted_ids




def submit_batch_job(jsonl_uri: str, ts_label: str) -> tuple[str, str]:
    """Submit a batch prediction job. Returns (job_name, output_uri)."""
    initialize_machine()
    bucket_name = fyp_cf["data_io"]["GCS_bucket_name"]
    data_prefix = fyp_cf["data_io"].get("gcs_data_prefix", "data")
    output_uri = f"gs://{bucket_name}/{data_prefix}/{BATCH_OUTPUT_PREFIX}/{ts_label}/"
    job = fyp_cf["machine"]["client"].batches.create(
        model=fyp_cf["machine"]["model"],
        src=jsonl_uri,
        config=google.genai.types.CreateBatchJobConfig(dest=output_uri),
    )
    return job.name, output_uri




def poll_batch_job(job_name: str) -> str:
    """Return the current ``JobState`` name of a batch job."""
    initialize_machine()
    job = fyp_cf["machine"]["client"].batches.get(name=job_name)
    return str(getattr(job.state, "name", job.state))




def download_and_ingest(output_uri: str, submitted_ids: list) -> str:
    """Read batch output from GCS, map to the raw shape, and save a raw file.

    Returns the saved raw JSON filename (in ``machine_annotations_raw``).
    """
    bucket = _gcs_bucket()
    bucket_name = fyp_cf["data_io"]["GCS_bucket_name"]
    prefix = output_uri.replace(f"gs://{bucket_name}/", "").rstrip("/")
    records = []
    for blob in bucket.list_blobs(prefix=prefix):
        if not blob.name.endswith(".jsonl"):
            continue
        for line in blob.download_as_text().splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))

    raw = ingest_records_to_raw(
        records, submitted_ids,
        model=fyp_cf["machine"]["model"],
        prompt_fn=os.path.basename(fyp_cf["machine"]["prompt"]),
        annotation_version=annotation_versioning.current_annotation_version(),
    )
    fine_ts = "".join(c for c in str(_dt.datetime.now()) if c in "0123456789")
    filename = f"{MACHINE_ANNOTATIONS_LABEL}_{fine_ts}.json"
    data_io.save_json(data=raw, storage_location="machine_annotations_raw", filename=filename)
    return filename
