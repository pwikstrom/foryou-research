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
from os.path import join as local_join, getsize as local_getsize
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import time

import fyp.data_io as data_io
from fyp.organize_datasets import select_videos_from_study_dataset, create_study_recoded_dataset
from fyp.fyp_main import initialize, connect_to_google, chunk_list, convert_dtypes_to_pyarrow
import fyp.mypyktok as pyk
from fyp.recode_variables import rename_columns, recode_events_df

from os import environ

import numpy as np
import pandas as pd
import threading
import time
import sys
import shutil
import json
import textwrap





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
    cf = None,
    video_id: str = None, 
    verbose: bool = True,
    dry_run: bool = False,
    ):


    if dry_run:
        from time import sleep
        sleep(1)
        if verbose:
            print(f"Dry run: would have downloaded video {video_id}")
        return video_id

    if cf is None:
        cf = initialize()
    
    if cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

    if cf['data_io']['bucket'] is None:
        raise ValueError("No GCS bucket specified")
    if video_id is None:
        raise ValueError("No video id specified")



    gcs_media_prefix = cf['data_io']['gcs_media_prefix']

    pyk.specify_browser('chrome')

    #tiktok_url = f"https://www.tiktokv.com/share/video/{video_id}/"
    tiktok_url = f"https://www.tiktok.com/@/video/{video_id}/"

    # try to scrape metadata and download video
    scrape_metadata = pyk.save_tiktok(
        tiktok_url,
        save_video=True,
        max_duration_to_save = cf['misc']['max_duration_for_download'],
        browser_name='chrome',
        save_path=gcs_media_prefix,
        stream_to_bucket = cf["data_io"]["bucket"],
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
                blob = cf["data_io"]["bucket"].blob(f"{gcs_media_prefix}/{video_id}.mp4")
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
                    blob = cf["data_io"]["bucket"].get_blob(f"{gcs_media_prefix}/{video_id}_{ccc:02}.jpeg")

                    while blob and blob.exists():
                        blob.download_to_filename(local_join(cf["paths"]["temp"],f"{video_id}_{ccc:02}.jpeg"))
                        if blob.size >= cf["misc"]["min_media_object_size"]:
                            image_files.append(local_join(cf["paths"]["temp"],f"{video_id}_{ccc:02}.jpeg"))
                        ccc += 1
                        blob = cf["data_io"]["bucket"].get_blob(f"{gcs_media_prefix}/{video_id}_{ccc:02}.jpeg")

                    # use the images to build a slideshow
                    make_slideshow(
                        image_files,
                        output=local_join(cf["paths"]["temp"],f"{video_id}.mp4"),
                        duration=2,
                        swipe=False,
                        verbose=verbose
                    )

                    # upload the video slideshow to the storage bucket if it is large enough
                    if local_getsize(local_join(cf["paths"]["temp"],f"{video_id}.mp4")) > cf["misc"]["min_media_object_size"]:
                        if verbose:
                            print(f"Uploading video file to storage bucket...")
                        blob = cf["data_io"]["bucket"].blob(f"{gcs_media_prefix}/{video_id}.mp4")
                        blob.upload_from_filename(local_join(cf["paths"]["temp"],f"{video_id}.mp4"))
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
                if cf["data_io"]["bucket"].blob(f"{gcs_media_prefix}/{video_id}.mp4").exists():
                    blob = cf["data_io"]["bucket"].get_blob(f"{gcs_media_prefix}/{video_id}.mp4")
                    if blob.size < cf["misc"]["min_media_object_size"]:
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








def start_monitor(futures, submit_times, interval=5, label="monitor", bar_width=30):
    """
    futures: list[Future]
    submit_times: dict[Future -> float]  time.time() at submission
    """



    def _fmt_secs(s):
        if s is None:
            return "n/a"
        s = int(s)
        h, r = divmod(s, 3600)
        m, s = divmod(r, 60)
        if h: return f"{h}h{m}m{s}s"
        if m: return f"{m}m{s}s"
        return f"{s}s"

    def _bar(done, total, width=30, fill="#", empty="-"):
        if total <= 0:
            return "[" + empty * width + "] 0%"
        frac = max(0.0, min(1.0, done / total))
        n_fill = int(round(frac * width))
        n_empty = max(0, width - n_fill)
        pct = int(round(frac * 100))
        return f"[{fill * n_fill}{empty * n_empty}] {pct:3d}%"

    def _run():
        start = min(submit_times.values()) if submit_times else time.time()
        seen_done = set()
        durations = []

        total = len(futures)
        while True:
            now = time.time()
            done_futs = [f for f in futures if f.done()]
            
            good_scrapes = []
            for fut in done_futs:
                _, res = fut.result()
                good_scrapes += [1 if type(res)==pd.DataFrame else 0]
            n_good_scrapes = sum(good_scrapes)
            
            running = sum(f.running() for f in futures)
            done = len(done_futs)
            pending = total - done - running
            failed = sum(1 for f in done_futs if f.exception() is not None)

            # record turnaround times (submission to completion)
            for f in done_futs:
                if f not in seen_done:
                    seen_done.add(f)
                    durations.append(now - submit_times.get(f, start))

            elapsed = now - start
            avg_turnaround = (sum(durations) / len(durations)) if durations else None
            throughput = (done / elapsed) if elapsed > 0 else 0.0
            success_rate = (n_good_scrapes / done) if done > 0 else 0
            remaining = total - done
            eta = (remaining / throughput) if throughput > 0 else None

            bar = _bar(done, total, width=bar_width)

            line = (
                f"[{label}] {bar}  "
                f"done {done:,}/{total:,}  success {success_rate:.0%}  pending {pending:,}  "#running {running}  failed {failed}  "
                #f"elapsed {_fmt_secs(elapsed)}  avg {_fmt_secs(avg_turnaround)}  "
                f"scrapeRate {throughput:.2f}/s  ETA {_fmt_secs(eta)}     "
            )

            # trim to terminal width if needed
            try:
                term_width = shutil.get_terminal_size(fallback=(140, 20)).columns
            except Exception:
                term_width = 140
            if len(line) > term_width:
                line = line[:max(0, term_width - 1)]

            # single-line update
            if "WEB_INTERFACE" in environ:
                 progress_data = {
                     "done": done,
                     "total": total,
                     "rate": throughput,
                     "eta": eta if eta is not None else 0
                 }
                 print(f"::PROGRESS::{json.dumps(progress_data)}", flush=True)
            else:
                 sys.stdout.write("\r" + line)
                 sys.stdout.flush()

            if done == total:
                break
            time.sleep(interval)

        # finish with a newline so the next print does not overwrite the last status
        sys.stdout.write("\n")
        sys.stdout.flush()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t








def download_video_threads(
    cf:dict = None,
    interesting_videos:list[str] = None,
    max_workers:int = 4,
    verbose:bool = False,
    dry_run:bool = False):
    

    if cf is None:
        cf = initialize()

    if dry_run:
        print("********* This is a dry run. It's all fake. No data io action at all. *********")
    else:
        if cf['data_io']['bucket'] is None:
            cf = connect_to_google(cf)

        if cf['data_io']['bucket'] is None:
            raise ValueError("No GCS bucket specified")
        if interesting_videos is None:
            raise ValueError("No interesting videos specified")

        if len(interesting_videos) == 0:
            return pd.DataFrame()

    results_by_index = {}

    def worker(idx_video):
        idx, video = idx_video
        return idx, download_single_video(
            cf = cf,
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


        monitor_thread = start_monitor(futures, submit_times, interval=5, label="dl", bar_width=32)


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
        
        scrape_filename = f"scrape_{fine_ts}.parquet"

        # saving the results to local temp just in case everything goes to pieces
        results.to_parquet(local_join(cf['paths']['temp'], "recovered_"+scrape_filename))


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
            results = results.rename(columns={c:"S_"+c if not c=="item_id" and not c.startswith("S_") else c for c in results.columns}).copy()
            results = rename_columns(results).copy()

            # only keep columns as defined by the variable schema
            dropped_vars_str = textwrap.wrap(", ".join(list(set(results.columns) - set(cf['var_schema'].variable_name))), width=120)
            relevant_cols = [c for c in cf['var_schema'].variable_name if c in results.columns]
            results = results[relevant_cols].copy()

            if verbose:
                print(f"Dropped these columns, which are not in the variable schema:\n{"\n".join(dropped_vars_str)}\nCurrent shape: {results.shape}")
    

            # recode the data
            results = recode_events_df(
                cf = cf,
                study_dataset = results,
                drop_single_value_cols=False,
                load_from_cache = False,
                save_to_cache = False,
                verbose = verbose
                )

            # add scraped_ok column that is True for all rows - necessary for later merging with other datasets
            results["scraped_ok"] = pd.Series(True, index=results.index, dtype="bool[pyarrow]")


            data_io.save_parquet(cf=cf, df=results, storage_location="scrape", filename=scrape_filename)

            print(f"Saved {len(results):,} rows to '{scrape_filename}'. Media downloaded for {len(results[results['S_video_downloaded']]):,} of these.")

        except Exception as e:
            print(f"CRITICAL: Failed to save results to parquet: {e}")
            print("Recovering the un-processed results from temp")
            data_io.move(
                cf=cf,
                src_storage_location="temp",
                dst_storage_location="scrape",
                filename="recovered_"+scrape_filename,
                verbose=verbose
                )


    if not dry_run and len(failed_items)>0:
        data_io.save_json(cf, failed_items, "scrape", f"scrape_failed_items_{fine_ts}.json", verbose=verbose)
        print(f"Saved {len(failed_items)} failed items")

    return results








def download_videos_loop(
    cf = None,
    study_name = None,
    study_dataset = None,
    load_from_cache = True,
    batch_size = 500,
    max_batches = None,
    verbose = False,
    dry_run = False
    ):



    max_batches = max_batches if max_batches is not None else np.inf

    if study_name is None and study_dataset is None:
        print("    ERROR: This process cannot run without a study name or a study dataset as input. Process failed.")
        return None

    if cf is None:
        cf = initialize()

    if load_from_cache and study_name is not None:
        if data_io.exists(
            cf=cf,
            storage_location="cache",
            filename=f"{study_name}_recoded.parquet",
            verbose=verbose
            ):
            if verbose:
                print("    Loading study dataset from cache", end=" ", flush=True)
            study_dataset = data_io.load_parquet(
                cf=cf,
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
                cf = cf,
                study_name = study_name,
                load_from_cache = True,
                save_to_cache = True,
                verbose = verbose
            )


    if study_dataset is None:
        print("    ERROR: This process cannot run without a study dataset. Process failed.")
        return None

    print(f"    Downloading media objects and metadata for unseen videos, study '{study_name}', batch size: {batch_size}, max batches: {max_batches}")
    print(f"    Now: {datetime.now()}")
    #print("--"*60)

    selected_videos_df = select_videos_from_study_dataset(
        cf = cf,
        study_dataset = study_dataset,
        query_string = "~scraped_ok & ~scraped_fail",
        verbose = verbose,
        notebook_mode = False
    )

    batch_number = 1

    batch_target = min(max_batches, len(selected_videos_df.index) // batch_size + 1)

    print(f"  Starting loop... There are {len(selected_videos_df):,} videos to process in {batch_target:,} batches")

    for batch in chunk_list(selected_videos_df.index.to_list(), batch_size):
        
        print(f"  Batch {batch_number} of {max_batches:,}")

        _ = download_video_threads(
            cf = cf,
            interesting_videos = batch, 
            max_workers=4, 
            verbose = verbose,
            dry_run = dry_run)
        

        if max_batches is not None and batch_number >= max_batches:
            break

        batch_number += 1

        if dry_run:
            break


    print(f"  Loop ended: {datetime.now()}")










def load_scrape_data(
    cf = None,
    filters=None,
    verbose=False):


    if cf is None:
        cf = initialize()

    print("Loading scraped data...")

    scrape_data = data_io.load_parquet(cf, "recoded", "scrape_recoded.parquet", filters=filters, verbose=verbose)

    return scrape_data









def consolidate_scrape_data(cf: dict = None, verbose: bool = False):
    from fyp.fyp_main import initialize
    import fyp.data_io as data_io
    import fyp
    import pandas as pd
    from fyp.fyp_main import convert_dtypes_to_pyarrow

    cf = initialize()

    many_scrape_dfs = []
    for fn in data_io.listdir(cf=cf, storage_location="scrape"):
        if fn.startswith("scrape_") and fn.endswith(".parquet"):
            df = data_io.load_parquet(cf=cf, storage_location="scrape", filename=fn)
            many_scrape_dfs.append(df)
            if verbose:
                print(fn, df.shape)

    scrape_df = pd.concat(many_scrape_dfs, ignore_index=True)



    # -------------------------------------------------
    # There may be some items listed twice - once as video_downloaded and once as not
    # This code addresses that issue
    # -------------------------------------------------

    # deduplicate based on item_id but if there are both a true and a false video_downloaded status, keep both
    scrape_df = scrape_df.drop_duplicates(subset=["item_id","S_video_downloaded"]).copy()
    if verbose:
        print(f"Dropping duplicates based on items and whether the video is downloaded or not: {scrape_df.shape}")

    # identify items with inconsistent video_downloaded status
    items_w_inconsistent_video_download_status = scrape_df["item_id"].value_counts()
    items_w_inconsistent_video_download_status = items_w_inconsistent_video_download_status[items_w_inconsistent_video_download_status>1].index.tolist()

    # use the list generated above to separate items with consistent vs inconsistent video download status
    items_w_consistent_video_download_status = scrape_df[~scrape_df['item_id'].isin(items_w_inconsistent_video_download_status)].copy()
    items_w_inconsistent_video_download_status = scrape_df[scrape_df['item_id'].isin(items_w_inconsistent_video_download_status)].copy()
    if verbose:
        print(f"Identifying conflicting items in the dataset listed twice - once as video_downloaded and once as not")
        print(
            f"There are {len(items_w_inconsistent_video_download_status):,} items with such inconsistencies, "
            f"and {len(items_w_consistent_video_download_status):,} that look alright.")

    if len(items_w_inconsistent_video_download_status)>0:
        # for items with inconsistent video download status, only keep the ones where video_downloaded is True
        items_w_inconsistent_video_download_status = items_w_inconsistent_video_download_status[items_w_inconsistent_video_download_status['S_video_downloaded']].copy()
        if verbose:
            print(f"Fixed the inconsistencies by keeping the one of the pairs with video_download=True")
            print(f"This reduces the number of inconsistent items to {len(items_w_inconsistent_video_download_status)}")

        # recombine the two dataframes
        scrape_df = pd.concat([items_w_consistent_video_download_status,items_w_inconsistent_video_download_status])
        if verbose:
            print(f"After this procedure, the shape of the scrape DF is: {scrape_df.shape}")


    if verbose:
        print(f"Consolidating scrape data into a single file...")
    _ = data_io.save_parquet(cf, scrape_df, "recoded", "scrape_recoded.parquet")
    if verbose:
        print(f"Consolidated scrape data into a single file. Shape: {scrape_df.shape}")










def load_failed_scrapes(
    cf = None,
    consolidate = True,
    verbose = False,
    super_verbose = False):
    # Load list of failed scraped attempts.

    from datetime import datetime
    from fyp.fyp_main import initialize
    import fyp.data_io as data_io

    if cf is None:
        cf = initialize()

    if verbose:
        print("Loading failed scrapes...")

    failed_scrape_fn_core = "scrape_failed_items"

    failed_scrape_files = [gg for gg in data_io.listdir(cf, "scrape", verbose=verbose) if gg.startswith(failed_scrape_fn_core)]

    failed_scrapes = []
    for fn in failed_scrape_files:
        if super_verbose:
            print(fn)
        some_dict = data_io.load_json(cf, "scrape", fn, verbose=verbose)
        if some_dict is not None:
            failed_scrapes += some_dict

    failed_scrapes = list(set(map(lambda one_item_id:str(one_item_id), failed_scrapes)))


    if consolidate and len(failed_scrape_files) > 1:
        fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
        if verbose:
            print(f"{len(failed_scrapes):,} of these are unique and will be saved as a new consolidated file {failed_scrape_fn_core}_{fine_ts}.json.")

        data_io.save_json(cf, failed_scrapes, "scrape", f"{failed_scrape_fn_core}_{fine_ts}.json", verbose=verbose)

        for fn in failed_scrape_files:
            data_io.move(cf, "scrape", "archive", fn, verbose=verbose)
            if verbose:
                print(f"Moved {fn} to archive")


    if verbose:
        print(f"Loaded list of all failed scrapes: {len(failed_scrapes):,}")

    return failed_scrapes




