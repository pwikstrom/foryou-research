#!/usr/bin/env python3
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""



import json
import os
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
import fyp.scrape_queues as scrape_queues
from fyp import scrape_contract as sc
from fyp import scrape_versioning
from fyp.fyp_config import fyp_cf
from fyp.platform_scraper import SLIDESHOW_SECONDS_PER_IMAGE, ThrottleController, get_scraper
from fyp.recode_variables import recode_events_df, rename_columns
from fyp.utils import chunk_list, record_dropped_columns, start_monitor

SCRAPES_LABEL = fyp_cf["labels"]["SCRAPES_LABEL"]
FAILED_SCRAPES_LABEL = fyp_cf["labels"]["FAILED_SCRAPES_LABEL"]





def _check_graceful_stop(process_name: str) -> bool:
    """Check if a graceful stop has been requested via sentinel file."""
    sentinel = Path(fyp_cf['paths']['project_root']) / "tmp" / "graceful_stop" / f"{process_name}.stop"
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



    def _fit_letterbox(img_clip, canvas_size):
        W, H = canvas_size
        iw, ih = img_clip.w, img_clip.h
        if iw / ih >= W / H:
            scaled = img_clip.resized(width=W)
        else:
            scaled = img_clip.resized(height=H)
        return scaled



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
        img = ImageClip(image_path, duration=duration)
        boxed = _fit_letterbox(img, canvas_size)

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



    # Main function logic starts here
    if not files:
        raise ValueError("No input files provided")

    bg_color = _normalize_color(bg_color)

    if canvas_size is None:
        canvas_size = _infer_canvas_size(files)

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
            print(f"Dry run: would have downloaded video {video_id}")
        return video_id


    if video_id is None:
        raise ValueError("No video id specified")

    use_gcs = fyp_cf['data_io']['use_gcs_for_media']
    bucket = fyp_cf['data_io']['bucket']
    min_size = fyp_cf["misc"]["min_media_object_size"]
    temp_dir = fyp_cf["paths"]["temp"]

    if use_gcs and bucket is None:
        raise ValueError("No GCS bucket specified")

    if scraper is None:
        scraper = get_scraper(platform, verbose=verbose)

    # Media lives under a per-platform subpath ({prefix}/{platform}/{id}.mp4);
    # legacy flat files are found by the reader-side fallback, never written.
    media_prefix = f"{fyp_cf['data_io']['gcs_media_prefix']}/{scraper.platform}"
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
                    print(f"OK   - Photos downloaded - '{video_id}' - {col_count} metadata fields")

                if use_gcs:
                    # GCS path: check bucket, download jpegs to temp, assemble, upload
                    blob = bucket.blob(f"{media_prefix}/{video_id}.mp4")
                    if blob.exists():
                        if verbose:
                            print("Photo slideshow already in bucket")
                        scrape_metadata.loc[0,'video_downloaded'] = True
                    else:
                        if verbose:
                            print("Converting photos to video slideshow")

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

                        # Audio is optional: any failure yields a silent slideshow.
                        try:
                            audio_path = scraper.fetch_slideshow_audio(video_id, temp_dir)
                        except Exception:
                            audio_path = None

                        temp_mp4 = os.path.join(temp_dir, f"{video_id}.mp4")
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

                        if os.path.getsize(temp_mp4) > min_size:
                            if verbose:
                                print("Uploading video file to storage bucket...")
                            blob = bucket.blob(f"{media_prefix}/{video_id}.mp4")
                            blob.upload_from_filename(temp_mp4)
                            scrape_metadata.loc[0,'video_downloaded'] = True
                            # Source jpegs are no longer needed once the mp4 is
                            # stored (parity with the local branch's cleanup).
                            for name in source_blob_names:
                                try: bucket.blob(name).delete()
                                except Exception: pass
                        else:
                            if verbose:
                                print("Generated video file is too small, not uploading.")
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
                            print("Photo slideshow already exists locally")
                        scrape_metadata.loc[0,'video_downloaded'] = True
                    else:
                        if verbose:
                            print("Converting photos to video slideshow")

                        ccc = 1
                        image_files = []
                        while True:
                            cand = os.path.join(platform_media_dir, f"{video_id}_{ccc:02}.jpeg")
                            if not os.path.exists(cand):
                                break
                            if os.path.getsize(cand) >= min_size:
                                image_files.append(cand)
                            ccc += 1

                        # Audio is optional: any failure yields a silent slideshow.
                        try:
                            audio_path = scraper.fetch_slideshow_audio(video_id, temp_dir)
                        except Exception:
                            audio_path = None

                        temp_mp4 = os.path.join(temp_dir, f"{video_id}.mp4")
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
                                print("Moving slideshow to media folder...")
                            os.replace(temp_mp4, final_mp4)
                            scrape_metadata.loc[0,'video_downloaded'] = True
                        else:
                            if verbose:
                                print("Generated video file is too small, discarding.")
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
                    print(f"OK   - Video downloaded '{video_id}' - {col_count} metadata fields")

                if use_gcs:
                    # check if it truly is stored and is big enough
                    if verbose:
                        print("Checking video file in bucket")
                    if bucket.blob(f"{media_prefix}/{video_id}.mp4").exists():
                        blob = bucket.get_blob(f"{media_prefix}/{video_id}.mp4")
                        if blob.size < min_size:
                            if verbose:
                                print(f"   - Deleting video file smaller than threshold: {blob.name} of size {blob.size} bytes")
                            blob.delete()
                            scrape_metadata.loc[0,'video_downloaded'] = False
                        if verbose:
                            print(f"   - Video file {blob.name} of size {blob.size:,} bytes is okay")
                    else:
                        if verbose:
                            print("   - WARNING: File not found")
                        scrape_metadata.loc[0,'video_downloaded'] = False
                else:
                    if verbose:
                        print("Checking video file in local media folder")
                    local_mp4 = os.path.join(platform_media_dir, f"{video_id}.mp4")
                    if os.path.exists(local_mp4):
                        local_size = os.path.getsize(local_mp4)
                        if local_size < min_size:
                            if verbose:
                                print(f"   - Deleting video file smaller than threshold: {local_mp4} of size {local_size} bytes")
                            try: os.remove(local_mp4)
                            except OSError: pass
                            scrape_metadata.loc[0,'video_downloaded'] = False
                        else:
                            if verbose:
                                print(f"   - Video file {local_mp4} of size {local_size:,} bytes is okay")
                    else:
                        if verbose:
                            print("   - WARNING: File not found")
                        scrape_metadata.loc[0,'video_downloaded'] = False

            return scrape_metadata
        
        # if metadata is downloaded but no video is downloaded
        elif col_count > 1 and scrape_metadata.loc[0,'video_downloaded']==False:
            if verbose:
                print(f"Accessed {col_count} metadata fields for {video_id} but did not download media object(s)")
            return scrape_metadata
        else:
            if verbose:
                print(f"Insufficient metadata columns ({col_count}) - Download of {video_id} - failed")

    except Exception as e:
        print(e)

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
    use_gcs = fyp_cf['data_io']['use_gcs_for_media']
    if use_gcs:
        bucket = fyp_cf['data_io'].get('bucket')
        prefix = fyp_cf['data_io']['gcs_media_prefix']
        bucket_name = getattr(bucket, "name", "") if bucket is not None else ""
        def _link(vid: str) -> str:
            return f"gs://{bucket_name}/{prefix}/{media_paths.media_relpath(platform, vid)}"
    else:
        media_dir = fyp_cf['paths']['media']
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

    Shared by ``download_video_threads`` and ``rescue_meta_threads``.
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
    scrape_filename = f"{SCRAPES_LABEL}_{fine_ts}.parquet"

    # save the raw results to local temp just in case everything goes to pieces
    results.to_parquet(os.path.join(fyp_cf['paths']['temp'], "recovered_" + scrape_filename))

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
        dropped = sorted(set(results.columns) - set(fyp_cf['var_schema'].variable_name))
        relevant_cols = [c for c in fyp_cf['var_schema'].variable_name if c in results.columns]
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
        print(f"Saved {len(results):,} rows to '{scrape_filename}'. Media downloaded for {len(results[results['video_downloaded']]):,} of these.")

    except Exception as e:
        print(f"CRITICAL: Failed to save results to parquet: {e}")
        print("Recovering the un-processed results from temp")
        data_io.move(
            src_storage_location="temp",
            dst_storage_location="scrape",
            filename="recovered_" + scrape_filename,
            verbose=verbose,
        )

    return results




def rescue_meta_threads(
    interesting_videos:list[str] = None,
    max_workers:int = 4,
    verbose:bool = False,
    dry_run:bool = False,
    batch_label: str | None = None,
    cumulative_done: int = 0,
    cumulative_total: int = 0,
    reporter=None,
    platform: str | None = None):



    if dry_run:
        print("********* This is a dry run. It's all fake. No data io action at all. *********")
    else:
        if interesting_videos is None:
            raise ValueError("No interesting videos specified")

        if len(interesting_videos) == 0:
            return pd.DataFrame()

    results_by_index = {}
    scraper = get_scraper(platform, verbose=verbose)

    def worker(idx_video):
        idx, video = idx_video
        return idx, download_single_video(
            video_id = video,
            verbose=verbose,
            save_video=False,
            dry_run=dry_run,
            scraper=scraper)


    if verbose:
        print(f"dry_run: {dry_run}")
        print(f"Scraping data for {len(interesting_videos)} items with {max_workers} threads.")


    with ThreadPoolExecutor(max_workers=max_workers) as ex:


        futures = []
        submit_times = {}
        for iv in enumerate(interesting_videos):
            fut = ex.submit(worker, iv)
            futures.append(fut)
            submit_times[fut] = time.time()


        monitor_thread = start_monitor(
            futures, submit_times, interval=5, label="dl", bar_width=32,
            result_checker=lambda f: isinstance(f.result()[1], pd.DataFrame),
            batch_label=batch_label,
            cumulative_done=cumulative_done,
            cumulative_total=cumulative_total
        )


        for fut in as_completed(futures):
            idx, res = fut.result()
            results_by_index[idx] = res
        
        monitor_thread.join()

    results = []
    failed_items = []
    permanent_failed_ids: list[str] = []
    transient_failed_ids: list[str] = []
    for idx in range(len(interesting_videos)):
        res = results_by_index.get(idx)
        if isinstance(res, pd.DataFrame) and res.shape[1] > 10:
            results += [res]
        else:
            vid = interesting_videos[idx]
            failed_items += [vid]
            error_type = res.attrs.get('error_type', 'unknown') if isinstance(res, pd.DataFrame) else 'unknown'
            # The scraper owns its platform's permanent-vs-transient taxonomy.
            if scraper.classify_error(error_type).startswith('permanent'):
                permanent_failed_ids.append(vid)
            else:
                transient_failed_ids.append(vid)

    if permanent_failed_ids or transient_failed_ids:
        print(f"  Failures: {len(permanent_failed_ids)} permanent, "
              f"{len(transient_failed_ids)} transient (will retry)")

    if len(results)==0:
        print("The scrape procedure did not generate any useful results")
        return pd.DataFrame(), permanent_failed_ids, transient_failed_ids

    results = pd.concat(results)

    fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
    
    if not dry_run and len(results)>0:
        results = _canonicalize_recode_save(results, scraper, fine_ts, verbose=verbose, reporter=reporter)

    if not dry_run and len(failed_items)>0:
        data_io.save_json(data = failed_items, storage_location="scrape", filename=f"{FAILED_SCRAPES_LABEL}_{fine_ts}.json", verbose=verbose)
        print(f"Saved {len(failed_items)} failed items")

    return results, permanent_failed_ids, transient_failed_ids






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

    use_gcs = fyp_cf['data_io']['use_gcs_for_media']
    min_size = fyp_cf['misc']['min_media_object_size']
    relpaths = [media_paths.media_relpath(platform, "{vid}")] if platform else []
    relpaths.append("{vid}.mp4")

    if use_gcs:
        bucket = fyp_cf['data_io']['bucket']
        gcs_media_prefix = fyp_cf['data_io']['gcs_media_prefix']
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

    media_dir = fyp_cf['paths']['media']
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
        print("********* This is a dry run. It's all fake. No data io action at all. *********")
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
            print(
                f"  {len(already_have_media)}/{len(interesting_videos)} items "
                f"already have media — will do metadata-only scrape for those"
            )

    results_by_index = {}
    # Record the active scrape-contract version once per batch (idempotent, non-raising).
    scrape_versioning.ensure_current_version_registered()
    # Concurrency bounds are platform policy (e.g. TikTok caps at 6: a single
    # session behind cookies trips behavioural flags beyond that).
    throttle_initial, throttle_min, throttle_max = scraper.throttle_limits(max_workers)
    throttle = ThrottleController(
        initial=throttle_initial, minimum=throttle_min, maximum=throttle_max,
        on_change=on_concurrency_change)

    def worker(idx_video):
        idx, video = idx_video
        throttle.acquire()
        try:
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
            # Report outcome to throttle controller
            if isinstance(res, pd.DataFrame) and res.empty:
                error_cat = res.attrs.get('error_type')
            else:
                error_cat = None
            throttle.report_result(error_cat)
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
        print(f"dry_run: {dry_run}")
        print(f"Scraping data for {len(interesting_videos)} items with {max_workers} threads.")

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
            result_checker=lambda f: isinstance(f.result()[1], pd.DataFrame),
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

        try:
            for fut in as_completed(futures, timeout=batch_deadline):
                idx, res = fut.result()
                results_by_index[idx] = res
        except TimeoutError:
            stuck = [interesting_videos[i] for i, f in enumerate(futures) if not f.done()]
            print(
                f"  [scrape] Batch deadline of {batch_deadline}s exceeded; "
                f"{len(stuck)} worker(s) did not finish: {stuck[:5]}"
                + (" ..." if len(stuck) > 5 else ""),
                flush=True,
            )
            # Record DNF items as empty DataFrames (failures)
            for i in range(len(interesting_videos)):
                if i not in results_by_index:
                    empty = pd.DataFrame()
                    empty.attrs['error_type'] = 'timeout'
                    results_by_index[i] = empty

        monitor_thread.join(timeout=5)

    if throttle.total_throttle_events > 0:
        print(f"  Throttle: {throttle.total_throttle_events} rate-limit events, "
              f"final concurrency: {throttle.current}")

    results = []
    failed_items = []
    permanent_failed_ids: list[str] = []
    transient_failed_ids: list[str] = []
    for idx in range(len(interesting_videos)):
        res = results_by_index.get(idx)
        if isinstance(res, pd.DataFrame) and res.shape[1] > 10:
            results += [res]
        else:
            vid = interesting_videos[idx]
            failed_items += [vid]
            error_type = res.attrs.get('error_type', 'unknown') if isinstance(res, pd.DataFrame) else 'unknown'
            # The scraper owns its platform's permanent-vs-transient taxonomy.
            if scraper.classify_error(error_type).startswith('permanent'):
                permanent_failed_ids.append(vid)
            else:
                transient_failed_ids.append(vid)

    if permanent_failed_ids or transient_failed_ids:
        print(f"  Failures: {len(permanent_failed_ids)} permanent, "
              f"{len(transient_failed_ids)} transient (will retry)")

    if len(results)==0:
        print("The scrape procedure did not generate any useful results")
        return pd.DataFrame(), permanent_failed_ids, transient_failed_ids

    results = pd.concat(results)

    fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
    
    if not dry_run and len(results)>0:
        results = _canonicalize_recode_save(
            results, scraper, fine_ts, verbose=verbose, reporter=reporter,
            storage_link_overrides=already_have_media,
        )

    if not dry_run and len(failed_items)>0:
        data_io.save_json(data = failed_items, storage_location="scrape", filename=f"{FAILED_SCRAPES_LABEL}_{fine_ts}.json", verbose=verbose)
        print(f"Saved {len(failed_items)} failed items")

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



    print(f"    Downloading media objects and metadata for selected videos, batch size: {batch_size}, max batches: {max_batches}")
    print(f"    Now: {datetime.now()}")


    batch_number = 1

    batch_target = min(max_batches, len(video_list) // batch_size + 1)

    print(f"  Starting loop... There are {len(video_list):,} videos to process in {batch_target:,} batches")

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
        print(f"  Batch {batch_label}")

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

        with open(os.path.join(fyp_cf['paths']['temp'], "temp_failed_scrapes.json"), "w") as f:
            json.dump(all_permanent_failed + all_transient_failed, f)
        with open(os.path.join(fyp_cf['paths']['temp'], "temp_good_scrapes.json"), "w") as f:
            json.dump(good_scrapes, f)

        cumulative_done += len(batch)

        # Emit queue update after each batch (for web UI)
        queue_remaining = len(video_list) - cumulative_done
        if reporter is not None:
            reporter.emit_data({"scrape_queue_len": max(0, queue_remaining)})
        elif "WEB_INTERFACE" in os.environ:
            print(f"::DATA::{{\"scrape_queue_len\": {max(0, queue_remaining)}}}", flush=True)

        if max_batches is not None and batch_number >= max_batches:
            break

        # Check for cancellation request
        if cancellation_check is not None:
            if cancellation_check():
                print("  Cancellation requested. Finishing after this batch.")
                break
        elif _check_graceful_stop(stop_key):
            print("  Graceful stop requested. Finishing after this batch.")
            break

        batch_number += 1

        if dry_run:
            break

    # ----------------
    # Update scrape queue: remove successful + permanently failed items.
    # Transient failures stay in the queue for retry on next run.
    # -----------------
    items_to_remove = set(good_scrapes + all_permanent_failed)
    if items_to_remove:
        pruned, remaining = scrape_queues.prune_scrape_queue(platform_resolved, items_to_remove)
        if pruned:
            print(f"  Queue update: removed {pruned} "
                  f"({len(good_scrapes)} OK, {len(all_permanent_failed)} permanent fail). "
                  f"{len(all_transient_failed)} transient failures remain for retry. "
                  f"Queue length: {remaining}")


    print(f"  Loop ended: {datetime.now()}")
    return good_scrapes, all_permanent_failed, all_transient_failed










def scraper_loop(
    study_name = None,
    study_dataset = None,
    load_from_cache = True,
    batch_size = 500,
    max_batches = None,
    verbose = False,
    dry_run = False
    ):

    # Imported inside function to avoid circular import: organize_datasets imports from fyp.scrape
    from fyp.organize_datasets import create_study_recoded_dataset, select_videos_from_study_dataset

    #max_batches = max_batches if max_batches is not None else np.inf

    if study_name is None and study_dataset is None:
        print("    ERROR: This process cannot run without a study name or a study dataset as input. Process failed.")
        return None


    if load_from_cache and study_name is not None:
        if data_io.exists(
            storage_location="cache",
            filename=f"{study_name}_recoded.parquet",
            verbose=verbose
            ):
            if verbose:
                print("    Loading study dataset from cache", end=" ", flush=True)
            study_dataset = data_io.load_parquet(
                storage_location="cache",
                filename=f"{study_name}_recoded.parquet",
                verbose=verbose
                )
            print(study_dataset.attrs['study_name'])
            if verbose:
                print(f"  |  Shape: {study_dataset.shape}")
        else:
            if verbose:
                print("    No cached study dataset found. I must run the process to create it. Please wait a moment...")
            study_dataset = create_study_recoded_dataset(
                study_name = study_name,
                load_from_cache = True,
                save_to_cache = True,
                verbose = verbose
            )


    if study_dataset is None:
        print("    ERROR: This process cannot run without a study dataset. Process failed.")
        return None


    selected_videos_df = select_videos_from_study_dataset(
        study_dataset = study_dataset,
        query_string = "~scraped_ok & ~scraped_fail",
        verbose = verbose,
        notebook_mode = False
    )


    scraper_loop_from_list(
        video_list = selected_videos_df.index.to_list(),
        batch_size = batch_size,
        max_batches = max_batches,
        verbose = verbose,
        dry_run = dry_run
        )


    print(f"  Loop ended: {datetime.now()}")















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
        print(f"Queue for '{platform_resolved}' is empty or invalid. Nothing to scrape.")
        return

    print(f"Found {len(video_list)} items in '{platform_resolved}' queue.")

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




def consolidate_and_save_scrape_data(
    force_consolidation: bool = False,
    return_saved_data: bool = True,
    verbose: bool = False,
    ):



    top_verbose = True

    # There is no need to look for raw scrape files. Contrary to activity data
    # and annotations, the scrape files are recoded and immediately after the scrape 

    if top_verbose:
        print("Checking for new scrape files for consolidation...")

    # check if there are any changes in the relevant folder compared to last time this process was run.    
    if data_io.exists(storage_location="recoded",filename="consolidated_enrichment_files.json",verbose=verbose):
        dataset_meta = data_io.load_json(storage_location="recoded",filename="consolidated_enrichment_files.json",verbose=verbose)
        if verbose:
            print("Dataset meta loaded")
    else:
        dataset_meta = {SCRAPES_LABEL: {"filenames": []}}

    files_to_concatenate = []
    for fn in data_io.listdir(storage_location="scrape"):
        if fn.startswith(SCRAPES_LABEL) and fn.endswith(".parquet"):
            files_to_concatenate.append(fn)

    latest_filename_list = dataset_meta.get(SCRAPES_LABEL, {}).get("filenames", [])
    if not force_consolidation and set(files_to_concatenate) <= set(latest_filename_list):
        if top_verbose:
            print("No new scrape files found. No need to consolidate.")
        if return_saved_data:
            if data_io.exists(storage_location="recoded", filename=f"{SCRAPES_LABEL}_recoded.parquet"):
                if verbose: print("Returning existing file.")
                return False, data_io.load_parquet(storage_location="recoded", filename=f"{SCRAPES_LABEL}_recoded.parquet"), set()
            if verbose: print("No existing consolidated file — returning empty.")
            return False, pd.DataFrame(), set()
        return False, None, set()

    
    # ---------------------------------------------------------------
    if top_verbose:
        print("Loading scrape files...")
    many_scrape_dfs = []
    scraper = get_scraper(verbose=False)
    for fn in files_to_concatenate:
        df = data_io.load_parquet(storage_location="scrape", filename=fn)
        # Migrate legacy (pre-canonical) parquets to the canonical schema; a no-op
        # for files already saved with canonical names.
        df = _canonicalize_legacy_scrape(df, filename=fn, scraper=scraper)
        many_scrape_dfs.append(df)
        if verbose:
            print(fn, df.shape)

    if top_verbose:
        print(f"Consolidating {len(many_scrape_dfs):,} scrape files (dropping duplicate items)...")
    scrape_df = pd.concat(many_scrape_dfs, ignore_index=True)

    # Backfill source_platform for rows scraped before the column existed.
    # Canonical-era files skip _canonicalize_legacy_scrape's rename path, so the
    # fill has to happen here; all pre-column history is TikTok by definition.
    backfill_platform = sc.default_platform(sc.load_contract()) or "tiktok"
    if "source_platform" not in scrape_df.columns:
        scrape_df["source_platform"] = pd.NA
    scrape_df["source_platform"] = (
        scrape_df["source_platform"].fillna(backfill_platform).astype("string[pyarrow]")
    )


    # -------------------------------------------------
    # There may be some items listed twice - once as video_downloaded and once as not
    # This code addresses that issue
    # -------------------------------------------------

    # deduplicate based on item_id but if there are both a true and a false video_downloaded status, keep both
    scrape_df = scrape_df.drop_duplicates(subset=["source_platform","item_id","video_downloaded"]).copy()
    if verbose:
        print(f"    Dropping duplicates based on items and whether the video is downloaded or not: {scrape_df.shape}")

    # identify items with inconsistent video_downloaded status
    items_w_inconsistent_video_download_status = scrape_df["item_id"].value_counts()
    items_w_inconsistent_video_download_status = items_w_inconsistent_video_download_status[items_w_inconsistent_video_download_status>1].index.tolist()

    # use the list generated above to separate items with consistent vs inconsistent video download status
    items_w_consistent_video_download_status = scrape_df[~scrape_df['item_id'].isin(items_w_inconsistent_video_download_status)].copy()
    items_w_inconsistent_video_download_status = scrape_df[scrape_df['item_id'].isin(items_w_inconsistent_video_download_status)].copy()
    if verbose:
        print("    Identifying conflicting items in the dataset listed twice - once as video_downloaded and once as not")
        print(
            f"    There are {len(items_w_inconsistent_video_download_status):,} items with such inconsistencies, "
            f"and {len(items_w_consistent_video_download_status):,} that look alright.")

    if len(items_w_inconsistent_video_download_status)>0:
        # for items with inconsistent video download status, only keep the ones where video_downloaded is True
        items_w_inconsistent_video_download_status = items_w_inconsistent_video_download_status[items_w_inconsistent_video_download_status['video_downloaded']].copy()
        if verbose:
            print("    Fixed the inconsistencies by keeping the one of the pairs with video_download=True")
            print(f"    This reduces the number of inconsistent items to {len(items_w_inconsistent_video_download_status)}")

        # recombine the two dataframes
        scrape_df = pd.concat([items_w_consistent_video_download_status,items_w_inconsistent_video_download_status])


    memory_per_column = scrape_df.memory_usage(deep=True)
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    if top_verbose:
        print(f"Shape: {scrape_df.shape} | Memory usage: {total_memory_mb:.2f} MB")


    # Count-overflow repair, per-K engagement rates, and plays_per_day are now
    # produced at scrape time (BaseScraper.canonicalize_batch) and back-filled for
    # any legacy parquet by _canonicalize_legacy_scrape at load, so consolidation
    # only needs to concatenate and deduplicate.

    # Compute new item_ids by comparing against existing consolidated data
    new_item_ids: set[str] = set()
    existing_recoded_fn = f"{SCRAPES_LABEL}_recoded.parquet"
    if data_io.exists(storage_location="recoded", filename=existing_recoded_fn):
        existing_df = data_io.load_parquet(storage_location="recoded", filename=existing_recoded_fn)
        existing_ids = set(existing_df["item_id"]) if existing_df is not None else set()
        new_item_ids = set(scrape_df["item_id"]) - existing_ids
    else:
        new_item_ids = set(scrape_df["item_id"])

    if top_verbose and new_item_ids:
        print(f"Found {len(new_item_ids):,} newly scraped item_ids.")

    if top_verbose:
        print("Saving consolidated scrape data...")
    _ = data_io.save_parquet(df=scrape_df, storage_location="recoded", filename=existing_recoded_fn)


    # update the dataset meta file
    if SCRAPES_LABEL not in dataset_meta:
        dataset_meta[SCRAPES_LABEL] = {}
    dataset_meta[SCRAPES_LABEL]["filenames"] = files_to_concatenate
    _ = data_io.save_json(data=dataset_meta, storage_location="recoded", filename="consolidated_enrichment_files.json")

    if top_verbose:
        print("...done")



    return True, scrape_df, new_item_ids










def load_failed_scrapes(
    verbose = False,
    super_verbose = False):
    # Load list of failed scraped attempts.


    if verbose:
        print("Loading failed scrapes...")

    failed_scrapes_files = [gg for gg in data_io.listdir(storage_location="scrape", verbose=verbose) if gg.startswith(FAILED_SCRAPES_LABEL)]

    failed_scrapes = []
    for fn in failed_scrapes_files:
        if super_verbose:
            print(fn)
        some_dict = data_io.load_json(storage_location="scrape", filename=fn, verbose=verbose)
        if some_dict is not None:
            failed_scrapes += some_dict

    failed_scrapes = list(set(map(lambda one_item_id:str(one_item_id), failed_scrapes)))

    if len(failed_scrapes_files) > 1:
        fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
        if verbose:
            print(f"{len(failed_scrapes):,} of these are unique and will be saved as a new consolidated file {FAILED_SCRAPES_LABEL}_{fine_ts}.json.")

        result = data_io.save_json(data=failed_scrapes, storage_location="scrape", filename=f"{FAILED_SCRAPES_LABEL}_{fine_ts}.json", verbose=verbose)

        if result == 0:
            for fn in failed_scrapes_files:
                data_io.move(src_storage_location="scrape", dst_storage_location="archive", filename=fn, verbose=verbose)
                if verbose:
                    print(f"Moved {fn} to archive")

    if verbose:
        print(f"Loaded list of all failed scrapes: {len(failed_scrapes):,}")

    return failed_scrapes




