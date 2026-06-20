#!/usr/bin/env python3
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""



import json
import os
import textwrap
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image, ImageColor

import fyp.data_io as data_io
import fyp.mypyktok as pyk
import fyp.tiktok_dl as tiktok_dl
from fyp.fyp_config import fyp_cf
from fyp.recode_variables import recode_events_df, rename_columns
from fyp.utils import chunk_list, start_monitor

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
    verbose=False
):
    # Creates a slideshow video from a list of image files.

    from moviepy import ColorClip, CompositeVideoClip, ImageClip, concatenate_videoclips
    

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

    final.write_videofile(
        output,
        fps=fps,
        codec=codec,
        audio=False,
        preset=preset,
        threads=0,
        ffmpeg_params=["-crf", str(crf)],
        logger=None
    )

    for s in slides:
        s.close()
    final.close()








def download_single_video(
    video_id: str = None, 
    verbose: bool = True,
    save_video = True,
    dry_run: bool = False,
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
    media_dir = fyp_cf['paths']['media']
    min_size = fyp_cf["misc"]["min_media_object_size"]
    temp_dir = fyp_cf["paths"]["temp"]

    if use_gcs and bucket is None:
        raise ValueError("No GCS bucket specified")

    gcs_media_prefix = fyp_cf['data_io']['gcs_media_prefix']
    scraper_backend = fyp_cf['misc'].get('scraper_backend', 'pyktok')

    # Routing for save_tiktok: GCS mode -> bucket + gcs prefix; local mode -> local dir, no bucket
    save_path_arg = gcs_media_prefix if use_gcs else media_dir
    stream_to_bucket_arg = bucket if use_gcs else None

    tiktok_url = f"https://www.tiktok.com/@/video/{video_id}/"

    # try to scrape metadata and download video
    if scraper_backend == 'ytdlp':
        scrape_metadata = tiktok_dl.save_tiktok(
            tiktok_url,
            save_video=save_video,
            max_duration_to_save=fyp_cf['misc']['max_duration_for_download'],
            save_path=save_path_arg,
            stream_to_bucket=stream_to_bucket_arg,
            verbose=verbose,
        )
    else:
        pyk.specify_browser('chrome')
        scrape_metadata = pyk.save_tiktok(
            tiktok_url,
            save_video=save_video,
            max_duration_to_save=fyp_cf['misc']['max_duration_for_download'],
            browser_name='chrome',
            save_path=save_path_arg,
            stream_to_bucket=stream_to_bucket_arg,
            verbose=verbose,
        )

    try:
        # if there are columns in the result and a something has been downloaded
        col_count = len(scrape_metadata.columns)
        if col_count > 1 and scrape_metadata.loc[0,'video_downloaded']==True:

            # if this is an image post
            if len(scrape_metadata.loc[0,'image_list'])>0:
                if verbose:
                    print(f"OK   - Photos downloaded - '{video_id}' - {col_count} metadata fields")

                if use_gcs:
                    # GCS path: check bucket, download jpegs to temp, assemble, upload
                    blob = bucket.blob(f"{gcs_media_prefix}/{video_id}.mp4")
                    if blob.exists():
                        if verbose:
                            print("Photo slideshow already in bucket")
                        scrape_metadata.loc[0,'video_downloaded'] = True
                    else:
                        if verbose:
                            print("Converting photos to video slideshow")

                        ccc = 1
                        image_files = []
                        blob = bucket.get_blob(f"{gcs_media_prefix}/{video_id}_{ccc:02}.jpeg")

                        while blob and blob.exists():
                            blob.download_to_filename(os.path.join(temp_dir,f"{video_id}_{ccc:02}.jpeg"))
                            if blob.size >= min_size:
                                image_files.append(os.path.join(temp_dir,f"{video_id}_{ccc:02}.jpeg"))
                            ccc += 1
                            blob = bucket.get_blob(f"{gcs_media_prefix}/{video_id}_{ccc:02}.jpeg")

                        make_slideshow(
                            image_files,
                            output=os.path.join(temp_dir,f"{video_id}.mp4"),
                            duration=2,
                            swipe=False,
                            verbose=verbose
                        )

                        if os.path.getsize(os.path.join(temp_dir,f"{video_id}.mp4")) > min_size:
                            if verbose:
                                print("Uploading video file to storage bucket...")
                            blob = bucket.blob(f"{gcs_media_prefix}/{video_id}.mp4")
                            blob.upload_from_filename(os.path.join(temp_dir,f"{video_id}.mp4"))
                            scrape_metadata.loc[0,'video_downloaded'] = True
                        else:
                            if verbose:
                                print("Generated video file is too small, not uploading.")
                            scrape_metadata.loc[0,'video_downloaded'] = False
                else:
                    # Local path: jpegs are in media_dir (written by _download_images).
                    # Assemble the slideshow to a temp file, validate size, then atomically
                    # move into media_dir and remove source jpegs.
                    final_mp4 = os.path.join(media_dir, f"{video_id}.mp4")
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
                            cand = os.path.join(media_dir, f"{video_id}_{ccc:02}.jpeg")
                            if not os.path.exists(cand):
                                break
                            if os.path.getsize(cand) >= min_size:
                                image_files.append(cand)
                            ccc += 1

                        temp_mp4 = os.path.join(temp_dir, f"{video_id}.mp4")
                        make_slideshow(
                            image_files,
                            output=temp_mp4,
                            duration=2,
                            swipe=False,
                            verbose=verbose
                        )

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
                    if bucket.blob(f"{gcs_media_prefix}/{video_id}.mp4").exists():
                        blob = bucket.get_blob(f"{gcs_media_prefix}/{video_id}.mp4")
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
                    local_mp4 = os.path.join(media_dir, f"{video_id}.mp4")
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










def rescue_tiktok_meta_threads(
    interesting_videos:list[str] = None,
    max_workers:int = 4,
    verbose:bool = False,
    dry_run:bool = False,
    batch_label: str | None = None,
    cumulative_done: int = 0,
    cumulative_total: int = 0):
    


    if dry_run:
        print("********* This is a dry run. It's all fake. No data io action at all. *********")
    else:
        if interesting_videos is None:
            raise ValueError("No interesting videos specified")

        if len(interesting_videos) == 0:
            return pd.DataFrame()

    results_by_index = {}

    def worker(idx_video):
        idx, video = idx_video
        return idx, download_single_video(
            video_id = video, 
            verbose=verbose,
            save_video=False,
            dry_run=dry_run)


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
            if error_type in ('removed', 'private', 'geo_blocked', 'ip_blocked', 'extraction'):
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
        
        scrape_filename = f"{SCRAPES_LABEL}_{fine_ts}.parquet"

        # saving the results to local temp just in case everything goes to pieces
        results.to_parquet(os.path.join(fyp_cf['paths']['temp'], "recovered_"+scrape_filename))
    





        # -----------------------------------------------
        # recode the results before saving
        # -----------------------------------------------

        try:
            # fix up the image_list and video_durations of slide shows

            # first, set item_id as index - to allow for easy access
            results.set_index('item_id', inplace=True)
            results['image_list'] = results['image_list'].map(lambda x:len(x.split("|")) if not pd.isna(x) and x!="" and isinstance(x,str) else 0).astype("int64[pyarrow]")
            # for items with more than zero images in the image_list, set video_duration based on number of images * 2 seconds - just a hunch...
            results.loc[results[results['image_list']>0].index,'video_duration'] = results.loc[results[results['image_list']>0].index,'image_list'] * 2
            # move the item_id back from the index to a column - this also resets the index to a nice range, which is what I want
            results.reset_index(inplace=True)

            # drop do_not_modify column if it exists - it should not exist in 2026+ versions of the code
            results.drop(["do_not_modify"], axis=1, errors='ignore', inplace=True)

            # video duration is never zero - set zero durations to pd.NA
            results.loc[results[results['video_duration']<1].index,'video_duration'] = pd.NA

            # rename the columns
            #results = results.rename(columns={c:"S_"+c if not c=="item_id" and not c.startswith("S_") else c for c in results.columns}).copy()
            results = rename_columns(results).copy()

            # only keep columns as defined by the variable schema
            dropped_vars_str = textwrap.wrap(", ".join(list(set(results.columns) - set(fyp_cf['var_schema'].variable_name))), width=120)
            relevant_cols = [c for c in fyp_cf['var_schema'].variable_name if c in results.columns]
            results = results[relevant_cols].copy()

            if verbose and dropped_vars_str:
                joined_vars = '\n'.join(dropped_vars_str)
                print(f"Dropped these columns, which are not in the variable schema:\n{joined_vars}\nCurrent shape: {results.shape}")
    

            # recode the data
            results = recode_events_df(
                study_dataset = results,
                drop_single_value_cols=False,
                verbose = verbose
                )

            # add scraped_ok column that is True for all rows - necessary for later merging with other datasets
            results["scraped_ok"] = pd.Series(True, index=results.index, dtype="bool[pyarrow]")


            data_io.save_parquet(df=results, storage_location="scrape", filename=scrape_filename)

            print(f"Saved {len(results):,} rows to '{scrape_filename}'. Media downloaded for {len(results[results['video_downloaded']]):,} of these.")

        except Exception as e:
            print(f"CRITICAL: Failed to save results to parquet: {e}")
            print("Recovering the un-processed results from temp")
            data_io.move(
                src_storage_location="temp",
                dst_storage_location="scrape",
                filename="recovered_"+scrape_filename,
                verbose=verbose
                )



    if not dry_run and len(failed_items)>0:
        data_io.save_json(data = failed_items, storage_location="scrape", filename=f"{FAILED_SCRAPES_LABEL}_{fine_ts}.json", verbose=verbose)
        print(f"Saved {len(failed_items)} failed items")

    return results, permanent_failed_ids, transient_failed_ids






def check_existing_media(video_ids: list[str], max_workers: int = 16) -> set[str]:
    """Return the subset of video_ids whose media file is already stored.

    A video_id qualifies as "already downloaded" when a file named
    ``{video_id}.mp4`` exists at the configured media location and its size
    meets ``fyp_cf['misc']['min_media_object_size']``. Under-sized files are
    treated as invalid (consistent with post-download validation in
    ``download_single_video``) and are not included in the returned set.

    Routes between local filesystem and GCS via the same config as
    ``download_single_video`` (``fyp_cf['data_io']['use_gcs_for_media']``).
    GCS probes run on a bounded thread pool for throughput.

    Any exception on a single probe is treated as "unknown — include in the
    normal scrape path" (fail-safe: never falsely skip a media download).

    Args:
        video_ids: Video IDs to check.
        max_workers: Parallelism for GCS probes (ignored in local mode).

    Returns:
        Set of video_ids with valid existing media.
    """
    if not video_ids:
        return set()

    use_gcs = fyp_cf['data_io']['use_gcs_for_media']
    min_size = fyp_cf['misc']['min_media_object_size']

    if use_gcs:
        bucket = fyp_cf['data_io']['bucket']
        gcs_media_prefix = fyp_cf['data_io']['gcs_media_prefix']
        if bucket is None:
            return set()

        def _probe(vid: str) -> str | None:
            try:
                blob = bucket.get_blob(f"{gcs_media_prefix}/{vid}.mp4")
                if blob is not None and blob.size is not None and blob.size >= min_size:
                    return vid
            except Exception:
                return None
            return None

        present: set[str] = set()
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for result in ex.map(_probe, video_ids):
                if result is not None:
                    present.add(result)
        return present

    media_dir = fyp_cf['paths']['media']
    present = set()
    for vid in video_ids:
        try:
            path = os.path.join(media_dir, f"{vid}.mp4")
            if os.path.exists(path) and os.path.getsize(path) >= min_size:
                present.add(vid)
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
    on_concurrency_change: "callable | None" = None,
    on_video_done: "callable | None" = None):



    if dry_run:
        print("********* This is a dry run. It's all fake. No data io action at all. *********")
    else:
        if interesting_videos is None:
            raise ValueError("No interesting videos specified")

        if len(interesting_videos) == 0:
            return pd.DataFrame()

    already_have_media: set[str] = set()
    if not dry_run and interesting_videos:
        already_have_media = check_existing_media(interesting_videos)
        if already_have_media:
            print(
                f"  {len(already_have_media)}/{len(interesting_videos)} items "
                f"already have media — will do metadata-only scrape for those"
            )

    results_by_index = {}
    # Lower ceiling than before (was 12). With a single TikTok session
    # behind cookies, >6 concurrent requests reliably triggers TikTok's
    # behavioural flags — keep headroom but don't let the throttle grow
    # back into the danger zone after a recovery period.
    throttle = tiktok_dl.ThrottleController(
        initial=max_workers, minimum=2, maximum=max(max_workers, 6),
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
                dry_run=dry_run)
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
            cumulative_total=cumulative_total
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
            if error_type in ('removed', 'private', 'geo_blocked', 'ip_blocked', 'extraction'):
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
        
        scrape_filename = f"{SCRAPES_LABEL}_{fine_ts}.parquet"

        # saving the results to local temp just in case everything goes to pieces
        results.to_parquet(os.path.join(fyp_cf['paths']['temp'], "recovered_"+scrape_filename))


        # -----------------------------------------------
        # recode the results before saving
        # -----------------------------------------------

        try:
            # fix up the image_list and video_durations of slide shows

            # first, set item_id as index - to allow for easy access
            results.set_index('item_id', inplace=True)
            results['image_list'] = results['image_list'].map(lambda x:len(x.split("|")) if not pd.isna(x) and x!="" else 0).astype("int64[pyarrow]")
            # for items with more than zero images in the image_list, set video_duration based on number of images * 2 seconds - just a hunch...
            results.loc[results[results['image_list']>0].index,'video_duration'] = results.loc[results[results['image_list']>0].index,'image_list'] * 2
            # move the item_id back from the index to a column - this also resets the index to a nice range, which is what I want
            results.reset_index(inplace=True)

            # drop do_not_modify column if it exists - it should not exist in 2026+ versions of the code
            results.drop(["do_not_modify"], axis=1, errors='ignore', inplace=True)

            # video duration is never zero - set zero durations to pd.NA
            results.loc[results[results['video_duration']<1].index,'video_duration'] = pd.NA

            # rename the columns
            #results = results.rename(columns={c:"S_"+c if not c=="item_id" and not c.startswith("S_") else c for c in results.columns}).copy()
            results = rename_columns(results).copy()

            # only keep columns as defined by the variable schema
            dropped_vars_str = textwrap.wrap(", ".join(list(set(results.columns) - set(fyp_cf['var_schema'].variable_name))), width=120)
            relevant_cols = [c for c in fyp_cf['var_schema'].variable_name if c in results.columns]
            results = results[relevant_cols].copy()

            if verbose and dropped_vars_str:
                joined_vars = '\n'.join(dropped_vars_str)
                print(f"Dropped these columns, which are not in the variable schema:\n{joined_vars}\nCurrent shape: {results.shape}")
    

            # recode the data
            results = recode_events_df(
                study_dataset = results,
                drop_single_value_cols=False,
                verbose = verbose
                )

            # add scraped_ok column that is True for all rows - necessary for later merging with other datasets
            results["scraped_ok"] = pd.Series(True, index=results.index, dtype="bool[pyarrow]")


            data_io.save_parquet(df=results, storage_location="scrape", filename=scrape_filename)

            print(f"Saved {len(results):,} rows to '{scrape_filename}'. Media downloaded for {len(results[results['video_downloaded']]):,} of these.")

        except Exception as e:
            print(f"CRITICAL: Failed to save results to parquet: {e}")
            print("Recovering the un-processed results from temp")
            data_io.move(
                src_storage_location="temp",
                dst_storage_location="scrape",
                filename="recovered_"+scrape_filename,
                verbose=verbose
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
    ):



    max_batches = max_batches if max_batches is not None else np.inf



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

        ok_in_batch = 0
        fail_in_batch = 0
        done_in_batch = 0
        current_batch_size = len(batch)

        def _on_video_done(idx, ok, error_cat):
            nonlocal done_in_batch, ok_in_batch, fail_in_batch
            done_in_batch += 1
            if ok:
                ok_in_batch += 1
            else:
                fail_in_batch += 1
            if reporter is not None:
                completed = cumulative_done + done_in_batch
                pct = int(completed / total_items * 100) if total_items else 0
                pending = current_batch_size - done_in_batch
                reporter.update_progress(
                    pct,
                    f"Batch {batch_label}: {ok_in_batch} OK, {fail_in_batch} fail, {pending} pending",
                )

        results_from_scraper, perm_failed, trans_failed = download_video_threads(
            interesting_videos = batch,
            max_workers=4,
            verbose = verbose,
            dry_run = dry_run,
            batch_label=batch_label,
            cumulative_done=cumulative_done,
            cumulative_total=total_items,
            on_concurrency_change=_on_threads_change,
            on_video_done=_on_video_done)

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
        elif _check_graceful_stop("queue_scraper"):
            print("  Graceful stop requested. Finishing after this batch.")
            break

        batch_number += 1

        if dry_run:
            break

    # ----------------
    # Update scrape queue: remove successful + permanently failed items.
    # Transient failures stay in the queue for retry on next run.
    # -----------------
    target_queue_file = 'to_scrape.json'

    if data_io.exists(storage_location='cache', filename=target_queue_file, verbose=verbose):
        to_scrape_queue = data_io.load_json(storage_location='cache', filename=target_queue_file, verbose=verbose)

        if isinstance(to_scrape_queue, list):
            # Only remove items that succeeded or permanently failed
            items_to_remove = set(good_scrapes + all_permanent_failed)

            original_len = len(to_scrape_queue)
            updated_queue = [item for item in to_scrape_queue if item not in items_to_remove]

            if len(updated_queue) < original_len:
                data_io.save_json(data=updated_queue, storage_location='cache', filename=target_queue_file, verbose=verbose)
                print(f"  Queue update: removed {original_len - len(updated_queue)} "
                      f"({len(good_scrapes)} OK, {len(all_permanent_failed)} permanent fail). "
                      f"{len(all_transient_failed)} transient failures remain for retry. "
                      f"Queue length: {len(updated_queue)}")


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
    ):


    # Load queue
    target_queue_file = 'to_scrape.json'

    video_list = []
    if data_io.exists(storage_location='cache', filename=target_queue_file):
            video_list = data_io.load_json(storage_location='cache', filename=target_queue_file)

    if not video_list or not isinstance(video_list, list) or len(video_list) == 0:
        print("Queue is empty or invalid. Nothing to scrape.")
        return

    print(f"Found {len(video_list)} items in queue.")

    scraper_loop_from_list(
        video_list=video_list,
        batch_size=batch_size,
        max_batches=max_batches,
        verbose=verbose,
        dry_run=dry_run,
        reporter=reporter,
        cancellation_check=cancellation_check,
    )










def _engagement_per_play(numerator: pd.Series, plays: pd.Series) -> pd.Series:
    """Divide an engagement count by play count, returning a proportion.

    The yt-dlp sentinel ``-1`` (and any value < 0) in the numerator, and any
    play count <= 0, are treated as missing and yield ``pd.NA``.

    Args:
        numerator: Engagement counts (e.g. comments, faves, shares, saves).
        plays: Play (view) counts used as the denominator.

    Returns:
        A ``double[pyarrow]`` Series of ``numerator / plays`` proportions with
        ``pd.NA`` wherever either side is missing or the denominator is non-positive.
    """
    num = numerator.astype("double[pyarrow]").mask(numerator < 0, pd.NA)
    den = plays.astype("double[pyarrow]").mask(plays <= 0, pd.NA)
    return (num / den).astype("double[pyarrow]")






# Engagement-rate columns derived at consolidation: new per-play column → source count.
ENGAGEMENT_PER_PLAY_COLUMNS: dict[str, str] = {
    "comments_per_play": "stats_commentCount",
    "faves_per_play": "stats_diggCount",
    "shares_per_play": "stats_shareCount",
    "saves_per_play": "stats_collectCount",
}


# TikTok reports view/engagement counts as signed 32-bit integers; counts above
# 2**31 - 1 (~2.15 billion) arrive wrapped around to a negative value. The true
# count is recovered by adding 2**32. The yt-dlp "missing" sentinel -1 is left
# untouched (a genuine 4,294,967,295-view item would also wrap to -1, but that is
# vanishingly rare and indistinguishable from the sentinel).
_UINT32_RANGE: int = 1 << 32

OVERFLOW_REPAIR_COLUMNS: tuple[str, ...] = (
    "stats_playCount",
    "stats_diggCount",
    "stats_shareCount",
    "stats_commentCount",
    "stats_collectCount",
)


def repair_overflowed_counts(df: pd.DataFrame, columns: tuple[str, ...] = OVERFLOW_REPAIR_COLUMNS, verbose: bool = False) -> pd.DataFrame:
    """Recover signed-32-bit-overflowed TikTok counts in place.

    Any value strictly below -1 in a count column is treated as a 32-bit wrap of
    a count exceeding 2**31 and is corrected by adding 2**32. The -1 missing
    sentinel and all non-negative values are preserved.

    Args:
        df: DataFrame of scrape stats (mutated and returned).
        columns: Count column names to repair.
        verbose: When True, print the number of values repaired per column.

    Returns:
        The same DataFrame with overflowed counts recovered.
    """
    for col in columns:
        if col not in df.columns:
            continue
        series = df[col]
        mask = (series < -1).fillna(False)
        n_repaired = int(mask.sum())
        if n_repaired:
            df[col] = series.mask(mask, series + _UINT32_RANGE)
            if verbose:
                print(f"    Recovered {n_repaired:,} signed-32-bit-overflowed value(s) in {col}")
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
    for fn in files_to_concatenate:
        df = data_io.load_parquet(storage_location="scrape", filename=fn)
        many_scrape_dfs.append(df)
        if verbose:
            print(fn, df.shape)

    if top_verbose:
        print(f"Consolidating {len(many_scrape_dfs):,} scrape files (dropping duplicate items)...")
    scrape_df = pd.concat(many_scrape_dfs, ignore_index=True)


    # -------------------------------------------------
    # There may be some items listed twice - once as video_downloaded and once as not
    # This code addresses that issue
    # -------------------------------------------------

    # deduplicate based on item_id but if there are both a true and a false video_downloaded status, keep both
    scrape_df = scrape_df.drop_duplicates(subset=["item_id","video_downloaded"]).copy()
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


    # Recover any signed-32-bit-overflowed counts (TikTok view counts above
    # ~2.15B arrive wrapped to a negative value) before they are stored or used
    # to derive per-play rates / plays-per-day downstream.
    scrape_df = repair_overflowed_counts(scrape_df, verbose=top_verbose)

    # Derive per-play engagement rates so comparisons are not dominated by the
    # mechanical correlation between absolute counts and play count. Plays stays
    # absolute; the raw counts are retained for provenance.
    has_plays = "stats_playCount" in scrape_df.columns
    for new_col, src_col in ENGAGEMENT_PER_PLAY_COLUMNS.items():
        if has_plays and src_col in scrape_df.columns:
            scrape_df[new_col] = _engagement_per_play(scrape_df[src_col], scrape_df["stats_playCount"])
        else:
            scrape_df[new_col] = pd.Series(pd.NA, index=scrape_df.index, dtype="double[pyarrow]")


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




