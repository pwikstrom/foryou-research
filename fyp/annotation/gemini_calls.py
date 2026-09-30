"""Calling Gemini: client setup, generation config, retries and the threaded batch caller.

``initialize_machine`` builds the client (stored in the ``[machine.gemini]``
config block); ``call_machine`` sends one item's media and prompt with the
structured-output generation config and retries transient errors;
``call_machine_threads`` annotates a batch in parallel and saves the raw
responses for refinement.
"""

import datetime as _dt
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from random import random

import google.genai

import fyp.annotation.annotation_versioning as annotation_versioning
import fyp.core.data_io as data_io
import fyp.core.gemini_client as gemini_client
import fyp.core.media_paths as media_paths
import fyp.core.utils as fyp_utils
import fyp.scrape.scrape_queues as scrape_queues
from fyp.annotation.annotation_schema import (
    build_response_schema,
)
from fyp.core.logging_setup import get_logger
from fyp.core.progress_monitor import start_monitor
from fyp.core.runtime import cf as _cf
from fyp.core.runtime import label

logger = get_logger(__name__)


def _gcf():
    """The ``[machine.gemini]`` config block (canonical Gemini home)."""
    return _cf()["machine"]["gemini"]


def _machine_annotations_label() -> str:
    """Lazy accessor for the config-derived machine-annotations label."""
    return label("MACHINE_ANNOTATIONS_LABEL")


def initialize_machine():

    if _gcf().get("client", None) is not None:
        return _cf()

    _gcf()["client"] = None

    mode, reason = gemini_client.gemini_mode()
    if mode is None:
        logger.warning(reason)
        return

    if fyp_utils.online_ok():
        try:
            http_options = google.genai.types.HttpOptions(
                api_version=_gcf()["http_options_api_version"],
                timeout=_gcf()["http_options_timeout"],
            )
            _gcf()["client"] = gemini_client.make_client(http_options=http_options)

            logger.info(f"Google Gemini initialized successfully (mode: {mode})")

        except Exception as e:
            logger.error(f"Could not initialize Gemini. Gemini won't be available. {e}")

    else:
        logger.warning("I'm offline. Can't initialize Google Gemini.")


def _resolve_media_resolution(value=None):
    """Map a ``media_resolution`` setting to a genai enum, or ``None``.

    Empty / unset returns ``None`` (use the API default — unchanged behaviour).
    For Gemini-3 video, LOW and MEDIUM are equivalent (~70 tokens/frame) and HIGH
    is ~280 tokens/frame, so LOW is the cost lever. Accepts a bare level
    ("LOW") or the full enum name ("MEDIA_RESOLUTION_LOW").

    Args:
        value: An explicit setting (a variant/arm override); None reads the
            configured ``[machine].media_resolution``.

    Returns:
        A ``google.genai.types.MediaResolution`` value, or ``None``.
    """
    if value is None:
        value = _gcf().get("media_resolution", "")
    value = str(value or "").strip().upper()
    if not value:
        return None
    if not value.startswith("MEDIA_RESOLUTION_"):
        value = f"MEDIA_RESOLUTION_{value}"
    return getattr(google.genai.types.MediaResolution, value, None)


def build_structured_generation_config(gen_overrides: dict | None = None):
    """Build the structured-output generation config (cached when unmodified).

    Reuses the existing prompt as the system instruction and attaches the
    response schema from :mod:`fyp.annotation.annotation_schema`, so decoding is constrained
    to valid, conforming JSON. Repetition penalties are intentionally omitted —
    constrained decoding plus a thinking model does not loop the way free-text
    generation can (validated by an A/B evaluation against free-text output).

    Args:
        gen_overrides: Optional overrides for ``temperature`` /
            ``max_output_tokens`` / ``thinking_budget`` / ``media_resolution``
            (a variant's pins or an A/B arm's params). A non-empty dict builds
            a fresh config and never touches the cache slot, so the default
            path stays byte-identical.

    Returns:
        The ``GenerateContentConfig`` for structured annotation (the cached
        instance when ``gen_overrides`` is empty).
    """
    gen_overrides = {k: v for k, v in (gen_overrides or {}).items() if v is not None}
    if not gen_overrides and _gcf().get("structured_generation_config") is not None:
        return _gcf()["structured_generation_config"]

    machine_prompt = annotation_versioning.active_prompt_text()
    machine = {**_gcf(), **gen_overrides}

    gen_config = google.genai.types.GenerateContentConfig(
        system_instruction=machine_prompt,
        temperature=machine["temperature"],
        max_output_tokens=machine["max_output_tokens"],
        response_mime_type="application/json",
        response_schema=build_response_schema(),
        media_resolution=_resolve_media_resolution(machine.get("media_resolution")),
        thinking_config=google.genai.types.ThinkingConfig(
            thinking_budget=machine["thinking_budget"]
        ),
    )
    if not gen_overrides:
        _gcf()["structured_generation_config"] = gen_config
    return gen_config


# Transient failures (rate limits, 5xx, deadline/timeout, dropped connections)
# can plausibly succeed on a retry; client errors (bad request, missing media,
# auth, safety block) cannot and must fail fast.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_RETRYABLE_MARKERS = (
    "deadline_exceeded",
    "deadline exceeded",
    "unavailable",
    "resource_exhausted",
    "resource exhausted",
    "rate limit",
    "internal error",
    "internal server error",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "too many requests",
)


def _is_transient_error(exc: Exception) -> bool:
    """Decide whether a Gemini call failure is worth retrying.

    Transient failures (rate limits, 5xx server errors, deadline/timeout,
    dropped connections) can plausibly succeed on a retry; client-side errors
    (malformed request, missing media, auth failure, safety block) cannot.

    Args:
        exc: The exception raised by the Gemini call.

    Returns:
        True if a retry could plausibly succeed, False otherwise.
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    code = getattr(exc, "code", None)
    if not isinstance(code, int):
        code = getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _RETRYABLE_STATUS_CODES:
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS)


def _generate_with_retry(contents, gen_config, model: str | None = None):
    """Call the Gemini model with bounded exponential-backoff retries.

    Only transient errors (see :func:`_is_transient_error`) are retried; every
    other error propagates immediately so the caller records it as a DNF. The
    retry count and base backoff are read from config (``max_retries``,
    ``retry_base_delay``) with conservative defaults, so the policy is tunable
    without code changes.

    Args:
        contents: The request contents (video part + instruction).
        gen_config: The resolved ``GenerateContentConfig``.
        model: Optional model-id override (a variant's pin); None uses the
            configured ``[machine].model``.

    Returns:
        The model response object from ``generate_content``.

    Raises:
        Exception: The last exception if every attempt fails, or any
            non-transient error on its first occurrence.
    """
    if _gcf().get("client") is None:
        raise RuntimeError(
            "Gemini client not configured - see [machine] in config "
            "(set a Vertex project, or vertexai = false with GEMINI_API_KEY)."
        )

    max_retries = int(_gcf().get("max_retries", 2))
    base_delay = float(_gcf().get("retry_base_delay", 2.0))

    attempt = 0
    while True:
        try:
            return _gcf()["client"].models.generate_content(
                model=model or _gcf()["model"],
                config=gen_config,
                contents=contents,
            )
        except Exception as exc:
            if attempt >= max_retries or not _is_transient_error(exc):
                raise
            time.sleep(base_delay * (2**attempt) + random())
            attempt += 1


def call_machine(
    video_id: str = None,
    use_local_video_file=False,
    local_path: str | None = None,
    verbose=False,
    dry_run=False,
    platform: str | None = None,
    gen_overrides: dict | None = None,
) -> dict:

    initialize_machine()

    # A variant's pins (model / gen params) ride in as overrides; empty means
    # the exact historical config-driven path.
    gen_overrides = {k: v for k, v in (gen_overrides or {}).items() if v is not None}
    effective_model = gen_overrides.get("model") or _gcf()["model"]

    if dry_run:
        time.sleep(1)
        if verbose:
            logger.info(f"Dry run: would have annotated video {video_id}")
        return {
            "item_id": video_id,
            "error": "dry run",
            "finish_reason": "dry run",
            "response": "dry run",
        }

    # Platform of the item being annotated: drives media resolution and is
    # stamped onto the output row. Unmapped items fall back to the default
    # platform (resolve_media probes the other platforms' subpaths anyway).
    annotation_platform = platform or scrape_queues.default_platform()

    times = [_dt.datetime.now()]
    output = {
        "item_id": video_id,
        "source_platform": annotation_platform,
        "inference_ts": int(times[-1].timestamp()),
        "inference_duration": -1,
        "model": effective_model,
        "prompt_fn": annotation_versioning.active_prompt_label(),
        "annotation_version": annotation_versioning.active_annotation_version(),
        "structured": True,
        "usage": {},
        # None until an exception handler fills it — a successful call must
        # not report an error (nothing downstream reads this field; it exists
        # for humans debugging the raw output rows and temp JSONs).
        "error": None,
        "finish_reason": "did not even start",
        "response": "",
    }

    temp_fn = f"temp_machine_annotations_{output['item_id']}_{output['inference_ts']}.json"

    # The explicit kwarg is an override; otherwise the config flag decides.
    effective_local = use_local_video_file or not _cf()["data_io"]["use_gcs_for_media"]
    effective_local_dir = local_path or _cf()["paths"]["media"]

    # Media may live at the per-platform subpath or the legacy flat path;
    # media_paths.resolve_media owns that fallback order.
    resolved_media = None
    if local_path:
        # Explicit dir override (tests / one-offs): probe flat then platform subpath.
        for candidate in media_paths.candidate_relpaths(video_id, annotation_platform):
            path = os.path.join(effective_local_dir, candidate)
            if os.path.exists(path):
                resolved_media = {"kind": "local", "path": path}
                break
    else:
        resolved_media = media_paths.resolve_media(video_id, platform=annotation_platform)

    # initialise the contents for the model
    try:
        if effective_local:
            if verbose:
                logger.info(f"Using local video file for video id {video_id}")
            local_file = (
                resolved_media["path"]
                if resolved_media and resolved_media["kind"] == "local"
                else os.path.join(effective_local_dir, f"{video_id}.mp4")
            )
            with open(local_file, "rb") as f:
                video_bytes = f.read()
            contents = [
                google.genai.types.Part(
                    inline_data=google.genai.types.Blob(data=video_bytes, mime_type="video/mp4")
                ),
                google.genai.types.Part.from_text(text="Analyze this video"),
            ]
        else:
            if resolved_media and resolved_media["kind"] == "gcs":
                file_uri = media_paths.media_gs_uri(resolved_media)
            else:
                file_uri = f"gs://{_cf()['data_io']['GCS_bucket_name']}/{_cf()['data_io']['gcs_media_prefix']}/{video_id}.mp4"
            contents = [
                google.genai.types.Part.from_uri(file_uri=file_uri, mime_type="video/mp4"),
                google.genai.types.Part.from_text(text="Analyze this video"),
            ]

    except Exception as e:
        output["error"] = str(e)
        with open(os.path.join(_cf()["paths"]["temp"], temp_fn), "w") as file:
            json.dump(output, file)

        return output

    # run the model
    try:
        start_ts = _dt.datetime.now()
        resp = _generate_with_retry(
            contents, build_structured_generation_config(gen_overrides), model=effective_model
        )
    except Exception as e:
        times += [_dt.datetime.now()]

        # Same resolution order as the upload above (platform subpath + legacy flat).
        video_found = media_paths.resolve_media(video_id, platform=annotation_platform) is not None

        output["error"] = str(e)
        output["inference_duration"] = (times[-1] - times[-2]).total_seconds()

        if not video_found:
            output["finish_reason"] = "DNF - file not found in storage"
        else:
            output["finish_reason"] = "DNF - see error msg"

        with open(os.path.join(_cf()["paths"]["temp"], temp_fn), "w") as file:
            json.dump(output, file)
        return output

    try:
        the_finish_reason = str(resp.candidates[0].finish_reason)
    except (IndexError, AttributeError):
        the_finish_reason = "Finished, but don't know why"

    times += [_dt.datetime.now()]

    try:
        machine_annotations = copy(resp.text)
    except Exception as e:
        output["error"] = str(e)
        output["inference_duration"] = (times[-1] - times[-2]).total_seconds()
        output["finish_reason"] = the_finish_reason
        output["response"] = resp

        with open(os.path.join(_cf()["paths"]["temp"], temp_fn), "w") as file:
            json.dump(output, file)
        return output

    output["inference_duration"] = (times[-1] - times[-2]).total_seconds()
    output["finish_reason"] = the_finish_reason
    output["response"] = machine_annotations

    usage = getattr(resp, "usage_metadata", None)
    if usage is not None:
        output["usage"] = {
            "prompt_tokens": getattr(usage, "prompt_token_count", None),
            "candidates_tokens": getattr(usage, "candidates_token_count", None),
            "thoughts_tokens": getattr(usage, "thoughts_token_count", None),
            "total_tokens": getattr(usage, "total_token_count", None),
        }

    # save the json just in case everything crashes
    with open(os.path.join(_cf()["paths"]["temp"], temp_fn), "w") as file:
        json.dump(output, file)

    return output


def call_machine_threads(
    interesting_videos=None,
    max_workers=50,
    verbose=False,
    notebook_mode=False,
    dry_run=False,
    batch_label: str | None = None,
    cumulative_done: int = 0,
    cumulative_total: int = 0,
    cumulative_ok: int = 0,
    cumulative_fail: int = 0,
    reporter=None,
    platform_by_id: dict[str, str] | None = None,
):

    if notebook_mode:
        verbose = True

    # Per-backend dispatch: Gemini keeps the historical path verbatim; another
    # backend (local model) runs its own annotate_one with its own worker
    # width (a resident local model is effectively sequential).
    from fyp.annotation.backends import active_backend_name, get_backend

    backend_name = active_backend_name()
    backend = get_backend(backend_name) if backend_name != "gemini" else None
    if backend is not None:
        max_workers = backend.max_workers
    # A gemini *variant* rides the generic branch above but still talks to the
    # Gemini API — stagger and deadlines follow the implementation, not the
    # dispatch branch.
    _is_gemini_api = backend is None or backend.name == "gemini"

    if backend is None:
        initialize_machine()

    annotation_versioning.ensure_active_version_registered()

    results_by_index = {}

    def worker(idx_video):
        idx, video = idx_video

        # Stagger the first wave of requests: a burst of simultaneous calls to
        # the Gemini API tends to fail, and a short randomized sleep avoids it.
        # (Local backends are sequential — no stagger needed.)
        if _is_gemini_api and idx < max_workers:
            time.sleep(3 + random() * max_workers / 2)

        if backend is not None:
            if dry_run:
                time.sleep(1)
                return idx, {
                    "item_id": video,
                    "error": "dry run",
                    "finish_reason": "dry run",
                    "response": "dry run",
                }
            rr = backend.annotate_one(str(video), platform=(platform_by_id or {}).get(str(video)))
            return idx, rr

        t1 = _dt.datetime.now()
        rr = call_machine(
            video_id=video,
            dry_run=dry_run,
            verbose=verbose,
            platform=(platform_by_id or {}).get(str(video)),
        )

        return idx, rr

    _effective_model = backend.effective_model_id() if backend is not None else _gcf()["model"]
    if verbose:
        if dry_run:
            print("  [dry run] - ", end="", flush=True)
        logger.info(
            f"Calling {_effective_model} to annotate {len(interesting_videos):,} videos with {max_workers} threads."
        )

    def _annotation_ok(fut):
        try:
            _, rr = fut.result()
            return (
                bool(rr)
                and bool(rr.get("response"))
                and not str(rr.get("finish_reason", "")).startswith("DNF")
            )
        except Exception:
            return False

    # Per-batch deadline guards against individual Gemini calls hanging past the
    # SDK's http_options_timeout (observed in practice — SDK timeout is not
    # always honored). Deadline scales with the number of waves the thread
    # pool needs to process, with 1.5x safety margin plus startup-jitter buffer.
    # Gemini: matches http_options_timeout (ms→s). Local backend: the first
    # item also loads the model (~1-2 min) on top of ~30-60s inference.
    _per_call_seconds = 180 if _is_gemini_api else 600
    _safety_margin = 1.5
    _startup_sleep = 3 + max_workers / 2  # upper bound of worker() sleep
    _waves = max(1, (len(interesting_videos) + max_workers - 1) // max_workers)
    # Extra headroom for retry backoff sleeps a worker may incur on transient
    # failures (sum of base*2^k for k < max_retries).
    _max_retries = int(_gcf().get("max_retries", 2))
    _retry_base_delay = float(_gcf().get("retry_base_delay", 2.0))
    _retry_backoff = _retry_base_delay * (2**_max_retries - 1)
    batch_deadline = int(
        _waves * _per_call_seconds * _safety_margin + _startup_sleep + 60 + _retry_backoff
    )
    logger.info(
        f"[machine] batch_deadline={batch_deadline}s for {len(interesting_videos)} items, "
        f"{max_workers} workers, {_waves} waves"
    )

    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = []
        submit_times = {}
        for iv in enumerate(interesting_videos):
            fut = ex.submit(worker, iv)
            futures.append(fut)
            submit_times[fut] = time.time()

        monitor_thread = start_monitor(
            futures,
            submit_times,
            interval=5,
            label="machine",
            bar_width=32,
            result_checker=_annotation_ok,
            batch_label=batch_label,
            cumulative_done=cumulative_done,
            cumulative_total=cumulative_total,
            cumulative_ok=cumulative_ok,
            cumulative_fail=cumulative_fail,
            reporter=reporter,
        )

        # Collect results, bounding EACH worker by its own per-item deadline
        # (measured from when it was submitted) rather than only the wave-scaled
        # whole-batch deadline. A single hung Gemini call — the SDK
        # http_options_timeout is "not always honored" — would otherwise hold the
        # entire batch open until batch_deadline; per-item bounding abandons just
        # that straggler ~one per-call budget after it started. A normal call
        # (<= per-call budget) is never cut short; batch_deadline stays as an
        # absolute backstop.
        per_item_deadline = _per_call_seconds * _safety_margin + _retry_backoff
        wait_start = time.time()
        outstanding = set(range(len(futures)))
        timed_out = []
        while outstanding:
            now = time.time()
            for i in list(outstanding):
                fut = futures[i]
                if fut.done():
                    idx, res = fut.result()
                    results_by_index[idx] = res
                    outstanding.discard(i)
                elif now - submit_times[fut] > per_item_deadline:
                    # Blew its per-item deadline — stop waiting; the DNF block
                    # below records it and shutdown(cancel_futures) abandons it.
                    timed_out.append(i)
                    outstanding.discard(i)
            if not outstanding:
                break
            if time.time() - wait_start > batch_deadline:
                logger.warning(
                    f"[machine] Absolute batch deadline of {batch_deadline}s "
                    f"exceeded; {len(outstanding)} worker(s) still running."
                )
                break
            time.sleep(0.5)

        if timed_out:
            stuck = [interesting_videos[i] for i in timed_out]
            logger.warning(
                f"[machine] {len(timed_out)} worker(s) exceeded the per-item "
                f"deadline of {int(per_item_deadline)}s and were abandoned: "
                f"{stuck[:5]}" + (" ..." if len(stuck) > 5 else "")
            )

        # Record DNF entries for any video whose worker didn't return in time
        for i in range(len(futures)):
            if i in results_by_index:
                continue
            results_by_index[i] = {
                "item_id": interesting_videos[i],
                "error": f"worker did not complete within its {int(per_item_deadline)}s deadline",
                "finish_reason": "DNF - worker timeout",
                "response": "",
                "model": _effective_model,
            }

        monitor_thread.join(timeout=10)
    finally:
        # Don't wait for stuck worker threads — they'll be killed at process exit
        ex.shutdown(wait=False, cancel_futures=True)

    if verbose:
        logger.info(f"Items processed: {len(results_by_index)}")

    # No raw file is written on a dry run (or an empty batch) — filename stays None.
    filename = None
    if len(results_by_index) > 0 and not dry_run:
        fine_ts = "".join([k for k in str(_dt.datetime.now()) if k in "0123456789"])

        filename = f"{_machine_annotations_label()}_{fine_ts}.json"

        data_io.save_json(
            data=results_by_index,
            storage_location="machine_annotations_raw",
            filename=filename,
            verbose=verbose,
        )
        if verbose:
            logger.info(f"Saved raw machine annotations to '{filename}'")

    return results_by_index, filename
