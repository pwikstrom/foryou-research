#!/usr/bin/env python3
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""

import collections
import datetime as _dt
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from pathlib import Path
from random import random

import fuzzy_json
import google.genai
import numpy as np
import pandas as pd

import fyp.data_io as data_io
import fyp.media_paths as media_paths
import fyp.scrape_queues as scrape_queues
import fyp.utils as fyp_utils
import fyp.annotation_versioning as annotation_versioning
import fyp.core.gemini_client as gemini_client
from fyp.logging_setup import get_logger

#from fyp.organize_datasets import select_videos_from_study_dataset
from fyp.annotation_schema import (
    build_response_schema,
    flatten_structured,
)
from fyp.recode_variables import recode_events_df, recode_fuzzy_match, rename_columns
from fyp.types import convert_dtypes_to_pyarrow
from fyp.utils import start_monitor

logger = get_logger(__name__)


def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf


def _gcf():
    """The ``[machine.gemini]`` config block (canonical Gemini home)."""
    return _cf()["machine"]["gemini"]



def _check_graceful_stop(process_name: str) -> bool:
    """Check if a graceful stop has been requested via sentinel file."""
    sentinel = Path(_cf()['paths']['project_root']) / "tmp" / "graceful_stop" / f"{process_name}.stop"
    return sentinel.exists()


def _machine_annotations_label() -> str:
    """Lazy accessor for the config-derived machine-annotations label."""
    return _cf()["labels"]["MACHINE_ANNOTATIONS_LABEL"]




def __getattr__(name: str):
    """Serve the config-derived module constant lazily (PEP 562)."""
    if name == "MACHINE_ANNOTATIONS_LABEL":
        return _machine_annotations_label()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")








# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# functions in this section call the machine and get the raw responses
# *********************************************************************************************************
# *********************************************************************************************************





def invalidate_caches():
    """Drop the cached Gemini client and generation config.

    Both rebuild lazily on next use. Call after any runtime change to
    ``[machine]`` values (the cached ``GenerateContentConfig`` bakes in
    temperature / max_output_tokens / media_resolution / thinking_budget, and
    the client bakes in the credential mode). The version descriptor cache in
    :mod:`fyp.annotation_versioning` self-invalidates via its config signature.
    """
    _gcf()["structured_generation_config"] = None
    _gcf()["client"] = None






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
                timeout=_gcf()["http_options_timeout"]
            )
            _gcf()["client"] = gemini_client.make_client(
                http_options=http_options
            )

            logger.info(f"Google Gemini initialized successfully (mode: {mode})")


        except Exception as e:
            logger.error(f"Could not initialize Gemini. Gemini won't be available. {e}")

    else:
        logger.warning("I'm offline. Can't initialize Google Gemini.")


def annotation_configured() -> tuple[bool, str]:
    """Whether machine annotation is configured to run on the active backend.

    A pure config/dependency check — no network, no client construction.
    Dispatches to the active backend's ``availability()`` (see
    :mod:`fyp.annotation.backends`): for Gemini that reproduces the historical
    credential + gs://-media rules byte-identically; for a local backend it
    covers platform / dependency / model-download requirements instead.

    Returns:
        ``(ok, reason)``. When ``ok`` is False, ``reason`` is a user-facing
        explanation of what to configure; an empty string otherwise.
    """
    from fyp.annotation.backends import active_backend_name, get_backend

    name = active_backend_name()
    try:
        backend = get_backend(name)
    except ValueError as exc:
        return False, str(exc)
    result = backend.availability(deep=False)
    return result.ok, result.reason






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
    response schema from :mod:`fyp.annotation_schema`, so decoding is constrained
    to valid, conforming JSON. Repetition penalties are intentionally omitted —
    constrained decoding plus a thinking model does not loop the way free-text
    generation can (validated by the Phase 2 A/B evaluation).

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
            time.sleep(base_delay * (2 ** attempt) + random())
            attempt += 1






def call_machine(
        video_id: str = None,
        use_local_video_file = False,
        local_path: str | None = None,
        verbose = False,
        dry_run = False,
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
            "item_id" : video_id,
            "error" : "dry run",
            "finish_reason": "dry run",
            "response" : "dry run",
        }



    # Platform of the item being annotated: drives media resolution and is
    # stamped onto the output row. Unmapped items fall back to the default
    # platform (resolve_media probes the other platforms' subpaths anyway).
    annotation_platform = platform or scrape_queues.default_platform()

    times = [_dt.datetime.now()]
    output = {
        "item_id" : video_id,
        "source_platform" : annotation_platform,
        "inference_ts" : int(times[-1].timestamp()),
        "inference_duration" : -1,
        "model" : effective_model,
        "prompt_fn" : annotation_versioning.active_prompt_label(),
        "annotation_version" : annotation_versioning.current_annotation_version(),
        "structured" : True,
        "usage" : {},
        # None until an exception handler fills it — a successful call must
        # not report an error (nothing downstream reads this field; it exists
        # for humans debugging the raw output rows and temp JSONs).
        "error" : None,
        "finish_reason": "did not even start",
        "response" : "",
    }

    temp_fn = f"temp_machine_annotations_{output['item_id']}_{output['inference_ts']}.json"

    # The explicit kwarg is an override; otherwise the config flag decides.
    effective_local = use_local_video_file or not _cf()['data_io']['use_gcs_for_media']
    effective_local_dir = local_path or _cf()['paths']['media']

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
            with open(local_file,'rb') as f:
                video_bytes = f.read()
            contents = [
                google.genai.types.Part(
                    inline_data=google.genai.types.Blob(data=video_bytes,
                    mime_type='video/mp4')
                ),
                google.genai.types.Part.from_text(text="Analyze this video")
            ]
        else:
            if resolved_media and resolved_media["kind"] == "gcs":
                file_uri = media_paths.media_gs_uri(resolved_media)
            else:
                file_uri = f"gs://{_cf()['data_io']['GCS_bucket_name']}/{_cf()['data_io']['gcs_media_prefix']}/{video_id}.mp4"
            contents = [
                google.genai.types.Part.from_uri(
                    file_uri=file_uri,
                    mime_type="video/mp4"
                ),
                google.genai.types.Part.from_text(text="Analyze this video")
            ]

    except Exception as e:
        output["error"] = str(e)
        with open(os.path.join(_cf()["paths"]["temp"], temp_fn), 'w') as file:
            json.dump(output, file)

        return output


    # run the model
    try:
        start_ts = _dt.datetime.now()
        resp = _generate_with_retry(contents, build_structured_generation_config(gen_overrides),
                                    model=effective_model)
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

        with open(os.path.join(_cf()["paths"]["temp"], temp_fn), 'w') as file:
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

        with open(os.path.join(_cf()["paths"]["temp"], temp_fn), 'w') as file:
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
    with open(os.path.join(_cf()["paths"]["temp"], temp_fn), 'w') as file:
        json.dump(output, file)

    return output
















def call_machine_threads(
        interesting_videos = None,
        max_workers=50,
        verbose=False,
        notebook_mode = False,
        dry_run = False,
        batch_label: str | None = None,
        cumulative_done: int = 0,
        cumulative_total: int = 0,
        cumulative_ok: int = 0,
        cumulative_fail: int = 0,
        reporter=None,
        platform_by_id: dict[str, str] | None = None):

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

    annotation_versioning.ensure_current_version_registered()

    results_by_index = {}

    def worker(idx_video):
        idx, video = idx_video

        # Maybe Gemini doesn't like to get to many request at once.
        # Sleeping for a bit with the first ones solves the problem.
        # (Local backends are sequential — no stagger needed.)
        if _is_gemini_api and idx < max_workers:
            time.sleep(3+random()*max_workers/2)

        if backend is not None:
            if dry_run:
                time.sleep(1)
                return idx, {"item_id": video, "error": "dry run",
                             "finish_reason": "dry run", "response": "dry run"}
            rr = backend.annotate_one(
                str(video), platform=(platform_by_id or {}).get(str(video)))
            return idx, rr

        t1 = _dt.datetime.now()
        rr = call_machine(
            video_id = video,
            dry_run = dry_run,
            verbose = verbose,
            platform = (platform_by_id or {}).get(str(video)),
        )

        return idx, rr


    _effective_model = (backend.effective_model_id() if backend is not None
                        else _gcf()["model"])
    if verbose:
        if dry_run:
            print("  [dry run] - ", end="", flush=True)
        logger.info(f"Calling {_effective_model} to annotate {len(interesting_videos):,} videos with {max_workers} threads.")

    def _annotation_ok(fut):
        try:
            _, rr = fut.result()
            return bool(rr) and bool(rr.get("response")) and not str(rr.get("finish_reason", "")).startswith("DNF")
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
    _retry_backoff = _retry_base_delay * (2 ** _max_retries - 1)
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
            futures, submit_times, interval=5, label="machine", bar_width=32,
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
        for i, fut in enumerate(futures):
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
    if len(results_by_index)>0 and not dry_run:

        fine_ts = "".join([k for k in str(_dt.datetime.now()) if k in "0123456789"])

        filename = f"{_machine_annotations_label()}_{fine_ts}.json"

        data_io.save_json(data=results_by_index, storage_location="machine_annotations_raw", filename=filename, verbose=verbose)
        if verbose:
            logger.info(f"Saved raw machine annotations to '{filename}'")



    return results_by_index, filename






# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# I'm not using structured outputs because I understand that the machine is calling all the required bits
# and pieces in the request at the same time if I'd do that. I want it to think about it sequentially. So
# as a result it happens that the json like output structure is wrong and introduces labels and keys that
# I don't want. This funciton is trying to figure out which columns are rare and try to merge them back 
# into the dominant columns. 
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************




# Minimum difflib name-similarity required to merge a rare (<10% populated)
# column into a dominant (>90% populated) one. Genuine stray-key variants score
# ~0.85+, while unrelated pairs (e.g. a real column vs item_id) score <0.35, so
# 0.6 cleanly separates them and prevents collapsing mostly-failed batches.
RARE_COLUMN_MERGE_MIN_SIMILARITY = 0.6




def consolidate_rare_columns_from_gemini_output(
        outputs_from_machine_df_in,
        verbose=False,
        notebook_mode = False):

    if notebook_mode:
        verbose = True
    """
    Clean up Gemini’s loosely structured output:
    1. Compute each column’s non-null ratio so we can spot “rare” keys (<10% populated).
    2. For every rare column, find the most similar high-population (“dominant”) column name.
    3. Move the rare column’s values into the dominant column whenever that row is empty there; otherwise clear the rare slot.
    4. Recalculate ratios, drop any columns that are now entirely empty, and repeat until no rare columns remain.
    
    This effectively merges stray keys back into their intended dominant columns and removes the redundant leftovers.
    """


    outputs_from_machine_df = outputs_from_machine_df_in.copy()

    nonnull_ratio = (len(outputs_from_machine_df) - outputs_from_machine_df.isna().sum()) / len(outputs_from_machine_df)

    if notebook_mode:
        logger.info(outputs_from_machine_df.shape)
        logger.info(len(nonnull_ratio[nonnull_ratio<0.1]))
        logger.info(nonnull_ratio[nonnull_ratio<0.1])
        logger.info(len(nonnull_ratio[nonnull_ratio<0.5]))
        logger.info(nonnull_ratio[nonnull_ratio<0.5])
        logger.info(len(nonnull_ratio[nonnull_ratio<0.8]))
        logger.info(nonnull_ratio[nonnull_ratio<0.8])



    nonnull_ratio = (len(outputs_from_machine_df) - outputs_from_machine_df.isna().sum()) / len(outputs_from_machine_df)

    little_counter = 0

    while(len(nonnull_ratio[nonnull_ratio<0.1]))>0 and little_counter<5:
        if verbose:
            logger.info(little_counter)
            logger.info(len(nonnull_ratio[nonnull_ratio<0.1]))


        for unusual_col_name in nonnull_ratio[nonnull_ratio<0.1].index:
            try:
                if verbose:
                    logger.info(len(outputs_from_machine_df) - outputs_from_machine_df[unusual_col_name].isna().sum())
                dominant_col_name, similarity = fyp_utils.best_similarity_match(unusual_col_name, nonnull_ratio[nonnull_ratio>0.9].index)

                # Only merge a rare column into a dominant one when their names are
                # genuinely similar (a stray-key variant). Without this guard a
                # mostly-failed batch — where the only well-populated column is
                # item_id — merges every real column into item_id and clears the
                # values, collapsing the batch to item_id alone.
                if dominant_col_name is not None and similarity >= RARE_COLUMN_MERGE_MIN_SIMILARITY:
                    rows_w_nonnull_value_in_unusual_col = outputs_from_machine_df[~outputs_from_machine_df[unusual_col_name].isna()].loc[:,[dominant_col_name,unusual_col_name]]

                    for ii in rows_w_nonnull_value_in_unusual_col.index:
                        if outputs_from_machine_df.loc[ii,dominant_col_name] is np.nan:
                            if verbose:
                                logger.info(f"******* {ii} {dominant_col_name}")
                            outputs_from_machine_df.loc[ii,dominant_col_name] = outputs_from_machine_df.loc[ii,unusual_col_name]
                        else:
                            outputs_from_machine_df.loc[ii,unusual_col_name] = np.nan
            except KeyError:
                if verbose:
                    logger.error(f"ERROR: {unusual_col_name} doesn't seem to be among the columns")


            little_counter += 1


        nonnull_ratio = (len(outputs_from_machine_df) - outputs_from_machine_df.isna().sum()) / len(outputs_from_machine_df)
        outputs_from_machine_df.drop(nonnull_ratio[nonnull_ratio==0].index,axis=1,inplace=True, errors='ignore')
        if verbose:
            logger.info(outputs_from_machine_df.shape)
            logger.info("------------------------------------------------------")
        
        
    return outputs_from_machine_df







# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# functions in this section flatten and transform raw output jsons into a nice dataframe
# the main function is at the bootm of the section
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************





def flatten_one_machine_response(
        some_response,
        verbose=False,
        notebook_mode = False):

    if notebook_mode:
        verbose = True
    """
    Flattens a machine response into a single level dictionary.
    NOTE: This is directly dependent on the prompt you are using. 
    Changes to the prompt will require changes to this function
    """



    # if the response is not a dictionary, something is wrong - return it as is
    if some_response is None or type(some_response) != dict:
        if notebook_mode:
            logger.info(type(some_response))
        return some_response

    flat_response = deepcopy(some_response)

    # #######################
    # scenes
    if 'scenes' in flat_response.keys():
        if isinstance(flat_response['scenes'], str):
            flat_response['scenes'] = re.sub(r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response['scenes'])
            try:
                flat_response['scenes'] = fuzzy_json.loads(flat_response['scenes'])
            except Exception:
                return None
        if isinstance(flat_response['scenes'], list):
            try:
                description_list = []
                sentiment_list = []
                for k in flat_response['scenes']:
                    if isinstance(k, dict):
                        description_list += [k.get('description','')]
                        sentiment_list += [k.get('sentiment','')]
                flat_response['scenes'] = " | ".join(description_list)
                tt1 = collections.Counter(sentiment_list).most_common(1)
                if len(tt1) == 0:
                    flat_response['scene_sentiments'] = ""
                else:
                    flat_response['scene_sentiments'] = tt1[0][0]
            except Exception:
                return None
        else:
            return None

    # #######################
    # transcript
    if 'transcript' in flat_response.keys():
        if isinstance(flat_response['transcript'], str):
            flat_response['transcript'] = re.sub(r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response['transcript'])
            try:
                flat_response['transcript'] = fuzzy_json.loads(flat_response['transcript'])
            except Exception:
                return None
        if isinstance(flat_response['transcript'], list):
            try:
                text_list = []
                for k in flat_response['transcript']:
                    if isinstance(k, dict):
                        text_list += [k.get('text','')]
                    elif isinstance(k, str):
                        text_list += [k]
                flat_response['transcript'] = " | ".join(text_list)
            except Exception:
                return None
            #elif isinstance(flat_response['transcript'], str):
            #    aa = re.sub(r"\{.*?\|", ' | ', flat_response['transcript'].replace("'text':"," | "))
            #    flat_response['transcript'] = aa.replace("'},  | "," |").replace(" '"," ")[3:-3].strip()
        else:
            return None
            #flat_response['transcript'] = ""


    # #######################
    # objects
    for res_key in ['objects','symbols_and_brands','text_overlays','content_category']:
        if res_key in flat_response.keys():
            if isinstance(flat_response[res_key], str):
                flat_response[res_key] = re.sub(r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response[res_key])
                try:
                    flat_response[res_key] = fuzzy_json.loads(flat_response[res_key])
                except Exception:
                    if verbose:
                        logger.info(flat_response[res_key])
                    return None
            if isinstance(flat_response[res_key], list):
                try:
                    res_list = []
                    for k in flat_response[res_key]:
                        if isinstance(k, dict):
                            res_list += [k.get(res_key,'')]
                        elif isinstance(k, str):
                            res_list += [k]
                    flat_response[res_key] = " | ".join(res_list)
                except Exception:
                    return None
            else:#elif not isinstance(flat_response[res_key], str):
                return None
                #flat_response[res_key] = ""



    # #######################
    # sometimes audio summary hasn't been converted to json
    # not sure why this happens, this is trying to do something about that
    if 'audio_summary' in flat_response.keys():
        if isinstance(flat_response['audio_summary'],str):
            flat_response['audio_summary'] = re.sub(r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response['audio_summary'])
            try:
                flat_response['audio_summary'] = fuzzy_json.loads(flat_response['audio_summary'])
            except Exception:
                if verbose:
                    logger.info(flat_response['audio_summary'])
                return None
        
        for k in flat_response['audio_summary']:
            try:
                audio_detail = flat_response['audio_summary'][k]
            except Exception as e:
                if verbose:
                    logger.warning(f"{e} | {k} | {flat_response['audio_summary']}")
                return None
            if isinstance(audio_detail,list):
                flat_response[k] = " | ".join([s for s in audio_detail if type(s)==str])
            elif isinstance(audio_detail,str):
                flat_response[k] = audio_detail
            else:
                return None
        del flat_response['audio_summary']

    # #######################
    # faces
    if 'faces' in flat_response.keys():
        if isinstance(flat_response['faces'], str):
            flat_response['faces'] = re.sub(r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response['faces'])
            try:
                flat_response['faces'] = fuzzy_json.loads(flat_response['faces'])
            except Exception:
                if verbose:
                    logger.info(flat_response['faces'])
                return None

        if isinstance(flat_response['faces'], list):
            for face in flat_response['faces']:
                if isinstance(face, dict):
                    for k in face:
                        if "faces_"+k not in flat_response.keys():
                            flat_response["faces_"+k] = ""                    
                        try:
                            flat_response["faces_"+k] += str(face[k]) + " | "
                        except Exception:
                            return None
                else:
                    return None
        else:
            return None
        del flat_response['faces']

        for k in flat_response:
            if (k.startswith("faces_")) and (isinstance(flat_response[k],str)) and (flat_response[k].endswith(" | ")):
                flat_response[k] = flat_response[k][:-3]    


    # #######################
    # get rid of pesky lists that are still lingering - just pick the first element. This is a bit of a hack, but it works.
    for k in flat_response:
        if isinstance(flat_response[k],list):
            if verbose:
                logger.info(flat_response[k])
            # An empty list carries no value; collapse it to None rather than
            # indexing [0] (which raised IndexError on the occasional response).
            flat_response[k] = flat_response[k][0] if flat_response[k] else None

    return flat_response




def _compress_embedded_repeats(s: str, min_repeats: int = 3, max_unit_len: int = 12) -> str:
    """
    Compress repeated substrings embedded in a larger string.
    Finds the shortest repeating unit at each position that yields the longest run
    (≥ min_repeats), emits as [n]*[unit], and leaves any leftover tail uncompressed.
    
    Args:
      s: input string
      min_repeats: minimum repeats required to compress
      max_unit_len: maximum length of candidate unit to consider
    """
    n = len(s)
    i = 0
    out = []

    while i < n:
        best = None  # (covered_len, repeats, unit_len)
        # Try unit sizes starting from 1 so we prefer the *shortest* valid unit
        for unit_len in range(1, min(max_unit_len, n - i) + 1):
            unit = s[i:i + unit_len]
            # Count contiguous repeats of this unit starting at i
            k = 1
            j = i + unit_len
            while j + unit_len <= n and s[j:j + unit_len] == unit:
                k += 1
                j += unit_len
            if k >= min_repeats:
                covered = k * unit_len
                # Choose the candidate that covers the most chars; if tie, prefer shorter unit
                if best is None or covered > best[0] or (covered == best[0] and unit_len < best[2]):
                    best = (covered, k, unit_len)

        if best:
            covered, k, unit_len = best
            unit = s[i:i + unit_len]
            if len(unit)==1:
                out.append(f"{unit}")
            else:
                out.append(f"[{k}]*[{unit}]")

            i += covered  # skip the compressed run
        else:
            out.append(s[i])
            i += 1

    return "".join(out)



def _decode_valid_unicode_escapes(text, drop_invalid=True):
    """
    Decodes valid Unicode escape sequences (e.g., \\u0026) in a string.

    Args:
        text (str): The input string potentially containing Unicode escape sequences.
        drop_invalid (bool): If True, invalid or incomplete \\u sequences are dropped.
                             If False, they are kept as literal "\\u".

    Returns:
        str: The string with valid Unicode escapes converted to their corresponding characters.
    """


    _hex = re.compile(r"^[0-9a-fA-F]{4}$")

    # Convert only well-formed \uXXXX escapes; keep or remove the rest.
    parts = []
    i = 0
    while i < len(text):
        if text[i:i+2] == r"\u" and i + 6 <= len(text):
            candidate = text[i+2:i+6]
            if _hex.match(candidate):
                parts.append(chr(int(candidate, 16)))
                i += 6
                continue
            elif drop_invalid:
                i += 2  # skip the bad escape entirely
                continue
        if text[i:i+2] == r"\u":
            # broken escape: either double the backslash to keep it literal…
            parts.append(r"\\u")
            i += 2
            continue
        parts.append(text[i])
        i += 1
    return "".join(parts)

    
    
def fuzzy_load_of_json_from_string(resp_text_in: str, notebook_mode = False):
    """
    The model output is a bit unpredictable so this function is doing what it can to figure 
    out the json structure in the string and load it
    """

    resp_text = copy(resp_text_in)

    if type(resp_text)==str and len(resp_text)>0:
        resp_text = resp_text.replace("\n","")
        resp_text = resp_text.replace("```","")
        if resp_text[:4] == "json":
            resp_text = resp_text[4:]
        
        try:
            if resp_text.strip()[0] != "{":
                return None

            refined_text = _compress_embedded_repeats(resp_text, min_repeats = 3, max_unit_len = 12)
            refined_text = refined_text.replace(': null,',": ---,")
            refined_text = refined_text.replace(':null,',': ---,')
            refined_text = refined_text.replace('"null"','---')
            refined_text = refined_text.replace('\\"',"\'")
            refined_text = refined_text.replace("\'\'","\'")
            if "\\u" in refined_text:
                refined_text = _decode_valid_unicode_escapes(refined_text)
                refined_text = refined_text.encode("unicode_escape").decode("ascii")
            
            machine_annotations = fuzzy_json.loads(refined_text)
            
            return machine_annotations
        except Exception as e:
            if notebook_mode:
                logger.warning(f"{e} {refined_text}")
                return refined_text
            return None
    else:
        return None








def flatten_and_fix_machine_outputs(
        raw_outputs_from_machine,
        verbose = False,
        notebook_mode = False):

    if notebook_mode:
        verbose = True
    """
    Transform the output dicts from the video analysis process to fix errors in the response
    Flatten the response and elevate it to the top level of the output dicts
    It expects a dict of dicts with the following structure:
    "h1": {
        "response": <str>,
        "finish_reason": <str>
    },
    ...
    """


    bad_count = 0
    good_count = 0

    flattened_outputs_from_machine = {}
    for h in raw_outputs_from_machine:
        flattened_response = None
        flattened_outputs_from_machine[h] = copy(raw_outputs_from_machine[h])
        if raw_outputs_from_machine[h]['response'] is None or raw_outputs_from_machine[h]['response']=='':
            bad_count += 1
            print("!", end="", flush=True)
        else:
            entry = raw_outputs_from_machine[h]
            if entry.get("structured"):
                # Structured responses are schema-constrained valid JSON: parse
                # directly and use the deterministic structured flattener. Falls
                # back to the fuzzy loader only if the strict parse somehow fails.
                try:
                    json_response = json.loads(entry["response"])
                except (json.JSONDecodeError, TypeError):
                    json_response = fuzzy_load_of_json_from_string(entry["response"], notebook_mode=notebook_mode)
                if isinstance(json_response, dict):
                    flattened_response = flatten_structured(json_response)
                else:
                    flattened_response = None
            else:
                json_response = fuzzy_load_of_json_from_string(entry['response'], notebook_mode = notebook_mode)
                flattened_response = flatten_one_machine_response(json_response, verbose = False, notebook_mode = notebook_mode)
            if type(flattened_response)==dict:
                good_count += 1
                print(".", end="", flush=True)
                for rk in flattened_response:
                    flattened_outputs_from_machine[h][rk] = copy(flattened_response[rk])
            else:
                bad_count += 1
                print("X", end="", flush=True)
                if notebook_mode:
                    logger.error("Error when postprocessing response -> bad response")
                    logger.error(raw_outputs_from_machine[h])
        if (good_count + bad_count) % 100 == 0:
            print()

    if (good_count + bad_count) % 100 != 0:
        print()

    logger.info(f"...extracted {good_count} good responses from the file. Unable to use {bad_count} responses.")

    if good_count == 0:
        return None

    # convert the dict to a DF, reset the index and drop the old response structure 
    outputs_from_machine_df = pd.DataFrame(flattened_outputs_from_machine).T
    outputs_from_machine_df.reset_index(drop=True, inplace=True)
    outputs_from_machine_df.drop("response", axis=1, inplace=True)


    return outputs_from_machine_df










# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# the functions in this section clean up repetititions in the transcripts
# the main function is at the end of the section
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************







def _check_repetitive_patterns(
        text: str,
        min_pattern_length: int = 5,
        min_repetitions: int = 5,
        max_text_length: int = 1000
    ) -> str:
    """
    Check for repetitive patterns in a string
    """


    if not isinstance(text,str):
        return "Not a string"

    if len(text) > max_text_length:
        return "String too long"

    words = text.split()
    n = len(words)
    
    pattern_counts = collections.defaultdict(int)
    
    # Check for all possible pattern lengths from min_pattern_length to half of the total number of words
    for length in range(min_pattern_length, n // 2 + 1):
        for i in range(n - length + 1):
            pattern = tuple(words[i:i + length])
            pattern_counts[pattern] += 1
    
    repetitive_patterns = []
    
    for pattern, count in pattern_counts.items():
        if count >= min_repetitions:
            repetitive_patterns.append((pattern, count))

    if repetitive_patterns:
        return ("Found repetitive patterns", repetitive_patterns)
    else:
        return ("Good string", repetitive_patterns)








def _remove_repetitions(some_string):
    """
    I only use this for the transcriptions which often tend to be a bit repetitive
    """

    new_string = deepcopy(some_string.replace("-"," "))

    res = _check_repetitive_patterns(
        new_string,
        min_pattern_length = 4,
        min_repetitions = 12,
        max_text_length = 10000)
    
    if len(res[1])>0:
        # sort the results with longest repeated pattern first
        most_repeated = sorted(res[1], key = lambda x:len(x[0]), reverse=True)

        # iterate over the patterns. Keep the first occurrence in the string and
        # and remove all other ones. Sometimes this screws things up but it works
        # ok most of the time
        for i,mr in enumerate(most_repeated):
            #print(mr)
            the_phrase = " ".join(mr[0])

            # register the position of the first occurrence of the pattern
            first_occurance = new_string.find(the_phrase)

            # remove all occurrences of the pattern
            new_string = deepcopy(new_string.replace(the_phrase,""))

            # put back the pattern at the position of the first occurrence
            new_string = new_string[:first_occurance] + the_phrase  + new_string[first_occurance:]

            # remove double spaces
            new_string = " ".join([k for k in new_string.split(" ") if len(k)>0])

        # split the string on spaces and remove repetitions of words 
        # again, this gives some probems, but is generally a good thing
        list_of_words = []
        for k in new_string.split(" "):
            if len(list_of_words)==0 or list_of_words[-1] != k:
                list_of_words += [k]

        return new_string

    return some_string





def _prettify_string(a_string):
    new_string = deepcopy(a_string)
    things_to_remove = ["| |"]
    gh = 0
    while gh > -1:
        new_string = " ".join([g for g in new_string.split(" ") if len(g)>0]).strip()
        for ttr in things_to_remove:
            gh = new_string.find(ttr)
            if gh > -1:
                new_string = new_string.replace(ttr,"")
    return new_string






def remove_repetitions_from_transcripts(
    outputs_from_machine_df_in, # expecting a dataframe with a column called "transcript". Elements should be a pipe-separated stringified list.
    verbose = False,
    notebook_mode = False):

    if notebook_mode:
        verbose = True


    if verbose:
        logger.info("Removing repeated patterns in the transcripts - this may take a little while")

    outputs_from_machine_df = outputs_from_machine_df_in.copy()

    new_transcripts = []
    for transcript in outputs_from_machine_df["transcript"].tolist():
        if type(transcript) != str or len(transcript)<50:
            new_transcripts += [copy(transcript)]
        else:
            if " | " in transcript:
                new_scene_transcripts = []
                scene_transcripts = transcript.split(" | ")
                for sc_transcript in scene_transcripts:
                    if len(sc_transcript) < 50:
                        new_scene_transcripts += [copy(sc_transcript)]
                    else:
                        new_scene_transcripts += [_remove_repetitions(sc_transcript)]
                new_transcript = " | ".join(new_scene_transcripts)
            else:
                new_transcript = copy(transcript)

            if len(new_transcript)>=50:
                might_be_shorter = _remove_repetitions(new_transcript)
                if len(might_be_shorter) < len(new_transcript):
                    new_transcript = copy(might_be_shorter)
            
            new_transcripts += [copy(new_transcript)]


    outputs_from_machine_df['transcript_no_repetitions'] = new_transcripts

    if verbose:
        logger.info("Prettifying all strings")
    outputs_from_machine_df = outputs_from_machine_df.map(lambda x:x if not isinstance(x,str) else _prettify_string(x)).copy()

    return outputs_from_machine_df












def clean_up_machine_annotations(some_events, verbose = False):
    



    some_cleaned_up_events = some_events.copy()

    # iterate over all object type columns in the events DF that starts w G_, i.e. are machine annotations
    g_cols = [k for k in some_events.select_dtypes(exclude=["number"]).columns if k not in ["item_id","annotated_ok","annotated_fail"]]
    
    exclude_set = {_cf()['labels']['UNABLE_TO_DETECT'], "", _cf()['labels']['OTHER_THINGS']}

    for c in g_cols:
        # Step 1: Flatten and filter efficiently
        series = some_events[c]
        
        # explode lists to rows
        try:
            exploded = series.explode().dropna()
        except ValueError:
            # PyArrow-backed columns with all-empty lists can cause a length
            # mismatch in pandas explode(); safe to skip.
            continue

        if exploded.empty:
            continue


        # exclude set filtering
        # check against set is fast
        valid_mask = ~exploded.isin(exclude_set)
        valid_items = exploded[valid_mask]
        
        if valid_items.empty:
            continue

        accepted = _cf()['var_schema'].set_index('variable_name').loc[c,'accepted_labels']
        accepted_labels = pd.NA
        if pd.notna(accepted) and accepted.lower() != 'nan' and accepted.startswith('[') and accepted.endswith(']'):
            accepted = accepted[1:-1]
            accepted_labels = [x.strip().replace("//", "").replace("&", " and ").replace("/", " or ") for x in accepted.split(',')]

            pre_fuzzy_nunique = valid_items.nunique()

            # Remember where scalar NAs were so we can restore them after the
            # fuzzy match. recode_fuzzy_match replaces NA values with
            # OTHER_THINGS, but downstream code (e.g. the annotated_ok /
            # annotated_fail flags built from `type_of_story.isna()` at the
            # end of refine_one_raw_annotation_batch) depends on NAs staying
            # NA.
            na_mask = series.isna()

            # Fuzzy-match the WHOLE series (handles lists, scalars, NAs) so
            # that the consolidation and the final writeback below both operate
            # on the normalized values. Running the fuzzy match only on the
            # exploded valid_items (the previous behaviour) left the original
            # series unchanged, causing any value that needed fuzzy matching to
            # fail the keep_set membership check below and be collapsed to
            # OTHER_THINGS.
            series = recode_fuzzy_match(
                list_a=series,
                list_b=accepted_labels,
                threshold=0.8,
                verbose=verbose,
            )

            # Restore NAs that fuzzy matching turned into OTHER_THINGS.
            if na_mask.any():
                series = series.astype(object)
                series[na_mask] = pd.NA

            # Write the normalized series back immediately so columns that
            # have an accepted_labels list always get their fuzzy-match output
            # preserved, even if the consolidation step below is skipped
            # (e.g. avg_len >= 60 or tail too flat).
            some_cleaned_up_events[c] = series

            # Re-derive exploded / valid_items from the now-normalized series
            # for the downstream consolidation step.
            try:
                exploded = series.explode().dropna()
            except ValueError:
                continue

            valid_mask = ~exploded.isin(exclude_set)
            valid_items = exploded[valid_mask]

            if valid_items.empty:
                continue

            if verbose:
                logger.info(f"    {c}: Recoded against accepted labels with fuzzy matching... {valid_items.nunique()} ({pre_fuzzy_nunique})")



        # Check mean length
        # Vectorized string length based on a sample of 500 items.
        # A fixed random_state keeps the length estimate — and the rare-label
        # consolidation decision that branches on it — reproducible run-to-run.
        # Without it the whole refinement pipeline is non-deterministic.

        sample_size = min(500, len(valid_items))
        avg_len = valid_items.sample(sample_size, replace=False, random_state=0).astype(str).str.len().mean()
        
        if avg_len < 60:
            # Step 2: Cutoff logic
            # frequency of unique valid items
            counts = valid_items.value_counts()

            total_count = counts.sum()

            # if we have an accepted list, we want to keep all of them
            if pd.notna(accepted):
                target = total_count * 1
            else:
                target = total_count * 0.95
            
            # cumulative sum
            cum_counts = counts.cumsum()
            
            # find how many labels needed to cross target
            # we keep labels where cumsum < target, plus the one that crosses it
            cutoff_idx = cum_counts.searchsorted(target)
            # ensure at least 3 if possible?
            num_keep = max(3, cutoff_idx + 1)
            # clamp to length
            num_keep = min(num_keep, len(counts))



            # Heuristic: If we are keeping a huge portion of the labels to satisfy the coverage, 
            # or the absolute number of kept labels is huge (e.g. 90k out of 100k), then consolidation is inefficient/useless.
            # User guideline: "if the sum of occurrences of top X labels constitute more than y% ... and there still are a lot of small labels" -> consolidate.
            # But "100k rare labels -> 90k" -> don't consolidate.
            # Logic: If num_keep is > 80% of len(counts) and len(counts) > 1000, skip.
            
            if (len(counts) > 1000) and (num_keep > len(counts) * 0.80):
                 if verbose:
                     logger.info(f"    {c}: Skipping consolidation. Tail is too thick/flat (would keep {num_keep}/{len(counts)}).")
                 continue

            
            okay_list = counts.index[:num_keep].tolist()
            
            # fast lookup set
            keep_set = set(okay_list).union(exclude_set)

            # Step 3: Replacement
            # We need to iterate rows since we want to preserve list structure [[a, b], [c]] -> [[a, OTHER], [c]]
            # A simple map with set lookup is fastest for object columns with lists.
            # NOTE: `series` here is either the original series (when there is
            # no accepted_labels list) or the fuzzy-match-normalized series
            # (when there is). That keeps the membership check against
            # keep_set consistent with how keep_set was built.
            def _fast_replace(x):
                if isinstance(x, (list, np.ndarray)):
                    return [y if y in keep_set else _cf()['labels']['OTHER_THINGS'] for y in x]
                if isinstance(x, str):
                    return x if x in keep_set else _cf()['labels']['OTHER_THINGS']
                return x # keep NA or other

            some_cleaned_up_events[c] = series.apply(_fast_replace)


            if verbose:
                # approximated stats
                logger.info(f"    {c}: Cleaned up rare labels (kept top {num_keep})")

        else:
            if verbose:
                logger.info(f"    {c}: Avg string length > 60, not consolidating rare labels")
        




    return some_cleaned_up_events








# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# the highest level functions
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************


def refine_one_raw_annotation_batch(
    raw_outputs_from_machine = None,
    raw_json_filename = None,
    verbose = False,
    notebook_mode = False):

    if notebook_mode:
        verbose = True


    if raw_json_filename is None:
        raise ValueError("raw_json_filename cannot be None")



    if raw_outputs_from_machine is None:
        if verbose:
            logger.info(f"Loading raw annotations from {raw_json_filename}")
        raw_outputs_from_machine = data_io.load_json(
            storage_location="machine_annotations_raw",
            filename=raw_json_filename,
            verbose=verbose
        )

    if raw_outputs_from_machine is None:
        raise ValueError("raw_outputs_from_machine cannot be None")


    logger.info(f"Refining {len(raw_outputs_from_machine):,} raw annotations in this file...")

    # ---------------------------------------------------------------
    # 1. Flatten the json to a dataframe. Using fuzzy json for this
    # ---------------------------------------------------------------
    logger.info("Transforming the messy json into a flat dataframe")
    outputs_from_machine_df = flatten_and_fix_machine_outputs(raw_outputs_from_machine, verbose = verbose, notebook_mode = notebook_mode)

    if outputs_from_machine_df is None:
        logger.warning("I was unable to extract a single good response from this file. Returning None.")
        logger.warning("Consider deleting this raw file from the raw_annotations folder.")
        return None

    # ---------------------------------------------------------------
    # 2. Consolidate rare columns
    # ---------------------------------------------------------------
    logger.info("Consolidating rare columns from machine annotations.")
    outputs_from_machine_df = consolidate_rare_columns_from_gemini_output(outputs_from_machine_df, verbose = verbose, notebook_mode = notebook_mode)
    logger.info("...done")

    # ---------------------------------------------------------------
    # 3. Remove repetitions from transcripts
    # ---------------------------------------------------------------
    if 'transcript' in outputs_from_machine_df.columns:
        logger.info("Removing repetitions from machine annotation transcripts...")
        outputs_from_machine_df = remove_repetitions_from_transcripts(outputs_from_machine_df, verbose = verbose, notebook_mode = notebook_mode)
        logger.info("...done")

    

    # ---------------------------------------------------------------
    # implement the rules from the variable scheme - recoding lists, strings and other complex data
    # ---------------------------------------------------------------
    # (and a simple renaming of columns to make them easier to identify and read)
    #outputs_from_machine_df = rename_columns(outputs_from_machine_df.rename(columns={c:"G_"+c if not c=="item_id" and not c.startswith("G_") else c for c in outputs_from_machine_df.columns})).copy()
    outputs_from_machine_df = rename_columns(outputs_from_machine_df).copy()
    outputs_from_machine_df = recode_events_df(
            study_dataset = outputs_from_machine_df,
            drop_single_value_cols = False,
            verbose = verbose
            )



    # ---------------------------------------------------------------
    # consolidate some labels in non-numeric columns where that makes sense 
    # ---------------------------------------------------------------
    outputs_from_machine_df = clean_up_machine_annotations(some_events=outputs_from_machine_df, verbose=verbose)




    # ---------------------------------------------------------------
    # add flags for annotated ok and fail
    # ---------------------------------------------------------------
    outputs_from_machine_df["annotated_ok"] = ~outputs_from_machine_df["type_of_story"].isna().astype("bool[pyarrow]")
    outputs_from_machine_df["annotated_fail"] = outputs_from_machine_df["type_of_story"].isna().astype("bool[pyarrow]")
    #outputs_from_machine_df.loc[outputs_from_machine_df[outputs_from_machine_df.annotated_fail].index,[c for c in outputs_from_machine_df.columns if c.startswith("G_")]] = pd.NA


    # ---------------------------------------------------------------
    # Stamp each row with its annotation_version. recode_events_df drops this
    # (it is not a var_schema column), so re-attach it from the raw outputs.
    # Legacy raw files predating versioning have no such field and default to
    # the legacy version.
    # ---------------------------------------------------------------
    version_by_item = {}
    platform_by_item = {}
    for entry in raw_outputs_from_machine.values():
        if isinstance(entry, dict) and entry.get("item_id") is not None:
            version_by_item[str(entry["item_id"])] = entry.get(
                "annotation_version", annotation_versioning.LEGACY_VERSION
            )
            if entry.get("source_platform"):
                platform_by_item[str(entry["item_id"])] = str(entry["source_platform"])
    outputs_from_machine_df["annotation_version"] = (
        outputs_from_machine_df["item_id"].astype(str).map(version_by_item)
        .fillna(annotation_versioning.LEGACY_VERSION)
    )

    # Stamp source_platform the same way (raw files predating multi-platform
    # annotation have no such key and default to the default platform).
    outputs_from_machine_df["source_platform"] = (
        outputs_from_machine_df["item_id"].astype(str).map(platform_by_item)
        .fillna(scrape_queues.default_platform())
    )


    # ---------------------------------------------------------------
    # Convert dtypes to pyarrow and reset index
    # ---------------------------------------------------------------
    outputs_from_machine_df.reset_index(drop=True, inplace=True)
    outputs_from_machine_df = convert_dtypes_to_pyarrow(outputs_from_machine_df, verbose=verbose)


    if verbose:
        logger.info("Ready to save processed results")

    parquet_filename = raw_json_filename.replace(".json", ".parquet")

    data_io.save_parquet(
        df = outputs_from_machine_df,
        storage_location="machine_annotations_refined",
        filename=parquet_filename,
        verbose=verbose
    )
    logger.info(f"Saved processed the df - shape {outputs_from_machine_df.shape} - results to '{parquet_filename}'")
    logger.info("--"*60)
    
    return outputs_from_machine_df
    




def refine_and_save_all_raw_annotation_files(verbose = False, notebook_mode = False, force = False):

    result = {}

    raw_annotation_files = [fn for fn in data_io.listdir(storage_location="machine_annotations_raw") if fn.startswith(_machine_annotations_label()) and fn.endswith(".json")]
    result["raw_files"] = len(raw_annotation_files)

    refined_annotation_files = [fn for fn in data_io.listdir(storage_location="machine_annotations_refined") if fn.startswith(_machine_annotations_label()) and fn.endswith(".parquet")]
    result["refined_files_before"] = len(refined_annotation_files)

    if force:
        # Re-refine every raw file regardless of whether a refined parquet
        # already exists. Use this after a fix to the refinement pipeline that
        # invalidates the cached refined files.
        raw_files_up_for_refinement = list(raw_annotation_files)
    else:
        raw_files_up_for_refinement = [g for g in raw_annotation_files if g.replace(".json",".parquet") not in refined_annotation_files]
    if verbose:
        if force:
            logger.info(f"Force mode: re-refining all {len(raw_files_up_for_refinement)} raw files (ignoring {len(refined_annotation_files)} existing refined files)")
        else:
            logger.info(f"{len(refined_annotation_files)} raw annotation files have already been refined")
            logger.info(f"{len(raw_files_up_for_refinement)} files are up for refinement")

    for i,fn in enumerate(raw_files_up_for_refinement):
        if verbose:
            logger.info(f"\n{i+1}/{len(raw_files_up_for_refinement)} {fn}")
        refine_one_raw_annotation_batch(
            raw_outputs_from_machine = None,
            raw_json_filename = fn,
            verbose = verbose,
            notebook_mode = notebook_mode
            )

    refined_annotation_files = data_io.listdir(
        storage_location="machine_annotations_refined",
        return_absolute_path=False,
        verbose=False)
    refined_annotation_files = [u for u in refined_annotation_files if u.endswith(".parquet")]
    result["refined_files_after"] = len(refined_annotation_files)

    return result










def consolidate_and_save_refined_annotations(
    force_consolidation = False,
    return_saved_data = True,
    verbose = False,
    ):


    top_verbose = True

    # ---------------------------------------------------------------
    if top_verbose:
        logger.info("Checking for raw annotation batches that needs refining...")
    # check if there are any raw files that need refining and refine those
    result = refine_and_save_all_raw_annotation_files(verbose = verbose, notebook_mode = False)
    if top_verbose:
        if result["refined_files_after"] == result["refined_files_before"]:
            logger.info("    ...all files already refined.")
        else:
            logger.info(f"    ...refined {result['refined_files_after'] - result['refined_files_before']} files.")


    # ---------------------------------------------------------------
    # check if there are any changes in the relevant folder compared to last time this process was run.    
    if data_io.exists(storage_location="recoded",filename="consolidated_enrichment_files.json",verbose=verbose):
        dataset_meta = data_io.load_json(storage_location="recoded",filename="consolidated_enrichment_files.json",verbose=verbose)
        if verbose:
            logger.info("Dataset meta loaded")
    else:
        dataset_meta = {"machine_annotations": {"filenames": []}}

    files_to_concatenate = []
    for fn in data_io.listdir(storage_location="machine_annotations_refined"):
        if fn.startswith(_machine_annotations_label()) and fn.endswith(".parquet"):
            files_to_concatenate.append(fn)

    latest_filename_list = dataset_meta.get("machine_annotations", {}).get("filenames", [])

    # if all files found in the refine folder are already registered in the dataset meta, then no need to consolidate
    if not force_consolidation and set(files_to_concatenate) <= set(latest_filename_list):
        if top_verbose:
            logger.info("No new refined machine annotations files found. No need to consolidate.")
        if return_saved_data:
            if data_io.exists(storage_location="recoded", filename=f"{_machine_annotations_label()}_recoded.parquet"):
                if verbose: logger.info("Returning existing file.")
                return False, data_io.load_parquet(storage_location="recoded", filename=f"{_machine_annotations_label()}_recoded.parquet"), set()
            if verbose: logger.info("No existing consolidated file — returning empty.")
            return False, pd.DataFrame(), set()
        return False, None, set()
    
 
    # ---------------------------------------------------------------
    # load all refined files
    if top_verbose:
        logger.info("Loading refined annotation files...")
    refined_annotation_dfs = []
    for fn in files_to_concatenate:
        df = data_io.load_parquet(storage_location="machine_annotations_refined", filename=fn)
        refined_annotation_dfs.append(df)
        if verbose:
            logger.info(f"{fn} {df.shape}")

    
    # ---------------------------------------------------------------
    if top_verbose:
        logger.info(f"Consolidating {len(refined_annotation_dfs):,} refined files (keeping latest version of each item_id)...")
    consolidated_annotations = pd.concat(refined_annotation_dfs, ignore_index=True)

    # ---------------------------------------------------------------
    # Version-aware consolidation. Every row carries an annotation_version; rows
    # from legacy refined files predating versioning default to the legacy
    # version. The full multi-version history is archived (queryable, never
    # overwritten); the active dataset that downstream consumers read is then
    # derived from the promoted version, or — when nothing is promoted yet —
    # the latest annotation per item (identical to the historical behaviour).
    if "annotation_version" not in consolidated_annotations.columns:
        consolidated_annotations["annotation_version"] = annotation_versioning.LEGACY_VERSION
    consolidated_annotations["annotation_version"] = (
        consolidated_annotations["annotation_version"].fillna(annotation_versioning.LEGACY_VERSION)
    )

    # Backfill source_platform: refined files predating multi-platform
    # annotation carry no platform column (all TikTok-era rows). Item ids are
    # only guaranteed unique within a platform, so all annotation keying below
    # is composite (source_platform, item_id).
    if "source_platform" not in consolidated_annotations.columns:
        consolidated_annotations["source_platform"] = scrape_queues.default_platform()
    consolidated_annotations["source_platform"] = (
        consolidated_annotations["source_platform"].fillna(scrape_queues.default_platform())
    )

    annotation_archive = consolidated_annotations.drop_duplicates(
        subset=["source_platform", "item_id", "annotation_version"], keep="last"
    ).reset_index(drop=True)
    data_io.save_parquet(
        df=annotation_archive,
        storage_location="recoded",
        filename=f"{_machine_annotations_label()}_all_versions.parquet",
        verbose=verbose,
    )
    # Record which annotation versions the archive actually contains, so the
    # legacy-metadata union (and therefore the var_schema hash) is pruned to
    # versions that can occur in the data. NOTE: a consolidation that shrinks
    # this set changes the schema hash and marks studies for rebuild.
    annotation_versioning.record_versions_in_data(
        annotation_archive["annotation_version"].dropna().unique()
    )

    active_version = annotation_versioning.get_active_version()
    if active_version is None:
        # No version promoted yet: keep the most recent annotation per item
        # (the historical, version-agnostic behaviour — zero migration change).
        consolidated_annotations = consolidated_annotations.drop_duplicates(
            subset=["source_platform", "item_id"], keep="last"
        ).reset_index(drop=True)
    else:
        consolidated_annotations = annotation_versioning.select_active_view(
            consolidated_annotations, active_version
        )

    memory_per_column = consolidated_annotations.memory_usage(deep=True) 
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    if top_verbose:
        logger.info(f"Shape: {consolidated_annotations.shape} | Memory usage: {total_memory_mb:.2f} MB")

    # ---------------------------------------------------------------
    # Compute changed item_ids: IDs from newly added files that were not in the
    # previous consolidation file list (even re-annotations count as changes).
    # When force_consolidation is True, treat ALL items as changed.
    existing_recoded_fn = f"{_machine_annotations_label()}_recoded.parquet"
    new_item_ids: set[str] = set()
    if force_consolidation:
        new_item_ids = set(consolidated_annotations["item_id"])
        if top_verbose:
            logger.info(f"Force consolidation: all {len(new_item_ids):,} item_ids treated as changed.")
    else:
        new_files = set(files_to_concatenate) - set(latest_filename_list)
        if new_files:
            for fn, df in zip(files_to_concatenate, refined_annotation_dfs):
                if fn in new_files:
                    new_item_ids.update(df["item_id"].tolist())
        if top_verbose and new_item_ids:
            logger.info(f"Found {len(new_item_ids):,} changed/newly annotated item_ids from {len(new_files)} new file(s).")

    # ---------------------------------------------------------------
    # save the consolidated annotations
    if top_verbose:
        logger.info("Saving consolidated annotations...")
    data_io.save_parquet(
        df=consolidated_annotations,
        storage_location="recoded", filename=existing_recoded_fn, verbose=verbose)
    if top_verbose:
        logger.info("...done")

    # ---------------------------------------------------------------
    # update the dataset meta file
    if "machine_annotations" not in dataset_meta:
        dataset_meta["machine_annotations"] = {}
    dataset_meta["machine_annotations"]["filenames"] = files_to_concatenate
    _ = data_io.save_json(data = dataset_meta, storage_location="recoded", filename="consolidated_enrichment_files.json")

    return True, consolidated_annotations, new_item_ids





def rebuild_active_annotations_from_archive(verbose: bool = False):
    """Rebuild the active recoded annotations from the version archive.

    Fast path used after promoting a version: re-derives
    ``machine_annotations_recoded.parquet`` from the already-built
    ``machine_annotations_all_versions.parquet`` using the current active
    version (or latest-per-item when nothing is promoted) — no re-refinement of
    raw files. Per-study cached datasets still need a study refresh to pick up
    the change; this only updates the global active dataset.

    Args:
        verbose: Whether to print I/O progress.

    Returns:
        The number of rows in the rebuilt active dataset, or ``None`` if the
        archive is missing/empty.
    """
    archive_fn = f"{_machine_annotations_label()}_all_versions.parquet"
    recoded_fn = f"{_machine_annotations_label()}_recoded.parquet"
    if not data_io.exists(storage_location="recoded", filename=archive_fn):
        return None
    archive = data_io.load_parquet(storage_location="recoded", filename=archive_fn)
    if archive is None or archive.empty:
        return None

    active_version = annotation_versioning.get_active_version()
    dedup_cols = (
        ["source_platform", "item_id"] if "source_platform" in archive.columns else ["item_id"]
    )
    if active_version is None:
        active_df = archive.drop_duplicates(subset=dedup_cols, keep="last").reset_index(drop=True)
    else:
        active_df = annotation_versioning.select_active_view(archive, active_version)

    data_io.save_parquet(
        df=active_df, storage_location="recoded", filename=recoded_fn, verbose=verbose
    )
    return len(active_df)






def platform_map_for(item_ids: list[str]) -> dict[str, str]:
    """Map item ids to their ``source_platform`` via enrichment_status.parquet.

    Used by the annotation entry points to resolve each queued item's platform
    (the annotation queue stores bare ids). Ids missing from the status file are
    simply absent from the map — callers fall back to the default platform, and
    ``media_paths.resolve_media`` probes the other platforms' subpaths anyway.
    Never raises.

    Args:
        item_ids: The item ids to look up.

    Returns:
        ``{item_id: source_platform}`` for the ids that could be resolved.
    """
    try:
        if not data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
            return {}
        status_df = data_io.load_parquet_selective(
            storage_location="recoded", filename="enrichment_status.parquet",
            columns=["item_id", "source_platform"],
        )
        if "source_platform" not in status_df.columns:
            return {}
        wanted = {str(i) for i in item_ids}
        ids = status_df["item_id"].astype(str)
        mask = ids.isin(wanted) & status_df["source_platform"].notna()
        return dict(zip(ids[mask], status_df.loc[mask, "source_platform"].astype(str)))
    except Exception as e:
        logger.warning(f"WARNING: platform map lookup failed ({e}); falling back to default platform.")
        return {}






def annotate_from_video_id_list(
    fine_list = None,
    max_workers = 50,
    refine_after_annotation = True,
    verbose = False,
    notebook_mode = False,
    dry_run = False,
    batch_label: str | None = None,
    cumulative_done: int = 0,
    cumulative_total: int = 0,
    cumulative_ok: int = 0,
    cumulative_fail: int = 0,
    reporter=None,
    platform_by_id: dict[str, str] | None = None):

    if notebook_mode:
        verbose = True
    """
    This function takes a list of video IDs and calls the machine to annotate them.
    It also performs the necessary post processing of the raw outputs from the machine.
    """

    initialize_machine()


    if dry_run:
        logger.info("********* This is a dry run. It's all fake. No data io action at all. *********")


    if isinstance(fine_list, list) and len(fine_list) > 0:

        # Sanity check against corrupt lists (NaN / paths / URLs) — id shapes
        # differ per platform (TikTok 19-digit numeric, Instagram shortcode,
        # YouTube 11-char [A-Za-z0-9_-]), so the check is deliberately permissive.
        if not all(map(lambda video_id: type(video_id) == str and re.fullmatch(r"[A-Za-z0-9_-]{5,40}", video_id), fine_list)):
            raise ValueError("Some videoIDs in the list were corrupt. Cannot process this list.")

        if platform_by_id is None and not dry_run:
            platform_by_id = platform_map_for(fine_list)

        logger.info("Annotating videos...")

        raw_outputs_from_machine, raw_json_fn = call_machine_threads(
                interesting_videos = fine_list,
                max_workers=max_workers,
                verbose = verbose,
                notebook_mode = notebook_mode,
                dry_run = dry_run,
                batch_label=batch_label,
                cumulative_done=cumulative_done,
                cumulative_total=cumulative_total,
                cumulative_ok=cumulative_ok,
                cumulative_fail=cumulative_fail,
                reporter=reporter,
                platform_by_id=platform_by_id,
            )

        logger.info("...video annotation completed.")

        if dry_run:
            logger.info("Since this is a dry run I'm skipping the refinement step.")
            return [], []

        if refine_after_annotation:
            refined_df = refine_one_raw_annotation_batch(
                raw_outputs_from_machine = raw_outputs_from_machine,
                raw_json_filename = raw_json_fn,
                verbose = verbose, notebook_mode = notebook_mode)

            # Refinement can return None when flatten_and_fix_machine_outputs
            # fails for the entire batch. In that case we cannot tell which
            # items succeeded, so return empty lists — the caller will leave
            # the queue untouched and the items will be retried next run.
            if refined_df is None or refined_df.empty:
                return [], []

            if {"item_id", "annotated_ok", "annotated_fail"}.issubset(refined_df.columns):
                ok_ids = refined_df.loc[refined_df["annotated_ok"].fillna(False).astype(bool), "item_id"].astype(str).tolist()
                fail_ids = refined_df.loc[refined_df["annotated_fail"].fillna(False).astype(bool), "item_id"].astype(str).tolist()
                return ok_ids, fail_ids

            return [], []

        return [], []

    else:
        if verbose:
            logger.info("No videos to process")
        return [], []














def queue_annotation_loop(
    batch_size = 500,
    max_batches = None,
    verbose = False,
    dry_run = False,
    reporter=None,
    cancellation_check=None,
):

    import fyp.data_io as data_io
    target_cache_file = "to_annotate.json"
    
    if not data_io.exists(storage_location="cache", filename=target_cache_file):
        logger.error(f"    ERROR: Could not find target file '{target_cache_file}' in cache. Make sure you calculated targets first.")
        return None

    video_list = data_io.load_json(storage_location="cache", filename=target_cache_file)
    
    if not video_list or len(video_list) == 0:
        logger.info(f"    No videos to annotate found in '{target_cache_file}'.")
        return None

    logger.info(f"    Loaded {len(video_list)} videos from queue '{target_cache_file}'")

    return annotate_videos_loop_from_list(
        video_list = video_list,
        batch_size = batch_size,
        max_batches = max_batches,
        verbose = verbose,
        dry_run = dry_run,
        reporter = reporter,
        cancellation_check = cancellation_check,
    )









def annotate_videos_loop_from_list(
    video_list = None,
    batch_size = 500,
    max_batches = None,
    verbose = False,
    dry_run = False,
    reporter=None,
    cancellation_check=None,
    ):



    max_batches = max_batches if max_batches is not None else np.inf

    if video_list is None:
        logger.error("    ERROR: The annotation loop cannot run without a video list as input. Process failed.")
        return None

    initialize_machine()
    


    logger.info(f"    Annotating selected videos, batch size: {batch_size}, max batches: {max_batches}")
    logger.info(f"    Now: {_dt.datetime.now()}")

    batch_number = 1
    cumulative_done = 0
    cumulative_ok = 0
    cumulative_fail = 0

    batch_target = min(max_batches, len(video_list) // batch_size + 1)
    total_items = min(len(video_list), batch_target * batch_size)

    logger.info(f"  Starting loop... There are {total_items:,} videos to process in {batch_target:,} batches")

    target_cache_file = "to_annotate.json"

    for batch in fyp_utils.chunk_list(video_list, batch_size):

        batch_label = f"{batch_number}/{batch_target}"
        logger.info(f"  Batch {batch_label}")

        ok_ids, fail_ids = annotate_from_video_id_list(
            fine_list = batch,
            verbose = verbose,
            dry_run = dry_run,
            batch_label=batch_label,
            cumulative_done=cumulative_done,
            cumulative_total=total_items,
            cumulative_ok=cumulative_ok,
            cumulative_fail=cumulative_fail,
            reporter=reporter,
        )

        cumulative_done += len(batch)
        cumulative_ok += len(ok_ids)
        cumulative_fail += len(fail_ids)

        # Prune successful + failed items from the on-disk queue so it stays
        # in sync with reality. Mirrors the scraper's prune in
        # run_queue_scraper.py:133. Skipped for dry_run since nothing was
        # actually annotated.
        queue_remaining = len(video_list) - cumulative_done
        if not dry_run and data_io.exists(storage_location="cache", filename=target_cache_file):
            items_to_remove = set(ok_ids) | set(fail_ids)
            prune_counts = {}

            def _prune(fresh_queue):
                fresh_queue = fresh_queue if isinstance(fresh_queue, list) else []
                updated_queue = [v for v in fresh_queue if v not in items_to_remove]
                prune_counts["after"] = len(updated_queue)
                if len(updated_queue) == len(fresh_queue):
                    return None  # nothing pruned — skip the write
                return updated_queue

            # Atomic prune: ids appended by the web service while this batch
            # ran are never clobbered.
            data_io.update_json(
                storage_location="cache",
                filename=target_cache_file,
                mutate=_prune,
                default=[],
            )
            queue_remaining = prune_counts.get("after", queue_remaining)

        if reporter is not None:
            reporter.emit_data({"annotate_queue_len": max(0, queue_remaining)})
        elif "WEB_INTERFACE" in os.environ:
            # STDOUT PROTOCOL — MUST stay print(). process_manager.enqueue_output()
            # parses subprocess stdout for the ::DATA:: marker; never convert to logging.
            print(f"::DATA::{{\"annotate_queue_len\": {max(0, queue_remaining)}}}", flush=True)

        if max_batches is not None and batch_number >= max_batches:
            break

        # Check for graceful stop request
        if cancellation_check is not None:
            if cancellation_check():
                logger.info("  Cancellation requested. Finishing after this batch.")
                break
        elif _check_graceful_stop("queue_annotator"):
            logger.info("  Graceful stop requested. Finishing after this batch.")
            break

        batch_number += 1

        if dry_run:
            break

    logger.info(f"Loop ended: {_dt.datetime.now()}")













# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************


