#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""



from typing import List, Tuple, Union, Sequence



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
    from PIL import Image, ImageColor



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
    video_id: int = None, 
    verbose: bool = True,
    ):

    import fyp.mypyktok as pyk
    from os.path import join, exists, getsize
    from fyp.fyp_main import init_config, connect_to_google

    if cf is None:
        cf = init_config()
    if cf['media_storage']['bucket'] is None:
        cf = connect_to_google(cf)

    if cf['media_storage']['bucket'] is None:
        raise ValueError("No media storage bucket specified")
    if video_id is None:
        raise ValueError("No video id specified")


    pyk.specify_browser('chrome')

    #tiktok_url = f"https://www.tiktokv.com/share/video/{video_id}/"
    tiktok_url = f"https://www.tiktok.com/@/video/{video_id}/"

    # try to scrape metadata and download video
    scrape_metadata = pyk.save_tiktok(
        tiktok_url,
        save_video=True,
        max_duration_to_save = cf['misc']['max_duration_for_download'],
        browser_name='chrome',
        save_path="",
        stream_to_bucket = cf["media_storage"]["bucket"],
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
                blob = cf["media_storage"]["bucket"].blob(f"{video_id}.mp4")
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
                    blob = cf["media_storage"]["bucket"].get_blob(f"{video_id}_{ccc:02}.jpeg")

                    while blob and blob.exists():
                        blob.download_to_filename(join(cf["paths"]["temp"],f"{video_id}_{ccc:02}.jpeg"))
                        if blob.size >= cf["misc"]["min_media_object_size"]:
                            image_files.append(join(cf["paths"]["temp"],f"{video_id}_{ccc:02}.jpeg"))
                        ccc += 1
                        blob = cf["media_storage"]["bucket"].get_blob(f"{video_id}_{ccc:02}.jpeg")

                    # use the images to build a slideshow
                    make_slideshow(
                        image_files,
                        output=join(cf["paths"]["temp"],f"{video_id}.mp4"),
                        duration=2,
                        swipe=False,
                        verbose=verbose
                    )

                    # upload the video slideshow to the storage bucket if it is large enough
                    if getsize(join(cf["paths"]["temp"],f"{video_id}.mp4")) > cf["misc"]["min_media_object_size"]:
                        if verbose:
                            print(f"Uploading video file to storage bucket...")
                        blob = cf["media_storage"]["bucket"].blob(f"{video_id}.mp4")
                        blob.upload_from_filename(join(cf["paths"]["temp"],f"{video_id}.mp4"))
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
                if cf["media_storage"]["bucket"].blob(f"{video_id}.mp4").exists():
                    blob = cf["media_storage"]["bucket"].get_blob(f"{video_id}.mp4")
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
    

    return video_id








def start_monitor(futures, submit_times, interval=5, label="monitor", bar_width=30):
    """
    futures: list[Future]
    submit_times: dict[Future -> float]  time.time() at submission
    """

    import threading
    import time
    import sys
    import shutil
    from pandas import DataFrame
    from os import environ
    import json


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
                good_scrapes += [1 if type(res)==DataFrame else 0]
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
    cf = None,
    interesting_videos = None,
    max_workers=4,
    verbose=False):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pandas import concat, DataFrame
    from datetime import datetime
    from os import rename
    from os.path import join
    import json
    import time
    from fyp.fyp_main import init_config, connect_to_google


    if cf is None:
        cf = init_config()
    if cf['media_storage']['bucket'] is None:
        cf = connect_to_google(cf)

    if cf['media_storage']['bucket'] is None:
        raise ValueError("No media storage bucket specified")
    if interesting_videos is None:
        raise ValueError("No interesting videos specified")

    if len(interesting_videos) == 0:
        return DataFrame()

    results_by_index = {}

    def worker(idx_video):
        idx, video = idx_video
        return idx, download_single_video(
            cf = cf,
            video_id = video, 
            verbose=verbose)

    if verbose:
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
            #try:
            idx, res = fut.result()
            results_by_index[idx] = res
            #except Exception as e:
            #    idx = next(i for i, f in enumerate(futures) if f is fut)  
            #    results_by_index[idx] = e  
        
        monitor_thread.join()

    results = []
    failed_items = []
    for idx in range(len(interesting_videos)):
        if idx in results_by_index.keys() and type(results_by_index[idx])==DataFrame and results_by_index[idx].shape[1]>10: # download good
            results += [results_by_index[idx]]
        else:
            failed_items += [results_by_index[idx]]

    results = concat(results)

    fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
    
    if len(results)>0:
        
        final_path = join(cf['paths']['scrape'], f"scrape_metadata_{fine_ts}.pkl")
        temp_path = final_path + ".tmp"
        results.to_pickle(temp_path)
        rename(temp_path, final_path)
        print(f"Saved {len(results):,} rows to 'scrape_metadata_{fine_ts}.pkl'")
        print(f"and saved media objects to the bucket for {len(results[results['video_downloaded']]):,} of these.")

    if len(failed_items)>0:
        with open(join(cf['paths']['scrape'],f"scrape_failed_items_{fine_ts}.json"), "w") as jf:
            json.dump(failed_items, jf)
        print(f"Saved {len(failed_items)} failed items")


    return results







def download_videos_loop(
    cf = None,
    study_name = None,
    batch_size = 500,
    max_batches = None,
    verbose = False):

    from datetime import datetime
    from fyp.organize_datasets_OPTIMIZED import select_videos_from_half_baked
    from fyp.fyp_main import init_config, connect_to_google
    from os import environ

    if cf is None:
        cf = init_config()
    if cf['media_storage']['bucket'] is None:
        cf = connect_to_google(cf)

    if study_name is None:
        raise ValueError("No study name specified")

    # --- TEST MODE ---
    if environ.get("FYP_TESTING") and environ.get("FYP_TESTING") == "true":
        print("!!! TEST MODE ENABLED - Doing a mini batch once!!!")
        batch_size = 10
        max_batches = 1


    print(f"Downloading media objects and metadata for unseen videos, study '{study_name}', batch size: {batch_size}, max batches: {max_batches}")
    print(f"Now: {datetime.now()}")
    print("##"*60)


    selected_videos = [0] # just a non-empty list to get things started
    batch_number = 1

    print("Starting loop...")
    while len(selected_videos)>0:
        selected_videos = select_videos_from_half_baked(
            cf = cf,
            study_name = study_name,
            file_label = "SCRAPE",
            INCLUDE_UNSEEN_VIDEOS_IN_EXPORT = True,
            INCLUDE_FAILED_SCRAPES_IN_EXPORT = False,
            INCLUDE_SCRAPED_BUT_NOT_DOWNLOADED_IN_EXPORT = False,
            INCLUDE_DOWNLOADED_BUT_NOT_ANNOTATED_IN_EXPORT = False,
            INCLUDE_FAILED_ANNOTATIONS_IN_EXPORT = False,
            INCLUDE_DOWNLOADED_AND_ANNOTATED_IN_EXPORT = False,
            verbose = verbose
        )


        if len(selected_videos) > 0:
            work_with_these_videos_list_raw = [int(k) for k in selected_videos.item_id.to_list()]
            work_with_these_videos_list = work_with_these_videos_list_raw.copy()

            print(f"{len(work_with_these_videos_list):,} videos to process for study '{study_name}'")

            _ = download_video_threads(
                cf = cf,
                interesting_videos = work_with_these_videos_list[:batch_size], 
                max_workers=4, 
                verbose = verbose)
        
        if selected_videos is None:
            selected_videos = []

        if max_batches is not None and batch_number >= max_batches:
            break

        batch_number += 1


    print(f"Loop ended: {datetime.now()}")