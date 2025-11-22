#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


import fyp.fyp_main as fyp




def download_single_video(video_id: int):

    import fyp.mypyktok as pyk

    pyk.specify_browser('chrome')

    #tiktok_url = f"https://www.tiktokv.com/share/video/{video_id}/"
    tiktok_url = f"https://www.tiktok.com/@/video/{video_id}/"

    pyk_metadata = pyk.save_tiktok(
        tiktok_url,
        save_video=True,
        max_duration_to_save=fyp.cf['misc']['max_video_duration_for_download'],
        browser_name='chrome',
        save_path="",
        stream_to_bucket=fyp.cf["media_storage"]["bucket"],
        verbose=True
    )


    try:
        col_count = len(pyk_metadata.columns)
        if col_count > 1 and pyk_metadata.iloc[0]['video_downloaded']==True:
            if len(pyk_metadata.loc[0,'image_list'])>0:
                pass#print(f"OK   - Photos downloaded - '{video_id}' - {col_count} metadata fields")
            else:
                pass#print(f"OK   - Video downloaded '{video_id}' - {col_count} metadata fields")
            return pyk_metadata
        elif col_count > 1 and pyk_metadata.iloc[0]['video_downloaded']==False:
            pass#print(f"Accessed {col_count} metadata fields for {video_id} but did not download media object(s)")
            return pyk_metadata
        else:
            pass#print(f"Insufficient metadata columns ({col_count}) - Download of {video_id} - failed")
    except Exception as e:
        print(e.message, e.args)
    

    return video_id


def start_monitor(futures, interval=5):

    import threading
    import time
    import sys
    import shutil

    def _run():
        total = len(futures)
        start = time.time()
        while True:
            done = sum(f.done() for f in futures)
            running = sum(f.running() for f in futures)
            pending = total - done - running
            elapsed = time.time() - start

            print(
                f"[monitor] done: {done}/{total}, "
                f"running: {running}, pending: {pending}, "
                f"elapsed: {elapsed:.1f}s"
            )

            if done == total:
                break
            time.sleep(interval)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t




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





def download_video_threads(interesting_videos, max_workers=4):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pandas import concat, DataFrame
    from datetime import datetime
    from os.path import join
    import json
    import time

    results_by_index = {}

    def worker(idx_video):
        idx, video = idx_video
        return idx, download_single_video(video)

    print(f"Scraping data for {len(interesting_videos)} items with {max_workers} threads.")


    with ThreadPoolExecutor(max_workers=max_workers) as ex:


        futures = []
        submit_times = {}
        for iv in enumerate(interesting_videos):
            fut = ex.submit(worker, iv)
            futures.append(fut)
            submit_times[fut] = time.time()



        #futures = [ex.submit(worker, iv) for iv in enumerate(interesting_videos)]

        #monitor_thread = start_monitor(futures, interval=60)
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
        results.to_pickle(join(fyp.cf['paths']['pyk'],f"pyk_metadata_{fine_ts}.pkl"))
        print(f"Saved {len(results):,} rows to 'pyk_metadata_{fine_ts}.pkl'")
        print(f"and saved media objects to the bucket for {len(results[results['video_downloaded']]):,} of these.")

    if len(failed_items)>0:
        with open(join(fyp.cf['paths']['pyk'],f"pyk_failed_items_{fine_ts}.json"), "w") as jf:
            json.dump(failed_items, jf)
        print(f"Saved {len(failed_items)} failed items")


    return results

