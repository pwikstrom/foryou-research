#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""



from typing import List, Tuple, Union, Sequence
from PIL import Image, ImageColor
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import time

import fyp.data_io as data_io
from fyp.utils import chunk_list, start_monitor
import fyp.mypyktok as pyk
from fyp.recode_variables import rename_columns, recode_events_df
from fyp.fyp_config import fyp_cf

import os

import numpy as np
import pandas as pd
import threading
import time
import sys
import shutil
import json
import textwrap
from pathlib import Path



SCRAPES_LABEL = fyp_cf["labels"]["SCRAPES_LABEL"]
FAILED_SCRAPES_LABEL = fyp_cf["labels"]["FAILED_SCRAPES_LABEL"]





def _check_graceful_stop(process_name: str) -> bool:
    """Check if a graceful stop has been requested via sentinel file."""
    sentinel = Path(fyp_cf['paths']['project_root']) / "tmp" / "graceful_stop" / f"{process_name}.stop"
    return sentinel.exists()



def make_slideshow(
    files: List[str],
    output: str = "slideshow.mp4",
    duration: float = 3.0,
    transition: float = 0.6,
    swipe: bool = True,
    canvas_size: Tuple[int, int] = None,  # auto if None
    bg_color: Union[str, Tuple[int, int, int]] = "#000000",
    fps: int = 1,
    codec: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    verbose=False
):
    # Creates a slideshow video from a list of image files.

    from moviepy import (
        ImageClip,
        ColorClip,
        CompositeVideoClip,
        concatenate_videoclips
    )
    

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
        canvas_size: Tuple[int, int],
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



    def _infer_canvas_size(files: List[str]) -> Tuple[int, int]:
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


    if fyp_cf['data_io']['bucket'] is None:
        raise ValueError("No GCS bucket specified")
    if video_id is None:
        raise ValueError("No video id specified")



    gcs_media_prefix = fyp_cf['data_io']['gcs_media_prefix']

    pyk.specify_browser('chrome')

    tiktok_url = f"https://www.tiktok.com/@/video/{video_id}/"

    # try to scrape metadata and download video
    scrape_metadata = pyk.save_tiktok(
        tiktok_url,
        save_video=save_video,
        max_duration_to_save = fyp_cf['misc']['max_duration_for_download'],
        browser_name='chrome',
        save_path=gcs_media_prefix,
        stream_to_bucket = fyp_cf["data_io"]["bucket"],
        verbose=verbose
    )

    try:
        # if there are columns in the result and a something has been downloaded
        col_count = len(scrape_metadata.columns)
        if col_count > 1 and scrape_metadata.loc[0,'video_downloaded']==True:

            # if this is an image post
            if len(scrape_metadata.loc[0,'image_list'])>0:
                if verbose:
                    print(f"OK   - Photos downloaded - '{video_id}' - {col_count} metadata fields")

                # if there isn't a video already associated to this post...
                blob = fyp_cf["data_io"]["bucket"].blob(f"{gcs_media_prefix}/{video_id}.mp4")
                if blob.exists():
                    if verbose:
                        print(f"Photo slideshow already in bucket")
                    scrape_metadata.loc[0,'video_downloaded'] = True
                else:
                    if verbose:
                        print(f"Converting photos to video slideshow")

                    # look for image files and download those that are found
                    ccc = 1
                    image_files = []
                    blob = fyp_cf["data_io"]["bucket"].get_blob(f"{gcs_media_prefix}/{video_id}_{ccc:02}.jpeg")

                    while blob and blob.exists():
                        blob.download_to_filename(os.path.join(fyp_cf["paths"]["temp"],f"{video_id}_{ccc:02}.jpeg"))
                        if blob.size >= fyp_cf["misc"]["min_media_object_size"]:
                            image_files.append(os.path.join(fyp_cf["paths"]["temp"],f"{video_id}_{ccc:02}.jpeg"))
                        ccc += 1
                        blob = fyp_cf["data_io"]["bucket"].get_blob(f"{gcs_media_prefix}/{video_id}_{ccc:02}.jpeg")

                    # use the images to build a slideshow
                    make_slideshow(
                        image_files,
                        output=os.path.join(fyp_cf["paths"]["temp"],f"{video_id}.mp4"),
                        duration=2,
                        swipe=False,
                        verbose=verbose
                    )

                    # upload the video slideshow to the storage bucket if it is large enough
                    if os.path.getsize(os.path.join(fyp_cf["paths"]["temp"],f"{video_id}.mp4")) > fyp_cf["misc"]["min_media_object_size"]:
                        if verbose:
                            print(f"Uploading video file to storage bucket...")
                        blob = fyp_cf["data_io"]["bucket"].blob(f"{gcs_media_prefix}/{video_id}.mp4")
                        blob.upload_from_filename(os.path.join(fyp_cf["paths"]["temp"],f"{video_id}.mp4"))
                        scrape_metadata.loc[0,'video_downloaded'] = True
                    else:
                        if verbose:
                            print(f"Generated video file is too small, not uploading.")
                        scrape_metadata.loc[0,'video_downloaded'] = False

            # if this is a video...
            else:
                if verbose:
                    print(f"OK   - Video downloaded '{video_id}' - {col_count} metadata fields")

                # check if it truly is stored and is big enough
                if verbose:
                    print(f"Checking video file in bucket")
                if fyp_cf["data_io"]["bucket"].blob(f"{gcs_media_prefix}/{video_id}.mp4").exists():
                    blob = fyp_cf["data_io"]["bucket"].get_blob(f"{gcs_media_prefix}/{video_id}.mp4")
                    if blob.size < fyp_cf["misc"]["min_media_object_size"]:
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
    
    # things have gone terribly wrong if we end up down here
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
    for idx in range(len(interesting_videos)):
        if idx in results_by_index.keys() and type(results_by_index[idx])==pd.DataFrame and results_by_index[idx].shape[1]>10: # download good
            results += [results_by_index[idx]]
        else:
            failed_items += [results_by_index[idx]]

    if len(results)==0:
        print("The scrape procedure did not generate any useful results")
        return pd.DataFrame()

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

    return results






def download_video_threads(
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
    for idx in range(len(interesting_videos)):
        if idx in results_by_index.keys() and type(results_by_index[idx])==pd.DataFrame and results_by_index[idx].shape[1]>10: # download good
            results += [results_by_index[idx]]
        else:
            failed_items += [results_by_index[idx]]

    if len(results)==0:
        print("The scrape procedure did not generate any useful results")
        return pd.DataFrame()

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

    return results











def scraper_loop_from_list(
    video_list = [],
    study_name = None,
    batch_size = 500,
    max_batches = None,
    verbose = False,
    dry_run = False
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
    failed_scrapes = []

    for batch in chunk_list(video_list, batch_size):

        batch_label = f"{batch_number}/{batch_target}"
        print(f"  Batch {batch_label}")

        results_from_scraper = download_video_threads(
            interesting_videos = batch,
            max_workers=4,
            verbose = verbose,
            dry_run = dry_run,
            batch_label=batch_label,
            cumulative_done=cumulative_done,
            cumulative_total=total_items)

        if not results_from_scraper.empty and "item_id" in results_from_scraper.columns:
            good_scrapes += results_from_scraper["item_id"].to_list()

        failed_scrapes += [v for v in batch if v not in good_scrapes]
        with open(os.path.join(fyp_cf['paths']['temp'], "temp_failed_scrapes.json"), "w") as f:
            json.dump(failed_scrapes, f)
        with open(os.path.join(fyp_cf['paths']['temp'], "temp_good_scrapes.json"), "w") as f:
            json.dump(good_scrapes, f)

        cumulative_done += len(batch)

        # Emit queue update after each batch (for web UI)
        if "WEB_INTERFACE" in os.environ:
            queue_remaining = len(video_list) - cumulative_done
            print(f"::DATA::{{\"scrape_queue_len\": {max(0, queue_remaining)}}}", flush=True)

        if max_batches is not None and batch_number >= max_batches:
            break

        # Check for graceful stop request
        if _check_graceful_stop("queue_scraper"):
            print("  Graceful stop requested. Finishing after this batch.")
            break

        batch_number += 1

        if dry_run:
            break

    # ----------------
    # Update scrape queue file, by removing the items that have been scraped - both good and failed
    # -----------------
    target_queue_file = 'to_scrape.json'

    if data_io.exists(storage_location='cache', filename=target_queue_file, verbose=verbose):
        # Load the existing queue
        to_scrape_queue = data_io.load_json(storage_location='cache', filename=target_queue_file, verbose=verbose)
        
        if isinstance(to_scrape_queue, list):
            # Identify items to remove (both good and failed are considered "processed" in this context)
            processed_items = set(good_scrapes + failed_scrapes)
            
            # Filter the queue
            original_len = len(to_scrape_queue)
            updated_queue = [item for item in to_scrape_queue if item not in processed_items]
            
            # Save if changed
            if len(updated_queue) < original_len:
                data_io.save_json(data=updated_queue, storage_location='cache', filename=target_queue_file, verbose=verbose)
                if verbose:
                    print(f"    Updated scrape queue: Removed {original_len - len(updated_queue)} items. New length: {len(updated_queue)}")


    print(f"  Loop ended: {datetime.now()}")
    return good_scrapes, failed_scrapes










def scraper_loop(
    study_name = None,
    study_dataset = None,
    load_from_cache = True,
    batch_size = 500,
    max_batches = None,
    verbose = False,
    dry_run = False
    ):



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
    dry_run = False
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
        dry_run=dry_run
    )










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
            if verbose: print("Returning existing file.")
            return False, data_io.load_parquet(storage_location="recoded", filename=f"{SCRAPES_LABEL}_recoded.parquet"), set()
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
        print(f"    Identifying conflicting items in the dataset listed twice - once as video_downloaded and once as not")
        print(
            f"    There are {len(items_w_inconsistent_video_download_status):,} items with such inconsistencies, "
            f"and {len(items_w_consistent_video_download_status):,} that look alright.")

    if len(items_w_inconsistent_video_download_status)>0:
        # for items with inconsistent video download status, only keep the ones where video_downloaded is True
        items_w_inconsistent_video_download_status = items_w_inconsistent_video_download_status[items_w_inconsistent_video_download_status['video_downloaded']].copy()
        if verbose:
            print(f"    Fixed the inconsistencies by keeping the one of the pairs with video_download=True")
            print(f"    This reduces the number of inconsistent items to {len(items_w_inconsistent_video_download_status)}")

        # recombine the two dataframes
        scrape_df = pd.concat([items_w_consistent_video_download_status,items_w_inconsistent_video_download_status])


    memory_per_column = scrape_df.memory_usage(deep=True) 
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    if top_verbose:
        print(f"Shape: {scrape_df.shape} | Memory usage: {total_memory_mb:.2f} MB")


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
    if not SCRAPES_LABEL in dataset_meta:
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




