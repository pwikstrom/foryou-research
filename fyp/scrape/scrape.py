#!/usr/bin/env python3
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""



import json
import os
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image, ImageColor

import fyp.data_io as data_io
import fyp.media_paths as media_paths
from fyp.logging_setup import get_logger

# Sibling imports go through the package (never the old-path shims): a
# shim import here could bind a
# partially-initialized shim during the boot cascade (shim-poisoning rule,
# docs/fyp-import-graph.md).
from fyp.scrape import scrape_contract as sc
from fyp.scrape import scrape_queues, scrape_versioning, scraper_alerts
from fyp.scrape.platform_scraper import (
    SLIDESHOW_SECONDS_PER_IMAGE,
    THROTTLE_CATEGORIES,
    ThrottleController,
    get_scraper,
)
from fyp.recode_variables import recode_events_df, rename_columns
from fyp.utils import chunk_list, record_dropped_columns, start_monitor

logger = get_logger(__name__)


def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf




def _scrapes_label() -> str:
    """Lazy accessor for the config-derived scrapes label."""
    return _cf()["labels"]["SCRAPES_LABEL"]




def _failed_scrapes_label() -> str:
    """Lazy accessor for the config-derived failed-scrapes label."""
    return _cf()["labels"]["FAILED_SCRAPES_LABEL"]




_CONFIG_CONSTANT_ACCESSORS = {
    "SCRAPES_LABEL": _scrapes_label,
    "FAILED_SCRAPES_LABEL": _failed_scrapes_label,
    "MEMORY_STOP_FRACTION": lambda: _memory_stop_fraction(),
    "SLIDESHOW_MAX_DIMENSION": lambda: _slideshow_max_dimension(),
    "PERMANENT_STORM_THRESHOLD": lambda: _permanent_storm_threshold(),
    "TRANSIENT_STORM_THRESHOLD": lambda: _transient_storm_threshold(),
}




def __getattr__(name: str):
    """Serve the config-derived module constants lazily (PEP 562)."""
    accessor = _CONFIG_CONSTANT_ACCESSORS.get(name)
    if accessor is not None:
        return accessor()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")



# Consecutive throttle-category failures (across all workers) that trip the
# batch circuit breaker in download_video_threads.
CIRCUIT_BREAKER_THRESHOLD = 15

# Consecutive same-category *permanent* failures that mark the batch outcome as
# suspect (a "permanent storm"). A broken/flagged session can make every item
# fail with the same permanent classification (e.g. Instagram returning 404 for
# live posts → "removed"); pruning those ids from the queue would be wrong and
# hard to recover from. A healthy queue produces heterogeneous outcomes, so a
# long homogeneous run of one permanent category aborts the batch instead, and
# the affected ids are demoted to transient (kept queued, not recorded as
# failed). Overridable via ``[misc] scraper_permanent_storm_threshold``.
def _permanent_storm_threshold() -> int:
    """Lazy accessor for the permanent-storm guard threshold (see comment above)."""
    try:
        return int(_cf()["misc"].get("scraper_permanent_storm_threshold", 15))
    except (KeyError, TypeError, ValueError):
        return 15

# Consecutive same-category *transient* failures that mark the batch outcome as
# suspect (a "transient storm"). The permanent-storm guard's blind spot: a
# platform-side breakage classified on the retryable side (2026-08-10: TikTok
# deployed a new bot-challenge wall that made yt-dlp fail every item with
# "No video formats found!" → "unknown", a retryable category) neither trips the
# rate-limit circuit breaker nor the permanent-storm guard, so the worker churns
# the whole queue at 0% yield, burning 3 yt-dlp attempts per item and
# self-chaining until the queue stops pruning. A long homogeneous run of one
# transient category aborts the batch and stops chaining instead; the items are
# already transient so they simply stay queued — no demotion needed. The
# threshold is higher than the permanent guard's: transient runs (network
# blips) are more plausible in a healthy session, and any success resets the
# count. Overridable via ``[misc] scraper_transient_storm_threshold``.
def _transient_storm_threshold() -> int:
    """Lazy accessor for the transient-storm guard threshold (see comment above)."""
    try:
        return int(_cf()["misc"].get("scraper_transient_storm_threshold", 25))
    except (KeyError, TypeError, ValueError):
        return 25

# Fraction of the container memory limit at which a batch stops launching new
# downloads, drains in-flight work, saves what completed, and defers the rest
# back to the queue (see the memory safety valve in download_video_threads).
# Last-line insurance against any unexpected memory spike: without it the
# container is OOM-killed mid-drain and the whole batch is lost — the scrapers
# are deliberately single-attempt (not in process_routes.QUEUE_RETRY_SAFE), so
# nothing re-delivers it. (The historical spike source — moviepy slideshow
# assembly at native photo resolution — is bounded at the root by
# SLIDESHOW_MAX_DIMENSION in make_slideshow.)
def _memory_stop_fraction() -> float:
    """Lazy accessor for the memory safety-valve threshold (see comment above)."""
    try:
        return float(_cf()["misc"].get("scraper_memory_stop_fraction", 0.60))
    except (KeyError, TypeError, ValueError):
        return 0.60

# Longest edge of a slideshow canvas, in pixels. TikTok photo-mode source
# images run up to 2160x3840; rendering the slideshow at that native size makes
# moviepy hold ~15 GiB for a 20-image post (frame buffers scale with canvas
# area), which OOM-pressured the task-runner. Slideshows are display media, not
# archival: capping the longest edge bounds the whole moviepy/ffmpeg pipeline
# to well under 1 GiB. Overridable via ``[misc] slideshow_max_dimension``.
def _slideshow_max_dimension() -> int:
    """Lazy accessor for the slideshow canvas cap (see comment above)."""
    try:
        return int(_cf()["misc"].get("slideshow_max_dimension", 1000))
    except (KeyError, TypeError, ValueError):
        return 1000





def _container_memory_fraction() -> "tuple[float, float] | None":
    """Return ``(used_fraction, used_gib)`` for the container's memory cgroup.

    Reads the cgroup's own accounting (cgroup v2 ``memory.current`` /
    ``memory.max``, falling back to v1), which is exactly what the Cloud Run
    OOM-killer watches — it includes the ``/tmp`` tmpfs and child processes
    (ffmpeg), unlike process RSS. Returns ``None`` when the files are absent
    (local dev) or the limit is unset, which disables the memory guard.

    Returns:
        ``(used_fraction, used_gib)`` where ``used_fraction`` is usage / limit
        in ``[0, 1]`` and ``used_gib`` is absolute usage in GiB, or ``None``.
    """
    _gib = float(1 << 30)
    try:
        with open("/sys/fs/cgroup/memory.max") as fh:
            raw = fh.read().strip()
        if raw != "max":
            limit = int(raw)
            with open("/sys/fs/cgroup/memory.current") as fh:
                usage = int(fh.read().strip())
            if limit > 0:
                return usage / limit, usage / _gib
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as fh:
            limit = int(fh.read().strip())
        with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as fh:
            usage = int(fh.read().strip())
        # cgroup v1 reports a near-INT64_MAX sentinel when the limit is unset.
        if 0 < limit < (1 << 62):
            return usage / limit, usage / _gib
    except (OSError, ValueError):
        pass
    return None





def _check_graceful_stop(process_name: str) -> bool:
    """Check if a graceful stop has been requested via sentinel file."""
    sentinel = Path(_cf()['paths']['project_root']) / "tmp" / "graceful_stop" / f"{process_name}.stop"
    return sentinel.exists()



def make_slideshow(
    files: list[str],
    output: str = "slideshow.mp4",
    duration: float = 3.0,
    transition: float = 0.6,
    swipe: bool = True,
    canvas_size: tuple[int, int] = None,  # auto if None
    bg_color: str | tuple[int, int, int] = "#000000",
    fps: int = 1,
    codec: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    audio_path: str | None = None,
    verbose=False
):
    """Create a slideshow video from a list of image files.

    Args:
        files: image file paths, in slide order.
        output: output mp4 path.
        duration: seconds each image is shown.
        transition: swipe transition length in seconds (capped at duration).
        swipe: animate each slide in with a horizontal swipe.
        canvas_size: output (width, height); inferred from the images when None.
            Always clamped so the longest edge is at most
            ``SLIDESHOW_MAX_DIMENSION`` (moviepy frame buffers scale with
            canvas area — an uncapped 2160x3840 canvas costs ~15 GiB).
        bg_color: letterbox background color (name/hex string or RGB tuple).
        fps: output frame rate.
        codec: video codec passed to ffmpeg.
        crf: constant rate factor (quality) passed to ffmpeg.
        preset: ffmpeg encoder preset.
        audio_path: optional audio file muxed under the slideshow; trimmed to
            the video length when longer, ends early when shorter. Any audio
            problem degrades to a silent slideshow rather than failing.
        verbose: unused; kept for call-site symmetry.
    """
    from moviepy import AudioFileClip, ColorClip, CompositeVideoClip, ImageClip, concatenate_videoclips
    

    def _normalize_color(color): 
        if isinstance(color, str):
            try:
                rgb = ImageColor.getrgb(color)
            except ValueError as exc:
                raise ValueError(f"Invalid bg_color {color!r}") from exc
            return tuple(int(c) for c in rgb)
        if isinstance(color, Sequence) and len(color) == 3:
            try:
                return tuple(int(c) for c in color)
            except (TypeError, ValueError) as exc:
                raise ValueError("bg_color tuple must contain numeric values") from exc
        raise TypeError("bg_color must be a color string or an RGB tuple of length 3")



    def _load_fitted(image_path: str, canvas_size: tuple[int, int]) -> np.ndarray:
        # Decode and downscale with PIL *before* the image enters moviepy, so
        # ImageClip holds a canvas-sized array instead of the native-resolution
        # photo and no per-frame Resize effect is needed.
        W, H = canvas_size
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            scale = min(W / im.width, H / im.height)
            target = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
            if target != im.size:
                im = im.resize(target, Image.LANCZOS)
            return np.asarray(im)



    def _make_swipe_pos(width, transition):
        def pos(t):
            if transition <= 0:
                return (0, "center")
            if t <= transition:
                x = width * (1 - t / transition)
            else:
                x = 0
            return (x, "center")
        return pos



    def _build_slide(
        image_path: str,
        duration: float,
        canvas_size: tuple[int, int],
        bg_color,
        swipe: bool,
        transition: float,
    ):
        bg = ColorClip(size=canvas_size, color=bg_color, duration=duration)
        boxed = ImageClip(_load_fitted(image_path, canvas_size), duration=duration)

        if swipe and transition > 0:
            pos = _make_swipe_pos(canvas_size[0], transition)
            animated = boxed.with_position(pos)
        else:
            animated = boxed.with_position(("center", "center"))

        slide = CompositeVideoClip([bg, animated]).with_duration(duration)
        return slide



    def _infer_canvas_size(files: list[str]) -> tuple[int, int]:
        widths = []
        heights = []
        for f in files:
            try:
                with Image.open(f) as im:
                    w, h = im.size
                    widths.append(w)
                    heights.append(h)
            except Exception:
                pass

        if not widths or not heights:
            return (1920, 1080)

        return (max(widths), max(heights))



    def _clamp_canvas(size: tuple[int, int]) -> tuple[int, int]:
        # Bound the longest edge, then round down to even dimensions
        # (libx264 with yuv420p rejects odd frame sizes).
        w, h = size
        longest = max(w, h)
        if longest > _slideshow_max_dimension():
            scale = _slideshow_max_dimension() / longest
            w = round(w * scale)
            h = round(h * scale)
        return (max(2, w - (w % 2)), max(2, h - (h % 2)))



    # Main function logic starts here
    if not files:
        raise ValueError("No input files provided")

    bg_color = _normalize_color(bg_color)

    if canvas_size is None:
        canvas_size = _infer_canvas_size(files)
    canvas_size = _clamp_canvas(canvas_size)

    transition = max(0.0, min(transition, duration))

    slides = []
    for f in files:
        slide = _build_slide(
            f,
            duration,
            canvas_size,
            bg_color,
            swipe,
            transition
        )
        slides.append(slide)

    final = concatenate_videoclips(slides, method="compose")

    audio_clip = None
    if audio_path and os.path.exists(audio_path):
        try:
            audio_clip = AudioFileClip(audio_path)
            if audio_clip.duration and audio_clip.duration > final.duration:
                audio_clip = audio_clip.subclipped(0, final.duration)
            final = final.with_audio(audio_clip)
        except Exception:
            audio_clip = None  # degrade to a silent slideshow

    final.write_videofile(
        output,
        fps=fps,
        codec=codec,
        audio=final.audio is not None,
        audio_codec="aac",
        # moviepy's default temp-audio filename is not unique per process/thread;
        # derive it from the (unique) output path to survive concurrent workers.
        temp_audiofile=f"{output}.TEMP_audio.m4a",
        preset=preset,
        threads=0,
        ffmpeg_params=["-crf", str(crf)],
        logger=None
    )

    for s in slides:
        s.close()
    if audio_clip is not None:
        audio_clip.close()
    final.close()








def _scrape_future_succeeded(f) -> bool:
    """True only when a scrape future returned a NON-empty result row.

    The download workers return ``(idx, df)``; a failed fetch returns an empty
    DataFrame tagged with the error (``empty_fail``), which is still a
    DataFrame. The live monitor uses this to count genuine successes — counting
    empty fail-frames as OK made the "N OK · 0 fail" progress overstate success
    (it really meant "N finished"). Never raises: a future that errored counts
    as not-succeeded.
    """
    try:
        res = f.result()[1]
    except Exception:
        return False
    return isinstance(res, pd.DataFrame) and not res.empty


def download_single_video(
    video_id: str = None,
    verbose: bool = True,
    save_video = True,
    dry_run: bool = False,
    scraper=None,
    platform: str | None = None,
    ):


    if dry_run:
        from time import sleep
        sleep(1)
        if verbose:
            logger.info(f"Dry run: would have downloaded video {video_id}")
        return video_id


    if video_id is None:
        raise ValueError("No video id specified")

    use_gcs = _cf()['data_io']['use_gcs_for_media']
    bucket = _cf()['data_io']['bucket']
    min_size = _cf()["misc"]["min_media_object_size"]
    temp_dir = _cf()["paths"]["temp"]

    if use_gcs and bucket is None:
        raise ValueError("No GCS bucket specified")

    if scraper is None:
        scraper = get_scraper(platform, verbose=verbose)

    # Media lives under a per-platform subpath ({prefix}/{platform}/{id}.mp4);
    # legacy flat files are found by the reader-side fallback, never written.
    media_prefix = f"{_cf()['data_io']['gcs_media_prefix']}/{scraper.platform}"
    platform_media_dir = (
        media_paths.ensure_local_platform_dir(scraper.platform) if not use_gcs else ""
    )

    # Routing for fetch: GCS mode -> bucket + gcs prefix; local mode -> local dir, no bucket
    save_path_arg = media_prefix if use_gcs else platform_media_dir
    stream_to_bucket_arg = bucket if use_gcs else None

    # try to scrape metadata and download media via the platform scraper
    # (backend selection + URL construction live in the subclass)
    scrape_metadata = scraper.fetch(
        video_id,
        save_media=save_video,
        save_path=save_path_arg,
        stream_to_bucket=stream_to_bucket_arg,
        verbose=verbose,
    )

    try:
        # if there are columns in the result and a something has been downloaded
        col_count = len(scrape_metadata.columns)
        if col_count > 1 and scrape_metadata.loc[0,'video_downloaded']==True:

            # if this is an image post (platform-agnostic hook; 0 for platforms
            # without a carousel concept)
            if scraper.image_count(scrape_metadata.iloc[0]) > 0:
                if verbose:
                    logger.info(f"OK   - Photos downloaded - '{video_id}' - {col_count} metadata fields")

                if use_gcs:
                    # GCS path: check bucket, download jpegs to temp, assemble, upload
                    blob = bucket.blob(f"{media_prefix}/{video_id}.mp4")
                    if blob.exists():
                        if verbose:
                            logger.info("Photo slideshow already in bucket")
                        scrape_metadata.loc[0,'video_downloaded'] = True
                    else:
                        if verbose:
                            logger.info("Converting photos to video slideshow")

                        ccc = 1
                        image_files = []
                        source_blob_names = []
                        blob = bucket.get_blob(f"{media_prefix}/{video_id}_{ccc:02}.jpeg")

                        while blob and blob.exists():
                            blob.download_to_filename(os.path.join(temp_dir,f"{video_id}_{ccc:02}.jpeg"))
                            source_blob_names.append(blob.name)
                            if blob.size >= min_size:
                                image_files.append(os.path.join(temp_dir,f"{video_id}_{ccc:02}.jpeg"))
                            ccc += 1
                            blob = bucket.get_blob(f"{media_prefix}/{video_id}_{ccc:02}.jpeg")

                        temp_mp4 = os.path.join(temp_dir, f"{video_id}.mp4")
                        if not image_files:
                            # All source jpegs are missing or under min_size
                            # (e.g. a bot wall serving stub bodies): assembling
                            # would raise "No input files provided". Mark media
                            # as failed-transient so the id stays queued.
                            logger.warning(
                                f"No usable carousel images for '{video_id}' — "
                                f"skipping slideshow assembly; media stays "
                                f"queued for retry.")
                            scrape_metadata.loc[0, 'video_downloaded'] = False
                            scrape_metadata.attrs['media_error_type'] = 'carousel'
                            scrape_metadata.attrs['media_error_detail'] = (
                                'no usable carousel images for slideshow assembly')
                        else:
                            # Audio is optional: any failure yields a silent slideshow.
                            try:
                                audio_path = scraper.fetch_slideshow_audio(video_id, temp_dir)
                            except Exception:
                                audio_path = None

                            try:
                                make_slideshow(
                                    image_files,
                                    output=temp_mp4,
                                    duration=SLIDESHOW_SECONDS_PER_IMAGE,
                                    swipe=False,
                                    audio_path=audio_path,
                                    verbose=verbose
                                )
                            finally:
                                if audio_path:
                                    try: os.remove(audio_path)
                                    except OSError: pass

                        if image_files and os.path.getsize(temp_mp4) > min_size:
                            if verbose:
                                logger.info("Uploading video file to storage bucket...")
                            blob = bucket.blob(f"{media_prefix}/{video_id}.mp4")
                            blob.upload_from_filename(temp_mp4)
                            scrape_metadata.loc[0,'video_downloaded'] = True
                            # Source jpegs are no longer needed once the mp4 is
                            # stored (parity with the local branch's cleanup).
                            for name in source_blob_names:
                                try: bucket.blob(name).delete()
                                except Exception: pass
                        elif image_files:
                            if verbose:
                                logger.warning("Generated video file is too small, not uploading.")
                            scrape_metadata.loc[0,'video_downloaded'] = False

                        # /tmp is memory-backed on Cloud Run — drop the temp
                        # jpegs and the temp mp4 either way.
                        for ttt in range(1, ccc):
                            try: os.remove(os.path.join(temp_dir, f"{video_id}_{ttt:02}.jpeg"))
                            except OSError: pass
                        try: os.remove(temp_mp4)
                        except OSError: pass
                else:
                    # Local path: jpegs are in media_dir (written by _download_images).
                    # Assemble the slideshow to a temp file, validate size, then atomically
                    # move into media_dir and remove source jpegs.
                    final_mp4 = os.path.join(platform_media_dir, f"{video_id}.mp4")
                    if os.path.exists(final_mp4):
                        if verbose:
                            logger.info("Photo slideshow already exists locally")
                        scrape_metadata.loc[0,'video_downloaded'] = True
                    else:
                        if verbose:
                            logger.info("Converting photos to video slideshow")

                        ccc = 1
                        image_files = []
                        while True:
                            cand = os.path.join(platform_media_dir, f"{video_id}_{ccc:02}.jpeg")
                            if not os.path.exists(cand):
                                break
                            if os.path.getsize(cand) >= min_size:
                                image_files.append(cand)
                            ccc += 1

                        temp_mp4 = os.path.join(temp_dir, f"{video_id}.mp4")
                        if not image_files:
                            # All source jpegs are missing or under min_size:
                            # assembling would raise "No input files provided".
                            # Mark media as failed-transient so the id stays
                            # queued (mirrors the GCS branch).
                            logger.warning(
                                f"No usable carousel images for '{video_id}' — "
                                f"skipping slideshow assembly; media stays "
                                f"queued for retry.")
                            scrape_metadata.loc[0, 'video_downloaded'] = False
                            scrape_metadata.attrs['media_error_type'] = 'carousel'
                            scrape_metadata.attrs['media_error_detail'] = (
                                'no usable carousel images for slideshow assembly')
                        else:
                            # Audio is optional: any failure yields a silent slideshow.
                            try:
                                audio_path = scraper.fetch_slideshow_audio(video_id, temp_dir)
                            except Exception:
                                audio_path = None

                            try:
                                make_slideshow(
                                    image_files,
                                    output=temp_mp4,
                                    duration=SLIDESHOW_SECONDS_PER_IMAGE,
                                    swipe=False,
                                    audio_path=audio_path,
                                    verbose=verbose
                                )
                            finally:
                                if audio_path:
                                    try: os.remove(audio_path)
                                    except OSError: pass

                            if os.path.exists(temp_mp4) and os.path.getsize(temp_mp4) > min_size:
                                if verbose:
                                    logger.info("Moving slideshow to media folder...")
                                os.replace(temp_mp4, final_mp4)
                                scrape_metadata.loc[0,'video_downloaded'] = True
                            else:
                                if verbose:
                                    logger.warning("Generated video file is too small, discarding.")
                                if os.path.exists(temp_mp4):
                                    try: os.remove(temp_mp4)
                                    except OSError: pass
                                scrape_metadata.loc[0,'video_downloaded'] = False

                        # Clean up source jpegs from media_dir either way
                        for cand in image_files:
                            try: os.remove(cand)
                            except OSError: pass

            # if this is a video...
            else:
                if verbose:
                    logger.info(f"OK   - Video downloaded '{video_id}' - {col_count} metadata fields")

                if use_gcs:
                    # check if it truly is stored and is big enough
                    if verbose:
                        logger.info("Checking video file in bucket")
                    if bucket.blob(f"{media_prefix}/{video_id}.mp4").exists():
                        blob = bucket.get_blob(f"{media_prefix}/{video_id}.mp4")
                        if blob.size < min_size:
                            if verbose:
                                logger.info(f"   - Deleting video file smaller than threshold: {blob.name} of size {blob.size} bytes")
                            blob.delete()
                            scrape_metadata.loc[0,'video_downloaded'] = False
                        if verbose:
                            logger.info(f"   - Video file {blob.name} of size {blob.size:,} bytes is okay")
                    else:
                        if verbose:
                            logger.warning("   - WARNING: File not found")
                        scrape_metadata.loc[0,'video_downloaded'] = False
                else:
                    if verbose:
                        logger.info("Checking video file in local media folder")
                    local_mp4 = os.path.join(platform_media_dir, f"{video_id}.mp4")
                    if os.path.exists(local_mp4):
                        local_size = os.path.getsize(local_mp4)
                        if local_size < min_size:
                            if verbose:
                                logger.info(f"   - Deleting video file smaller than threshold: {local_mp4} of size {local_size} bytes")
                            try: os.remove(local_mp4)
                            except OSError: pass
                            scrape_metadata.loc[0,'video_downloaded'] = False
                        else:
                            if verbose:
                                logger.info(f"   - Video file {local_mp4} of size {local_size:,} bytes is okay")
                    else:
                        if verbose:
                            logger.warning("   - WARNING: File not found")
                        scrape_metadata.loc[0,'video_downloaded'] = False

            return scrape_metadata
        
        # if metadata is downloaded but no video is downloaded
        elif col_count > 1 and scrape_metadata.loc[0,'video_downloaded']==False:
            if verbose:
                logger.info(f"Accessed {col_count} metadata fields for {video_id} but did not download media object(s)")
            return scrape_metadata
        else:
            if verbose:
                logger.warning(f"Insufficient metadata columns ({col_count}) - Download of {video_id} - failed")

    except Exception as e:
        logger.error(e)

    # Failure path: return the empty DataFrame if it carries error attrs
    # (from tiktok_dl), otherwise fall back to the video_id string.
    if isinstance(scrape_metadata, pd.DataFrame) and scrape_metadata.empty and scrape_metadata.attrs.get('error_type'):
        return scrape_metadata

    return video_id










def _add_storage_link(
    results: pd.DataFrame,
    platform: str,
    overrides: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Populate the canonical ``storage_link`` from item_id + video_downloaded.

    The media object path mirrors the routing in ``download_single_video``: a
    ``gs://`` URL when media is stored on GCS, else the local media path, under
    the per-platform subpath. Empty for rows whose media was not downloaded.

    Args:
        results: a canonical scrape frame with ``item_id`` / ``video_downloaded``.
        platform: the platform whose media subpath new downloads were saved to.
        overrides: ``{item_id: actual_link}`` for items whose media already
            existed elsewhere (e.g. the legacy flat path) — keeps
            ``storage_link`` truthful for skip-media items.

    Returns:
        The same frame with a ``string[pyarrow]`` ``storage_link`` column.
    """
    overrides = overrides or {}
    use_gcs = _cf()['data_io']['use_gcs_for_media']
    if use_gcs:
        bucket = _cf()['data_io'].get('bucket')
        prefix = _cf()['data_io']['gcs_media_prefix']
        bucket_name = getattr(bucket, "name", "") if bucket is not None else ""
        def _link(vid: str) -> str:
            return f"gs://{bucket_name}/{prefix}/{media_paths.media_relpath(platform, vid)}"
    else:
        media_dir = _cf()['paths']['media']
        def _link(vid: str) -> str:
            return os.path.join(media_dir, media_paths.media_relpath(platform, vid))
    if "video_downloaded" in results.columns:
        downloaded = results["video_downloaded"].fillna(False)
    else:
        downloaded = pd.Series(False, index=results.index)
    results["storage_link"] = pd.Series(
        [overrides.get(vid) or (_link(vid) if dl else "") for vid, dl in zip(results["item_id"], downloaded)],
        index=results.index,
        dtype="string[pyarrow]",
    )
    return results




def _canonicalize_recode_save(
    results: pd.DataFrame,
    scraper,
    fine_ts: str,
    verbose: bool = False,
    reporter=None,
    storage_link_overrides: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Fix up, canonicalize, recode, and save a raw scrape batch to parquet.

    Called by ``download_video_threads``.
    Operates on the concatenated raw single-row frames: applies the scraper's
    ``prepare_raw_batch`` fix-ups on the RAW names (e.g. TikTok slideshow
    image_list/duration), canonicalizes via the scraper (rename + overflow
    repair + per-K rates + plays_per_day + scrape_status), stamps storage_link,
    filters to the var_schema, recodes, and writes ``scrapes_<ts>.parquet``.

    Args:
        results: concatenated raw single-row scrape frames.
        scraper: the platform scraper instance (provides canonicalize_batch).
        fine_ts: digit-only timestamp for the output filename.
        verbose: print dropped-column / save diagnostics.

    Returns:
        The saved (recoded, canonical) frame.
    """
    scrape_filename = f"{_scrapes_label()}_{fine_ts}.parquet"

    # save the raw results to local temp just in case everything goes to pieces
    results.to_parquet(os.path.join(_cf()['paths']['temp'], "recovered_" + scrape_filename))

    try:
        results.drop(["do_not_modify"], axis=1, errors='ignore', inplace=True)
        # platform-specific fix-ups on the RAW names (e.g. TikTok: image_list
        # URL string -> count, slideshow duration override) before the scraper
        # renames video_duration -> duration.
        results = scraper.prepare_raw_batch(results)

        # canonicalize: raw platform names -> canonical, plus per-K / plays_per_day /
        # scrape_status, then stamp the media storage_link.
        results = scraper.canonicalize_batch(results, status="ok")
        results = _add_storage_link(results, scraper.platform, storage_link_overrides)

        # apply prefix renames (no-op for scrape columns; covers annotation prefixes)
        results = rename_columns(results).copy()

        # only keep columns as defined by the variable schema
        dropped = sorted(set(results.columns) - set(_cf()['var_schema'].variable_name))
        relevant_cols = [c for c in _cf()['var_schema'].variable_name if c in results.columns]
        results = results[relevant_cols].copy()
        record_dropped_columns(
            "scrape_whitelist", dropped, reason="whitelist", reporter=reporter,
            guardrail="off", verbose=verbose,
        )

        # recode_events_df drops role=skip columns (scrape_ts / storage_link are
        # base provenance fields kept on disk but hidden from analysis). Snapshot
        # the base columns by item_id, then restore any the recode dropped.
        base_present = [c for c in scraper.base_columns if c in results.columns]
        base_snapshot = (
            results[["item_id"] + [c for c in base_present if c != "item_id"]].copy()
            if "item_id" in results.columns else None
        )

        # recode the data
        results = recode_events_df(
            study_dataset=results,
            drop_single_value_cols=False,
            verbose=verbose,
            reporter=reporter,
        )

        if base_snapshot is not None and "item_id" in results.columns:
            for col in base_present:
                if col == "item_id" or col in results.columns:
                    continue
                mapping = dict(zip(base_snapshot["item_id"], base_snapshot[col]))
                restored = results["item_id"].map(mapping)
                try:
                    restored = restored.astype(scraper.base_columns[col])
                except Exception:
                    pass
                results[col] = restored

        # scraped_ok is retained as a derived back-compat shim (downstream merges
        # in organize_datasets / data_service still read it); scrape_status is the
        # source of truth.
        results["scraped_ok"] = (results["scrape_status"] == "ok").astype("bool[pyarrow]")

        data_io.save_parquet(df=results, storage_location="scrape", filename=scrape_filename)
        logger.info(f"Saved {len(results):,} rows to '{scrape_filename}'. Media downloaded for {len(results[results['video_downloaded']]):,} of these.")

    except Exception as e:
        logger.error(f"CRITICAL: Failed to save results to parquet: {e}")
        logger.info("Recovering the un-processed results from temp")
        data_io.move(
            src_storage_location="temp",
            dst_storage_location="scrape",
            filename="recovered_" + scrape_filename,
            verbose=verbose,
        )

    return results





def check_existing_media(
    video_ids: list[str],
    max_workers: int = 16,
    platform: str | None = None,
) -> dict[str, str]:
    """Return the video_ids whose media file is already stored, with its link.

    A video_id qualifies as "already downloaded" when a media file exists at
    the platform subpath (``{platform}/{video_id}.mp4``) or the legacy flat
    path (``{video_id}.mp4``) and its size meets
    ``fyp_cf['misc']['min_media_object_size']``. Under-sized files are treated
    as invalid (consistent with post-download validation in
    ``download_single_video``).

    The returned link is the file's *actual* location (``gs://`` URL or local
    path) so callers can stamp a truthful ``storage_link`` for items whose
    media predates the per-platform layout.

    Routes between local filesystem and GCS via the same config as
    ``download_single_video`` (``fyp_cf['data_io']['use_gcs_for_media']``).
    GCS probes run on a bounded thread pool for throughput.

    Any exception on a single probe is treated as "unknown — include in the
    normal scrape path" (fail-safe: never falsely skip a media download).

    Args:
        video_ids: Video IDs to check.
        max_workers: Parallelism for GCS probes (ignored in local mode).
        platform: The items' platform (narrows the probe to that platform's
            subpath plus the legacy flat path).

    Returns:
        ``{video_id: storage_link}`` for ids with valid existing media.
    """
    if not video_ids:
        return {}

    use_gcs = _cf()['data_io']['use_gcs_for_media']
    min_size = _cf()['misc']['min_media_object_size']
    relpaths = [media_paths.media_relpath(platform, "{vid}")] if platform else []
    relpaths.append("{vid}.mp4")

    if use_gcs:
        bucket = _cf()['data_io']['bucket']
        gcs_media_prefix = _cf()['data_io']['gcs_media_prefix']
        if bucket is None:
            return {}
        bucket_name = getattr(bucket, "name", "")

        def _probe(vid: str) -> tuple[str, str] | None:
            for rel_template in relpaths:
                try:
                    blob_name = f"{gcs_media_prefix}/{rel_template.format(vid=vid)}"
                    blob = bucket.get_blob(blob_name)
                    if blob is not None and blob.size is not None and blob.size >= min_size:
                        return vid, f"gs://{bucket_name}/{blob_name}"
                except Exception:
                    continue
            return None

        present: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for result in ex.map(_probe, video_ids):
                if result is not None:
                    present[result[0]] = result[1]
        return present

    media_dir = _cf()['paths']['media']
    present = {}
    for vid in video_ids:
        for rel_template in relpaths:
            try:
                path = os.path.join(media_dir, rel_template.format(vid=vid))
                if os.path.exists(path) and os.path.getsize(path) >= min_size:
                    present[vid] = path
                    break
            except Exception:
                continue
    return present





_yt_dlp_plugins_warmed = False


def _warm_yt_dlp_plugins() -> None:
    """Trigger yt-dlp's lazy plugin loading once, on the calling thread.

    yt-dlp imports its plugin modules (e.g. the bgutil PO-token provider) on
    the first ``YoutubeDL`` instantiation. When that first instantiation
    happens concurrently in worker threads, the plugin registry races and
    every loser prints an "already registered" assertion traceback. A single
    warm-up instantiation before the pool spawns makes the load
    single-threaded. Never raises — yt-dlp problems surface in the workers.
    """
    global _yt_dlp_plugins_warmed
    if _yt_dlp_plugins_warmed:
        return
    try:
        import yt_dlp

        yt_dlp.YoutubeDL({"quiet": True})
    except Exception:
        pass
    _yt_dlp_plugins_warmed = True





def download_video_threads(
    interesting_videos:list[str] = None,
    max_workers:int = 4,
    verbose:bool = False,
    dry_run:bool = False,
    batch_label: str | None = None,
    cumulative_done: int = 0,
    cumulative_total: int = 0,
    cumulative_ok: int = 0,
    cumulative_fail: int = 0,
    reporter=None,
    on_concurrency_change: "callable | None" = None,
    on_video_done: "callable | None" = None,
    platform: str | None = None):



    if dry_run:
        logger.info("********* This is a dry run. It's all fake. No data io action at all. *********")
    else:
        if interesting_videos is None:
            raise ValueError("No interesting videos specified")

        if len(interesting_videos) == 0:
            return pd.DataFrame()

    # One scraper instance for the whole batch (loads the scrape contract once);
    # the platform-specific fetch/canonicalize/classify live on it. Created
    # before the media check so the probe knows the platform's media subpath.
    scraper = get_scraper(platform, verbose=verbose)

    already_have_media: dict[str, str] = {}
    if not dry_run and interesting_videos:
        already_have_media = check_existing_media(interesting_videos, platform=scraper.platform)
        if already_have_media:
            logger.info(
                f"  {len(already_have_media)}/{len(interesting_videos)} items "
                f"already have media — will do metadata-only scrape for those"
            )

    results_by_index = {}
    # Record the active scrape-contract version once per batch (idempotent, non-raising).
    scrape_versioning.ensure_active_version_registered()
    # Concurrency bounds are platform policy (e.g. TikTok caps at 6: a single
    # session behind cookies trips behavioural flags beyond that).
    throttle_initial, throttle_min, throttle_max = scraper.throttle_limits(max_workers)
    throttle = ThrottleController(
        initial=throttle_initial, minimum=throttle_min, maximum=throttle_max,
        on_change=on_concurrency_change)

    # Circuit breaker: a run of consecutive throttle-category outcomes (fetch
    # OR media phase) means the session is rate-limited/bot-walled — e.g.
    # YouTube blocks the whole session for up to an hour. Grinding on burns
    # the queue for nothing, so the batch aborts; unprocessed items return as
    # "batch_aborted" (transient) and stay queued.
    breaker_lock = threading.Lock()
    breaker_state = {"consecutive": 0, "tripped": False}
    # Permanent-storm guard: a run of consecutive *identical* permanent
    # classifications (e.g. permanent:removed) means the session — not the
    # items — is broken, so the batch aborts and those ids are demoted to
    # transient instead of being pruned from the queue as permanently failed.
    storm_state = {"classification": None, "consecutive": 0, "tripped": False}
    storm_threshold = _permanent_storm_threshold()
    # Transient-storm guard: a run of consecutive *identical* transient
    # classifications (e.g. transient:unknown) that the circuit breaker and
    # the permanent-storm guard are both blind to — a platform-side breakage
    # whose error lands in a retryable category. Aborts the batch and stops
    # chaining; the items are transient so they stay queued as-is.
    t_storm_state = {"classification": None, "consecutive": 0, "tripped": False}
    t_storm_threshold = _transient_storm_threshold()
    abort_event = threading.Event()
    # Memory safety valve: set once the container's memory cgroup crosses
    # MEMORY_STOP_FRACTION. Workers that have not started downloading yet defer
    # their item back to the queue (transient) rather than push the container
    # into an OOM kill; in-flight downloads finish and are saved.
    mem_stop_event = threading.Event()
    inter_delay = scraper.inter_request_delay()

    def _breaker_track(category) -> None:
        with breaker_lock:
            if category in THROTTLE_CATEGORIES:
                breaker_state["consecutive"] += 1
                if breaker_state["consecutive"] >= CIRCUIT_BREAKER_THRESHOLD and not abort_event.is_set():
                    breaker_state["tripped"] = True
                    abort_event.set()
                    logger.warning(f"  [scrape] Circuit breaker: {breaker_state['consecutive']} "
                                   f"consecutive {sorted(THROTTLE_CATEGORIES)} results — "
                                   f"aborting batch; remaining items stay queued.")
            else:
                breaker_state["consecutive"] = 0
            if storm_state["tripped"] or t_storm_state["tripped"]:
                # Frozen once tripped: post-abort "batch_aborted" results are
                # transient and would otherwise wipe the storm classification.
                return
            classification = scraper.classify_error(category)
            if classification.startswith("permanent"):
                if classification == storm_state["classification"]:
                    storm_state["consecutive"] += 1
                else:
                    storm_state["classification"] = classification
                    storm_state["consecutive"] = 1
                if storm_state["consecutive"] >= storm_threshold and not storm_state["tripped"]:
                    storm_state["tripped"] = True
                    abort_event.set()
                    logger.warning(f"  [scrape] Permanent-storm guard: "
                                   f"{storm_state['consecutive']} consecutive "
                                   f"'{classification}' results — the session, not the "
                                   f"items, is suspect. Aborting batch; affected items "
                                   f"stay queued and are not recorded as failed.")
            else:
                storm_state["classification"] = None
                storm_state["consecutive"] = 0
            # 'batch_aborted' is synthetic (post-abort placeholder), never a
            # scraper verdict — it must not seed or extend a transient run.
            if (classification.startswith("transient")
                    and category != "batch_aborted"):
                if classification == t_storm_state["classification"]:
                    t_storm_state["consecutive"] += 1
                else:
                    t_storm_state["classification"] = classification
                    t_storm_state["consecutive"] = 1
                if (t_storm_state["consecutive"] >= t_storm_threshold
                        and not t_storm_state["tripped"]):
                    t_storm_state["tripped"] = True
                    abort_event.set()
                    logger.warning(f"  [scrape] Transient-storm guard: "
                                   f"{t_storm_state['consecutive']} consecutive "
                                   f"'{classification}' results — the platform or the "
                                   f"scraper is likely broken. Aborting batch; the items "
                                   f"are transient and stay queued for a later retry.")
            else:
                t_storm_state["classification"] = None
                t_storm_state["consecutive"] = 0

    def worker(idx_video):
        idx, video = idx_video
        throttle.acquire()
        try:
            if abort_event.is_set():
                aborted = pd.DataFrame()
                aborted.attrs['error_type'] = 'batch_aborted'
                aborted.attrs['error_detail'] = 'batch aborted by rate-limit circuit breaker'
                return idx, aborted
            if mem_stop_event.is_set():
                deferred = pd.DataFrame()
                deferred.attrs['error_type'] = 'batch_aborted'
                deferred.attrs['error_detail'] = 'deferred: container memory near limit'
                return idx, deferred
            skip_media = video in already_have_media
            res = download_single_video(
                video_id=video,
                verbose=verbose,
                save_video=not skip_media,
                dry_run=dry_run,
                scraper=scraper)
            # For items where media already exists, reflect actual storage state
            # in the metadata row — save_tiktok returns video_downloaded=False
            # when save_video=False, which is misleading for skipped items.
            if skip_media and isinstance(res, pd.DataFrame) and not res.empty:
                try:
                    res.loc[res.index[0], 'video_downloaded'] = True
                except Exception:
                    pass
            # Report outcome to throttle controller. A metadata success whose
            # media phase failed still carries a throttle-relevant category
            # (attrs['media_error_type'], see BaseScraper.fetch contract).
            if isinstance(res, pd.DataFrame) and res.empty:
                error_cat = res.attrs.get('error_type')
            elif isinstance(res, pd.DataFrame):
                error_cat = res.attrs.get('media_error_type')
            else:
                error_cat = None
            throttle.report_result(error_cat)
            _breaker_track(error_cat)
            if inter_delay > 0:
                # Sleep while holding the throttle slot: paces the whole
                # session, not just this thread.
                time.sleep(inter_delay)
            if on_video_done:
                ok = isinstance(res, pd.DataFrame) and not res.empty
                on_video_done(idx, ok, error_cat)
            return idx, res
        except Exception:
            throttle.report_result("unknown")
            raise
        finally:
            throttle.release()


    if verbose:
        logger.info(f"dry_run: {dry_run}")
        logger.info(f"Scraping data for {len(interesting_videos)} items with {max_workers} threads.")

    # Load yt-dlp's plugin registry once before workers spawn: plugin modules
    # (e.g. the bgutil PO-token provider) are imported on the first YoutubeDL
    # instantiation, and doing that concurrently from a dozen threads races
    # the provider registry into "already registered" assertion tracebacks.
    _warm_yt_dlp_plugins()

    # Pool is oversized so the ThrottleController's semaphore governs
    # actual concurrency — allows dynamic resizing without pool restart
    pool_size = max(max_workers, 12)
    with ThreadPoolExecutor(max_workers=pool_size) as ex:


        futures = []
        submit_times = {}
        for iv in enumerate(interesting_videos):
            fut = ex.submit(worker, iv)
            futures.append(fut)
            submit_times[fut] = time.time()


        monitor_thread = start_monitor(
            futures, submit_times, interval=5, label="dl", bar_width=32,
            result_checker=_scrape_future_succeeded,
            batch_label=batch_label,
            cumulative_done=cumulative_done,
            cumulative_total=cumulative_total,
            cumulative_ok=cumulative_ok,
            cumulative_fail=cumulative_fail,
            reporter=reporter,
        )


        # Batch-level deadline: prevents a single slow download from blocking
        # the entire batch indefinitely.  Stuck items are recorded as failures.
        _per_item_ceiling = 120
        _waves = max(1, (len(interesting_videos) + pool_size - 1) // pool_size)
        batch_deadline = min(int(_waves * _per_item_ceiling * 1.5 + 60), 1800)

        # Background memory watch: sample the container memory cgroup on a
        # ~1s timer, independent of item completion, so a fast climb is caught
        # even while downloads are in flight (the as_completed loop stalls
        # during a slow download and would miss a spike). Logs the curve for
        # diagnostics and, past MEMORY_STOP_FRACTION, trips the safety valve so
        # not-yet-started workers defer their items instead of OOM-killing the
        # container mid-drain (which loses the whole batch — the scrapers are
        # single-attempt by design, so nothing re-delivers it).
        mem_watch_stop = threading.Event()
        _mem_progress = {"done": 0}

        def _mem_watch():
            last_log = 0.0
            while not mem_watch_stop.wait(1.0):
                mem = _container_memory_fraction()
                if mem is None:
                    continue
                frac, used_gib = mem
                now = time.monotonic()
                if now - last_log >= 10:
                    logger.info(f"  [mem] {used_gib:.1f} GiB ({frac:.0%} of limit) "
                                f"after {_mem_progress['done']}/{len(interesting_videos)} items")
                    last_log = now
                if frac >= _memory_stop_fraction() and not mem_stop_event.is_set():
                    mem_stop_event.set()
                    logger.warning(f"  [scrape] Memory safety valve: {used_gib:.1f} GiB "
                                   f"({frac:.0%} of limit) — stopping new downloads; "
                                   f"remaining items stay queued for the next batch.")

        mem_watch_thread = threading.Thread(target=_mem_watch, daemon=True)
        mem_watch_thread.start()
        try:
            for fut in as_completed(futures, timeout=batch_deadline):
                idx, res = fut.result()
                results_by_index[idx] = res
                _mem_progress["done"] += 1
        except TimeoutError:
            stuck = [interesting_videos[i] for i, f in enumerate(futures) if not f.done()]
            logger.warning(
                f"  [scrape] Batch deadline of {batch_deadline}s exceeded; "
                f"{len(stuck)} worker(s) did not finish: {stuck[:5]}"
                + (" ..." if len(stuck) > 5 else "")
            )
            # Record DNF items as empty DataFrames (failures)
            for i in range(len(interesting_videos)):
                if i not in results_by_index:
                    empty = pd.DataFrame()
                    empty.attrs['error_type'] = 'timeout'
                    results_by_index[i] = empty
        finally:
            mem_watch_stop.set()
            mem_watch_thread.join(timeout=3)

        monitor_thread.join(timeout=5)

    if throttle.total_throttle_events > 0:
        logger.info(f"  Throttle: {throttle.total_throttle_events} rate-limit events, "
              f"final concurrency: {throttle.current}")

    results = []
    failed_items = []
    permanent_failed_ids: list[str] = []
    transient_failed_ids: list[str] = []
    media_retry_ids: list[str] = []
    storm_demoted = 0
    for idx in range(len(interesting_videos)):
        res = results_by_index.get(idx)
        if isinstance(res, pd.DataFrame) and res.shape[1] > 10:
            results += [res]
            # Metadata succeeded but the media phase failed transiently:
            # the row is saved (fresh metadata) AND the id stays queued so
            # media is retried next run. attrs don't survive pd.concat, so
            # this is the last place they're visible.
            media_error = res.attrs.get('media_error_type')
            if media_error is not None and not scraper.classify_error(media_error).startswith('permanent'):
                media_retry_ids.append(interesting_videos[idx])
        else:
            vid = interesting_videos[idx]
            error_type = res.attrs.get('error_type', 'unknown') if isinstance(res, pd.DataFrame) else 'unknown'
            # The scraper owns its platform's permanent-vs-transient taxonomy.
            classification = scraper.classify_error(error_type)
            if classification.startswith('permanent'):
                if storm_state["tripped"] and classification == storm_state["classification"]:
                    # Suspect storm verdict: keep the id queued and off the
                    # failed record — a later healthy session re-scrapes it.
                    transient_failed_ids.append(vid)
                    storm_demoted += 1
                    continue
                permanent_failed_ids.append(vid)
            else:
                transient_failed_ids.append(vid)
            # The category travels with the id so a later run can single out one
            # kind of failure — an "ip_blocked" verdict reached from one vantage
            # point may not hold from another, unlike a removed or private post.
            failed_items += [{"item_id": vid, "category": classification}]

    if storm_state["tripped"]:
        logger.warning(f"  Permanent-storm guard: {storm_demoted} "
                       f"'{storm_state['classification']}' failures demoted to transient "
                       f"— kept in queue, not recorded as failed.")

    # Durable, user-visible alert: a storm means the scraper (or its session)
    # is likely broken — e.g. the platform changed its site/API — and needs a
    # human look. Raise it on a storm; clear it once a batch produces real
    # results again with no storm. Best-effort on both sides (never blocks
    # scraping), and skipped in dry runs.
    if not dry_run:
        if storm_state["tripped"]:
            scraper_alerts.raise_alert(
                platform=scraper.platform,
                kind=scraper_alerts.KIND_PERMANENT_STORM,
                category=storm_state["classification"],
                count=storm_state["consecutive"],
                message=(
                    f"{storm_state['consecutive']} consecutive items failed with the same "
                    f"permanent verdict ({storm_state['classification']}) — the platform has "
                    f"likely changed something and the {scraper.platform} scraper (or its "
                    f"session) needs attention. Scraping was stopped; the affected items "
                    f"remain queued and were not recorded as failed."
                ),
            )
        elif t_storm_state["tripped"]:
            scraper_alerts.raise_alert(
                platform=scraper.platform,
                kind=scraper_alerts.KIND_TRANSIENT_STORM,
                category=t_storm_state["classification"],
                count=t_storm_state["consecutive"],
                message=(
                    f"{t_storm_state['consecutive']} consecutive items failed with the "
                    f"same transient verdict ({t_storm_state['classification']}) — the "
                    f"platform has likely changed something (e.g. a new bot wall or a "
                    f"broken extractor) and the {scraper.platform} scraper needs "
                    f"attention. Scraping was stopped; the affected items remain queued "
                    f"and will be retried once the scraper is healthy again."
                ),
            )
        elif results:
            scraper_alerts.clear_alert(scraper.platform, reason="healthy batch")

    if media_retry_ids:
        logger.info(f"  Media retries: {len(media_retry_ids)} items scraped metadata-only "
              f"(transient media failure) — kept in queue for media retry")
        transient_failed_ids += media_retry_ids

    if permanent_failed_ids or transient_failed_ids:
        logger.info(f"  Failures: {len(permanent_failed_ids)} permanent, "
              f"{len(transient_failed_ids)} transient (will retry)")

    if len(results)==0:
        logger.warning("The scrape procedure did not generate any useful results")
        empty_results = pd.DataFrame()
        empty_results.attrs['circuit_breaker_tripped'] = breaker_state["tripped"]
        empty_results.attrs['permanent_storm_tripped'] = storm_state["tripped"]
        empty_results.attrs['permanent_storm_category'] = storm_state["classification"]
        empty_results.attrs['transient_storm_tripped'] = t_storm_state["tripped"]
        empty_results.attrs['transient_storm_category'] = t_storm_state["classification"]
        empty_results.attrs['memory_stop'] = mem_stop_event.is_set()
        return empty_results, permanent_failed_ids, transient_failed_ids

    # ignore_index=True: each element is a single-row frame indexed 0, so a
    # plain concat leaves a duplicate index that turns the recode's
    # concat(axis=1) hashtag fan-out into a cartesian row explosion.
    results = pd.concat(results, ignore_index=True)

    fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])

    if not dry_run and len(results)>0:
        results = _canonicalize_recode_save(
            results, scraper, fine_ts, verbose=verbose, reporter=reporter,
            storage_link_overrides=already_have_media,
        )

    if not dry_run and len(failed_items)>0:
        data_io.save_json(data = failed_items, storage_location="scrape", filename=f"{_failed_scrapes_label()}_{fine_ts}.json", verbose=verbose)
        logger.info(f"Saved {len(failed_items)} failed items")

    # Signal upward (batch loop / queue-scraper chaining) that this batch was
    # aborted by the rate-limit circuit breaker or the permanent-storm guard.
    # Set post-save so pipeline transforms can't have dropped the attrs.
    # ``memory_stop`` is distinct from both: it does NOT stop chaining — the
    # completed rows are saved and pruned, and the deferred items are picked up
    # by the next (fresh-process) chain.
    results.attrs['circuit_breaker_tripped'] = breaker_state["tripped"]
    results.attrs['permanent_storm_tripped'] = storm_state["tripped"]
    results.attrs['permanent_storm_category'] = storm_state["classification"]
    results.attrs['transient_storm_tripped'] = t_storm_state["tripped"]
    results.attrs['transient_storm_category'] = t_storm_state["classification"]
    results.attrs['memory_stop'] = mem_stop_event.is_set()

    return results, permanent_failed_ids, transient_failed_ids











def scraper_loop_from_list(
    video_list = [],
    study_name = None,
    batch_size = 500,
    max_batches = None,
    verbose = False,
    dry_run = False,
    reporter=None,
    cancellation_check=None,
    platform: str | None = None,
    process_name: str | None = None,
    ):



    max_batches = max_batches if max_batches is not None else np.inf
    platform_resolved = platform or scrape_queues.default_platform()
    stop_key = process_name or f"queue_scraper_{platform_resolved}"



    logger.info(f"    Downloading media objects and metadata for selected videos, batch size: {batch_size}, max batches: {max_batches}")
    logger.info(f"    Now: {datetime.now()}")


    batch_number = 1

    batch_target = min(max_batches, len(video_list) // batch_size + 1)

    logger.info(f"  Starting loop... There are {len(video_list):,} videos to process in {batch_target:,} batches")

    total_items = min(len(video_list), batch_target * batch_size)
    cumulative_done = 0
    good_scrapes = []
    all_permanent_failed = []
    all_transient_failed = []

    if reporter is not None:
        reporter.emit_data({"threads": 8})

    def _on_threads_change(n):
        if reporter is not None:
            reporter.emit_data({"threads": n})

    for batch in chunk_list(video_list, batch_size):

        batch_label = f"{batch_number}/{batch_target}"
        logger.info(f"  Batch {batch_label}")

        # Progress is owned by the monitor thread inside download_video_threads
        # (it has throughput / processing count / ETA); pass the reporter and the
        # job-wide OK/fail carry-over so it renders totals, not batch-local counts.
        results_from_scraper, perm_failed, trans_failed = download_video_threads(
            interesting_videos = batch,
            max_workers=4,
            verbose = verbose,
            dry_run = dry_run,
            batch_label=batch_label,
            cumulative_done=cumulative_done,
            cumulative_total=total_items,
            cumulative_ok=len(good_scrapes),
            cumulative_fail=len(all_permanent_failed),
            reporter=reporter,
            on_concurrency_change=_on_threads_change,
            platform=platform_resolved)

        if not results_from_scraper.empty and "item_id" in results_from_scraper.columns:
            good_scrapes += results_from_scraper["item_id"].to_list()

        all_permanent_failed += perm_failed
        all_transient_failed += trans_failed

        if results_from_scraper.attrs.get('circuit_breaker_tripped'):
            logger.warning("  Rate-limit circuit breaker tripped — stopping the batch loop. "
                  "Unfinished items stay in the queue; re-run the scraper later.")
            if reporter is not None:
                reporter.emit_data({"rate_limit_abort": True})
            break

        if results_from_scraper.attrs.get('permanent_storm_tripped'):
            logger.warning(f"  Permanent-failure storm detected "
                  f"({results_from_scraper.attrs.get('permanent_storm_category')}) — "
                  f"batch outcome suspect; stopping the batch loop. Affected items "
                  f"stay in the queue; re-run the scraper once the session is healthy.")
            if reporter is not None:
                reporter.emit_data({"permanent_storm_abort": True})
            break

        if results_from_scraper.attrs.get('transient_storm_tripped'):
            logger.warning(f"  Transient-failure storm detected "
                  f"({results_from_scraper.attrs.get('transient_storm_category')}) — "
                  f"every item is failing the same retryable way, so the platform or "
                  f"the scraper is likely broken; stopping the batch loop. The items "
                  f"stay in the queue for a later retry.")
            if reporter is not None:
                reporter.emit_data({"transient_storm_abort": True})
            break

        with open(os.path.join(_cf()['paths']['temp'], "temp_failed_scrapes.json"), "w") as f:
            json.dump(all_permanent_failed + all_transient_failed, f)
        with open(os.path.join(_cf()['paths']['temp'], "temp_good_scrapes.json"), "w") as f:
            json.dump(good_scrapes, f)

        cumulative_done += len(batch)

        # Emit queue update after each batch (for web UI)
        queue_remaining = len(video_list) - cumulative_done
        if reporter is not None:
            reporter.emit_data({"scrape_queue_len": max(0, queue_remaining)})
        elif "WEB_INTERFACE" in os.environ:
            # STDOUT PROTOCOL — MUST stay print(). process_manager.enqueue_output()
            # parses subprocess stdout for the ::DATA:: marker; never convert to logging.
            print(f"::DATA::{{\"scrape_queue_len\": {max(0, queue_remaining)}}}", flush=True)

        if max_batches is not None and batch_number >= max_batches:
            break

        # Check for cancellation request
        if cancellation_check is not None:
            if cancellation_check():
                logger.info("  Cancellation requested. Finishing after this batch.")
                break
        elif _check_graceful_stop(stop_key):
            logger.info("  Graceful stop requested. Finishing after this batch.")
            break

        batch_number += 1

        if dry_run:
            break

    # ----------------
    # Update scrape queue: remove successful + permanently failed items.
    # Transient failures stay in the queue for retry on next run.
    # -----------------
    # Transient failures include metadata-only rows whose media phase failed
    # transiently — those ids are also in good_scrapes (the metadata row was
    # saved) but must stay queued for a media retry.
    items_to_remove = set(good_scrapes + all_permanent_failed) - set(all_transient_failed)
    if items_to_remove:
        pruned, remaining = scrape_queues.prune_scrape_queue(platform_resolved, items_to_remove)
        if pruned:
            logger.info(f"  Queue update: removed {pruned} "
                  f"({len(good_scrapes)} OK, {len(all_permanent_failed)} permanent fail). "
                  f"{len(all_transient_failed)} transient failures remain for retry. "
                  f"Queue length: {remaining}")


    logger.info(f"  Loop ended: {datetime.now()}")
    return good_scrapes, all_permanent_failed, all_transient_failed










def queue_scraper_loop(
    batch_size = 500,
    max_batches = 10,
    verbose = False,
    dry_run = False,
    reporter=None,
    cancellation_check=None,
    platform: str | None = None,
    process_name: str | None = None,
    ):


    # Load the per-platform queue (migrates a legacy to_scrape.json first)
    platform_resolved = platform or scrape_queues.default_platform()
    video_list = scrape_queues.load_scrape_queue(platform_resolved)

    if not video_list:
        logger.info(f"Queue for '{platform_resolved}' is empty or invalid. Nothing to scrape.")
        return

    logger.info(f"Found {len(video_list)} items in '{platform_resolved}' queue.")

    scraper_loop_from_list(
        video_list=video_list,
        batch_size=batch_size,
        max_batches=max_batches,
        verbose=verbose,
        dry_run=dry_run,
        reporter=reporter,
        cancellation_check=cancellation_check,
        platform=platform_resolved,
        process_name=process_name,
    )










def _parse_scrape_filename_ts(filename: str | None) -> "pd.Timestamp":
    """Best-effort parse a ``scrapes_<digits>.parquet`` filename into a Timestamp.

    Legacy raw scrape parquets predate the persisted ``scrape_ts`` column; the
    file's name encodes when the scrape ran (the digits of ``datetime.now()``),
    which serves as a per-file scrape time so ``plays_per_day`` can still be
    derived. Returns ``pd.NaT`` when the name carries no parseable timestamp.

    Args:
        filename: the scrape parquet filename.

    Returns:
        The parsed timestamp, or ``pd.NaT``.
    """
    if not filename:
        return pd.NaT
    digits = "".join(c for c in filename if c.isdigit())
    if len(digits) < 14:
        return pd.NaT
    try:
        return pd.to_datetime(digits[:14], format="%Y%m%d%H%M%S")
    except Exception:
        return pd.NaT




def _coalesce_retired_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Fold retired platform-specific columns into their generic base successors.

    Historical scrape parquets carry the pre-retirement per-platform names
    (``stats_diggCount`` / ``ig_like_count`` / ``yt_author_handle`` / ...); this
    coalesces each into its generic base column (``fave_count`` /
    ``author_handle`` / ...) per ``scrape_contract.RETIRED_TO_GENERIC`` and drops
    the source. A coalesce (not a rename) because several retired columns share
    one target — a rename would create duplicate labels on a mixed-platform
    frame. Values are kept verbatim, including the -1 missing-count sentinels
    (the rate/plays-per-day derivations mask negatives). Per-file parquets on
    disk keep their old names and are re-coalesced on every consolidation, the
    same self-healing convention as the legacy rename.

    Args:
        df: a single raw scrape parquet's frame (mutated and returned).

    Returns:
        The frame with generic columns populated and retired columns dropped.
    """
    present = [c for c in sc.RETIRED_TO_GENERIC if c in df.columns]
    if not present:
        return df
    target_dtypes = sc.field_dtypes(sc.load_contract())
    for src in present:
        tgt = sc.RETIRED_TO_GENERIC[src]
        dtype = target_dtypes.get(tgt)
        source = df[src].astype(dtype) if dtype else df[src]
        if tgt in df.columns:
            df[tgt] = df[tgt].combine_first(source)
            if dtype:
                df[tgt] = df[tgt].astype(dtype)
        else:
            df[tgt] = source
        df = df.drop(columns=[src])
    return df




def _canonicalize_legacy_scrape(df: pd.DataFrame, filename: str | None = None, scraper=None) -> pd.DataFrame:
    """Migrate a legacy (pre-canonical) scrape parquet to the canonical schema.

    New scrape parquets are already saved with canonical column names, per-K
    engagement rates, plays_per_day, and scrape_status, so this is a no-op for
    them. A legacy parquet (TikTok-named columns, no per-K, ``scraped_ok`` bool)
    is renamed to canonical, its counts overflow-repaired, the per-K rates and
    plays_per_day derived, ``scrape_ts`` back-filled from the filename timestamp,
    and ``scrape_status`` back-filled from ``scraped_ok`` — so old and new files
    concatenate into one canonical frame.

    Args:
        df: a single raw scrape parquet's frame.
        filename: the parquet filename (back-fills scrape_ts for legacy frames).
        scraper: a platform scraper instance (created if not provided).

    Returns:
        The canonical frame (the input unchanged when already canonical).
    """
    if not any(c in df.columns for c in sc.LEGACY_COLUMN_ALIASES):
        return df
    df = df.rename(columns=sc.LEGACY_COLUMN_ALIASES)
    if "scrape_ts" not in df.columns or df["scrape_ts"].isna().all():
        df["scrape_ts"] = pd.Series(
            _parse_scrape_filename_ts(filename), index=df.index, dtype="timestamp[ns][pyarrow]"
        )
    if scraper is None:
        scraper = get_scraper(verbose=False)
    df = scraper.repair_counts(df)
    df = scraper.derive_engagement_rates(df)
    df = scraper.derive_plays_per_day(df)
    if "scrape_status" not in df.columns:
        if "scraped_ok" in df.columns:
            ok = df["scraped_ok"].astype("boolean").fillna(False)
            df["scrape_status"] = ok.map({True: "ok", False: "failed"}).astype("string[pyarrow]")
        else:
            df["scrape_status"] = pd.Series("ok", index=df.index, dtype="string[pyarrow]")
    # Complete the canonical schema: base fields the legacy frame never carried
    # (e.g. storage_link — no media path was recorded historically) become all-NA.
    df = scraper.ensure_base_columns(df)
    return df




def _load_enrichment_seeds(verbose: bool = False) -> dict[str, pd.DataFrame]:
    """Load all donated enrichment-seed parquets from the ``recoded`` location.

    The seeds are written per platform by ``fyp.ingest.save_enrichment_seed``
    (``{platform}_{source}_enrichment_seed.parquet``, canonical scrape base
    schema, ``scrape_status="donated"``).

    Returns:
        ``{filename: DataFrame}`` for every non-empty seed file found.
    """
    seeds: dict[str, pd.DataFrame] = {}
    for fn in data_io.listdir(storage_location="recoded"):
        if fn.endswith("_enrichment_seed.parquet"):
            df = data_io.load_parquet(storage_location="recoded", filename=fn)
            if df is not None and len(df) > 0:
                seeds[fn] = df
                if verbose:
                    logger.info(f"    Loaded {len(df):,} donated seed rows from {fn}")
    return seeds






def _merge_enrichment_seeds(
    scrape_df: pd.DataFrame,
    seed_frames: dict[str, pd.DataFrame],
    verbose: bool = False,
) -> pd.DataFrame:
    """Append donated seed rows for items that have no real scrape row.

    Precedence is a plain anti-join on ``(source_platform, item_id)``: any real
    scrape row beats a donated one, and re-consolidation after a later real
    scrape automatically drops the donated row (consolidation rebuilds from all
    files each run). Donated rows are stamped ``scraped_ok=False`` /
    ``video_downloaded=False`` so the items stay scrape-eligible while their
    donated caption/author metadata surfaces downstream.
    """
    if not seed_frames:
        return scrape_df

    seeds = pd.concat(list(seed_frames.values()), ignore_index=True)
    seeds = seeds[seeds["item_id"].notna()].copy()
    seeds = seeds.drop_duplicates(subset=["source_platform", "item_id"], keep="first")

    if len(scrape_df) > 0 and {"source_platform", "item_id"}.issubset(scrape_df.columns):
        real_keys = pd.MultiIndex.from_frame(
            scrape_df[["source_platform", "item_id"]].astype("string[pyarrow]")
        )
        seed_keys = pd.MultiIndex.from_frame(
            seeds[["source_platform", "item_id"]].astype("string[pyarrow]")
        )
        seeds = seeds[~seed_keys.isin(real_keys)].copy()

    if len(seeds) == 0:
        return scrape_df

    seeds["scraped_ok"] = pd.Series(False, index=seeds.index, dtype="bool[pyarrow]")
    seeds["video_downloaded"] = pd.Series(False, index=seeds.index, dtype="bool[pyarrow]")
    seeds["storage_link"] = pd.Series("", index=seeds.index, dtype="string[pyarrow]")

    if verbose:
        logger.info(f"    Adding {len(seeds):,} donated seed row(s) for items without a real scrape.")
    return pd.concat([scrape_df, seeds], ignore_index=True)




# Backstage/provenance columns that change on every (re-)scrape without altering
# any analysis variable. They are excluded from the consolidation value diff so a
# value-preserving re-scrape (or a plain force re-consolidation) flags nothing,
# while a real backfill — e.g. play_count -1 sentinel → a real count — is caught.
_SCRAPE_PROVENANCE_COLS = frozenset({
    "scrape_ts",
    "scrape_contract_version",
    "storage_link",
})




def _scrape_value_signatures(df: pd.DataFrame, value_cols: list[str]) -> dict[str, str]:
    """Per-item content signature over the given value columns.

    Normalises every cell to a string (so pyarrow/int/bool/datetime dtypes and
    NA hash identically across the freshly-built frame and the frame loaded back
    from parquet), hashes each row, and combines the (order-independent) set of
    row hashes per ``item_id`` into a comparable signature string. Rows with a
    null ``item_id`` are ignored.

    Args:
        df: Consolidated scrape frame (existing or new).
        value_cols: Columns whose values define "changed"; provenance columns
            must already be excluded by the caller.

    Returns:
        ``{item_id: signature}`` — two frames agree on an item iff its signature
        matches.
    """
    if df is None or df.empty or "item_id" not in df.columns:
        return {}

    sub = df.loc[df["item_id"].notna(), ["item_id", *value_cols]]
    if sub.empty:
        return {}

    normalised = sub[value_cols].astype("string").fillna("\x00")
    row_hashes = pd.util.hash_pandas_object(normalised, index=False).to_numpy()
    frame = pd.DataFrame({
        "item_id": sub["item_id"].astype("string").to_numpy(),
        "row_hash": [format(int(h), "x") for h in row_hashes],
    })
    combined = frame.groupby("item_id", sort=False)["row_hash"].agg(
        lambda hashes: ",".join(sorted(hashes))
    )
    return {str(item): sig for item, sig in combined.items()}




def _compute_changed_scrape_ids(
    existing_df: pd.DataFrame | None,
    new_df: pd.DataFrame,
    verbose: bool = False,
) -> set[str]:
    """Item_ids whose consolidated scrape row changed vs the previous output.

    Returns the union of brand-new item_ids (present in ``new_df``, absent from
    ``existing_df``) and item_ids present in both whose value columns differ —
    the re-scrape backfill case (updating stale/missing scraped fields for items
    already consolidated) that a pure new-id set-difference silently misses. The
    result drives the consolidation impact analysis, so any study whose member
    items had their enrichment *values* updated is refreshed, not only studies
    that gained or lost members.

    A change in the value-column SET itself (a contract migration renaming or
    coalescing columns, or a new platform's first columns) marks every item as
    changed: the per-row signatures only cover the column intersection, so a
    pure schema change would otherwise diff as "nothing changed" and the
    downstream study refresh would never pick up the new columns.

    Args:
        existing_df: The previously consolidated scrape frame (``None`` on the
            first-ever consolidation).
        new_df: The freshly consolidated scrape frame about to be saved.
        verbose: Print a one-line changed/new/updated breakdown.

    Returns:
        The set of changed item_ids.
    """
    if new_df is None or new_df.empty or "item_id" not in new_df.columns:
        return set()

    if existing_df is None or existing_df.empty or "item_id" not in existing_df.columns:
        return {str(i) for i in new_df.loc[new_df["item_id"].notna(), "item_id"]}

    def _value_col_set(df: pd.DataFrame) -> set[str]:
        return {c for c in df.columns if c != "item_id" and c not in _SCRAPE_PROVENANCE_COLS}

    if _value_col_set(new_df) != _value_col_set(existing_df):
        if verbose:
            added = sorted(_value_col_set(new_df) - _value_col_set(existing_df))
            removed = sorted(_value_col_set(existing_df) - _value_col_set(new_df))
            logger.info(f"Scrape column set changed (+{added} / -{removed}) — flagging all items as changed.")
        return {str(i) for i in new_df.loc[new_df["item_id"].notna(), "item_id"]}

    value_cols = [
        c for c in new_df.columns
        if c != "item_id"
        and c not in _SCRAPE_PROVENANCE_COLS
        and c in existing_df.columns
    ]
    if not value_cols:
        existing_ids = {str(i) for i in existing_df["item_id"] if pd.notna(i)}
        return {
            str(i) for i in new_df["item_id"]
            if pd.notna(i) and str(i) not in existing_ids
        }

    old_sig = _scrape_value_signatures(existing_df, value_cols)
    new_sig = _scrape_value_signatures(new_df, value_cols)
    changed = {item for item, sig in new_sig.items() if old_sig.get(item) != sig}

    if verbose:
        new_count = sum(1 for item in changed if item not in old_sig)
        logger.info(
            f"Found {len(changed):,} changed scrape item_id(s) "
            f"({new_count:,} new, {len(changed) - new_count:,} re-scraped/updated)."
        )
    return changed






def consolidate_and_save_scrape_data(
    force_consolidation: bool = False,
    return_saved_data: bool = True,
    verbose: bool = False,
    ):



    top_verbose = True

    # There is no need to look for raw scrape files. Contrary to activity data
    # and annotations, the scrape files are recoded and immediately after the scrape 

    if top_verbose:
        logger.info("Checking for new scrape files for consolidation...")

    # check if there are any changes in the relevant folder compared to last time this process was run.    
    if data_io.exists(storage_location="recoded",filename="consolidated_enrichment_files.json",verbose=verbose):
        dataset_meta = data_io.load_json(storage_location="recoded",filename="consolidated_enrichment_files.json",verbose=verbose)
        if verbose:
            logger.info("Dataset meta loaded")
    else:
        dataset_meta = {_scrapes_label(): {"filenames": []}}

    files_to_concatenate = []
    for fn in data_io.listdir(storage_location="scrape"):
        if fn.startswith(_scrapes_label()) and fn.endswith(".parquet"):
            files_to_concatenate.append(fn)

    # Donated enrichment seeds participate in change detection: a fresh ingest
    # grows a seed file's row count without adding a scrapes_* parquet, and
    # must still trigger consolidation.
    seed_frames = _load_enrichment_seeds(verbose=verbose)
    seed_row_counts = {fn: len(df) for fn, df in seed_frames.items()}

    latest_filename_list = dataset_meta.get(_scrapes_label(), {}).get("filenames", [])
    latest_seed_row_counts = dataset_meta.get(_scrapes_label(), {}).get("seed_row_counts", {})
    # A scrape-contract change (new sv_) must rebuild even with no new files:
    # the per-file self-healing migrations (retired-column coalesce, legacy
    # renames) only run inside a rebuild, so skipping would leave the
    # consolidated parquet on the previous contract's column set forever.
    from fyp.scrape import scrape_versioning
    current_sv = scrape_versioning.active_scrape_version()
    latest_sv = dataset_meta.get(_scrapes_label(), {}).get("scrape_contract_version")
    if (not force_consolidation
            and set(files_to_concatenate) <= set(latest_filename_list)
            and seed_row_counts == latest_seed_row_counts
            and latest_sv == current_sv):
        if top_verbose:
            logger.info("No new scrape files found. No need to consolidate.")
        if return_saved_data:
            if data_io.exists(storage_location="recoded", filename=f"{_scrapes_label()}_recoded.parquet"):
                if verbose: logger.info("Returning existing file.")
                return False, data_io.load_parquet(storage_location="recoded", filename=f"{_scrapes_label()}_recoded.parquet"), set()
            if verbose: logger.info("No existing consolidated file — returning empty.")
            return False, pd.DataFrame(), set()
        return False, None, set()

    
    # ---------------------------------------------------------------
    if top_verbose:
        logger.info("Loading scrape files...")
    many_scrape_dfs = []
    scraper = get_scraper(verbose=False)
    for fn in files_to_concatenate:
        df = data_io.load_parquet(storage_location="scrape", filename=fn)
        # Fold retired platform-specific columns into their generic successors
        # BEFORE the legacy migration: its rate re-derivation reads the generic
        # count names via the flat [perk] map.
        df = _coalesce_retired_columns(df)
        # Migrate legacy (pre-canonical) parquets to the canonical schema; a no-op
        # for files already saved with canonical names.
        df = _canonicalize_legacy_scrape(df, filename=fn, scraper=scraper)
        many_scrape_dfs.append(df)
        if verbose:
            logger.info(f"{fn} {df.shape}")

    if top_verbose:
        logger.info(f"Consolidating {len(many_scrape_dfs):,} scrape files (dropping duplicate items)...")
    if many_scrape_dfs:
        scrape_df = pd.concat(many_scrape_dfs, ignore_index=True)
    else:
        # No real scrapes yet (e.g. a fresh platform with only donated seeds) —
        # start from an empty frame with the columns downstream steps touch.
        scrape_df = pd.DataFrame({
            "item_id": pd.Series([], dtype="string[pyarrow]"),
            "source_platform": pd.Series([], dtype="string[pyarrow]"),
            "video_downloaded": pd.Series([], dtype="bool[pyarrow]"),
        })

    # Backfill source_platform for rows scraped before the column existed.
    # Canonical-era files skip _canonicalize_legacy_scrape's rename path, so the
    # fill has to happen here; all pre-column history is TikTok by definition.
    backfill_platform = sc.default_platform(sc.load_contract()) or "tiktok"
    if "source_platform" not in scrape_df.columns:
        scrape_df["source_platform"] = pd.NA
    scrape_df["source_platform"] = (
        scrape_df["source_platform"].fillna(backfill_platform).astype("string[pyarrow]")
    )

    # Repair rows scraped before derive_plays_per_day masked the -1 missing-count
    # sentinel: a negative plays_per_day is impossible by construction, so mask to
    # NA. Per-file parquets keep the bad values but are re-masked on every load,
    # exactly like the legacy-column migration.
    if "plays_per_day" in scrape_df.columns:
        scrape_df["plays_per_day"] = scrape_df["plays_per_day"].mask(
            scrape_df["plays_per_day"] < 0, pd.NA
        )


    # -------------------------------------------------
    # There may be some items listed twice - once as video_downloaded and once as not
    # This code addresses that issue
    # -------------------------------------------------

    # deduplicate based on item_id but if there are both a true and a false video_downloaded status, keep both.
    # Sort newest scrape first so a re-scrape supersedes the older row (file order
    # is lexicographic ≈ oldest-first, and keep="first" would otherwise pin the
    # stale row forever).
    if "scrape_ts" in scrape_df.columns:
        scrape_df = scrape_df.sort_values(
            "scrape_ts", ascending=False, kind="mergesort", na_position="last"
        )
    scrape_df = scrape_df.drop_duplicates(subset=["source_platform","item_id","video_downloaded"]).copy()
    if verbose:
        logger.info(f"    Dropping duplicates based on items and whether the video is downloaded or not: {scrape_df.shape}")

    # identify items with inconsistent video_downloaded status
    items_w_inconsistent_video_download_status = scrape_df["item_id"].value_counts()
    items_w_inconsistent_video_download_status = items_w_inconsistent_video_download_status[items_w_inconsistent_video_download_status>1].index.tolist()

    # use the list generated above to separate items with consistent vs inconsistent video download status
    items_w_consistent_video_download_status = scrape_df[~scrape_df['item_id'].isin(items_w_inconsistent_video_download_status)].copy()
    items_w_inconsistent_video_download_status = scrape_df[scrape_df['item_id'].isin(items_w_inconsistent_video_download_status)].copy()
    if verbose:
        logger.info("    Identifying conflicting items in the dataset listed twice - once as video_downloaded and once as not")
        logger.info(
            f"    There are {len(items_w_inconsistent_video_download_status):,} items with such inconsistencies, "
            f"and {len(items_w_consistent_video_download_status):,} that look alright.")

    if len(items_w_inconsistent_video_download_status)>0:
        # for items with inconsistent video download status, only keep the ones where video_downloaded is True
        items_w_inconsistent_video_download_status = items_w_inconsistent_video_download_status[items_w_inconsistent_video_download_status['video_downloaded']].copy()
        if verbose:
            logger.info("    Fixed the inconsistencies by keeping the one of the pairs with video_download=True")
            logger.info(f"    This reduces the number of inconsistent items to {len(items_w_inconsistent_video_download_status)}")

        # recombine the two dataframes
        scrape_df = pd.concat([items_w_consistent_video_download_status,items_w_inconsistent_video_download_status])


    # ---------------------------------------------------------------
    # Donated enrichment seeds — lowest-precedence fallback rows for
    # items with no real scrape (anti-join on source_platform+item_id).
    # ---------------------------------------------------------------
    scrape_df = _merge_enrichment_seeds(scrape_df, seed_frames, verbose=top_verbose)


    memory_per_column = scrape_df.memory_usage(deep=True)
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    if top_verbose:
        logger.info(f"Shape: {scrape_df.shape} | Memory usage: {total_memory_mb:.2f} MB")


    # Count-overflow repair, per-K engagement rates, and plays_per_day are now
    # produced at scrape time (BaseScraper.canonicalize_batch) and back-filled for
    # any legacy parquet by _canonicalize_legacy_scrape at load, so consolidation
    # only needs to concatenate and deduplicate.

    # Compute changed item_ids by diffing against the existing consolidated data.
    # A set-difference on item_id alone only sees brand-new items; a re-scrape
    # that updates the VALUES of an item already consolidated (e.g. an Instagram
    # play_count going from the -1 sentinel to a real count) keeps the same
    # item_id and would be missed, so the study/collection impact analysis would
    # never refresh the studies that item belongs to. Compare the actual row
    # values instead, so any enrichment-value backfill surfaces as a change.
    existing_recoded_fn = f"{_scrapes_label()}_recoded.parquet"
    existing_df = None
    if data_io.exists(storage_location="recoded", filename=existing_recoded_fn):
        existing_df = data_io.load_parquet(storage_location="recoded", filename=existing_recoded_fn)

    new_item_ids = _compute_changed_scrape_ids(existing_df, scrape_df, verbose=top_verbose)

    if top_verbose:
        logger.info("Saving consolidated scrape data...")
    _ = data_io.save_parquet(df=scrape_df, storage_location="recoded", filename=existing_recoded_fn)


    # update the dataset meta file
    if _scrapes_label() not in dataset_meta:
        dataset_meta[_scrapes_label()] = {}
    dataset_meta[_scrapes_label()]["filenames"] = files_to_concatenate
    dataset_meta[_scrapes_label()]["seed_row_counts"] = seed_row_counts
    dataset_meta[_scrapes_label()]["scrape_contract_version"] = current_sv
    _ = data_io.save_json(data=dataset_meta, storage_location="recoded", filename="consolidated_enrichment_files.json")

    if top_verbose:
        logger.info("...done")



    return True, scrape_df, new_item_ids










def _merge_failed_scrape_records(
    records: dict[str, str | None],
    raw: list) -> None:
    """Merge one loaded failed-scrapes file into ``records`` in place.

    Two on-disk shapes coexist. Records written before the category was
    recorded are bare item-id strings carrying no reason; newer ones are
    ``{"item_id": ..., "category": "permanent:ip_blocked"}`` dicts. A known
    category always wins over a legacy id for the same item.

    Args:
        records: Accumulator mapping item id to category (``None`` if unknown).
        raw: The parsed contents of one failed-scrapes JSON file.
    """
    for entry in raw:
        if isinstance(entry, dict):
            item_id = entry.get("item_id")
            if item_id is not None:
                records[str(item_id)] = entry.get("category")
        else:
            records.setdefault(str(entry), None)






def _load_failed_scrape_records(
    verbose = False,
    super_verbose = False) -> dict[str, str | None]:
    """Load every recorded failed scrape as ``{item_id: category}``.

    Consolidates multiple on-disk records into one file and archives the
    originals, exactly as before; the consolidated file is written in the
    category-carrying shape, so a legacy id whose reason was never recorded
    survives consolidation with a ``None`` category rather than being dropped.

    Args:
        verbose: Log progress.
        super_verbose: Log each file name as it is read.

    Returns:
        Mapping of item id to its recorded category, ``None`` when unknown.
    """
    if verbose:
        logger.info("Loading failed scrapes...")

    failed_scrapes_files = [gg for gg in data_io.listdir(storage_location="scrape", verbose=verbose) if gg.startswith(_failed_scrapes_label())]

    records: dict[str, str | None] = {}
    for fn in failed_scrapes_files:
        if super_verbose:
            logger.info(fn)
        some_dict = data_io.load_json(storage_location="scrape", filename=fn, verbose=verbose)
        if some_dict is not None:
            _merge_failed_scrape_records(records, some_dict)

    if len(failed_scrapes_files) > 1:
        fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
        if verbose:
            logger.info(f"{len(records):,} of these are unique and will be saved as a new consolidated file {_failed_scrapes_label()}_{fine_ts}.json.")

        payload = [{"item_id": item_id, "category": category} for item_id, category in records.items()]
        result = data_io.save_json(data=payload, storage_location="scrape", filename=f"{_failed_scrapes_label()}_{fine_ts}.json", verbose=verbose)

        if result == 0:
            for fn in failed_scrapes_files:
                data_io.move(src_storage_location="scrape", dst_storage_location="archive", filename=fn, verbose=verbose)
                if verbose:
                    logger.info(f"Moved {fn} to archive")

    if verbose:
        logger.info(f"Loaded list of all failed scrapes: {len(records):,}")

    return records






def load_failed_scrapes(
    verbose = False,
    super_verbose = False):
    # Load list of failed scraped attempts.

    return list(_load_failed_scrape_records(verbose=verbose, super_verbose=super_verbose))






def load_failed_scrapes_detail(
    verbose = False,
    super_verbose = False) -> dict[str, str | None]:
    """Load failed scrapes with the reason each one failed.

    Use this instead of :func:`load_failed_scrapes` to select one kind of
    failure — e.g. re-queueing only the ``permanent:ip_blocked`` items after
    the scraper gains a different vantage point.

    Args:
        verbose: Log progress.
        super_verbose: Log each file name as it is read.

    Returns:
        Mapping of item id to its recorded category. ``None`` marks a record
        written before categories were stored, whose reason is unrecoverable.
    """
    return _load_failed_scrape_records(verbose=verbose, super_verbose=super_verbose)




