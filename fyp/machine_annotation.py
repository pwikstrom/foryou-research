#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""

import fyp.fyp_main as fyp




def flatten_new_machine_output(some_response):
    from copy import deepcopy
    from collections import Counter

    if some_response is None or type(some_response) != dict:
        print(type(some_response))
        return some_response

    flat_response = deepcopy(some_response)

    #print(1)

    required_things = [
        'transcript', 'objects', 'content_category', 'symbols_and_brands',
        'text_overlays', 'faces', 'scenes', 'audio_summary',
    ]
    for rt in required_things:
        if not rt in flat_response.keys():
            print(f"required key '{rt}' is missing in response. Returning None")
            return None


    if 'scene_sentiments' in flat_response.keys():
        if type(flat_response['scene_sentiments']) != str:
            flat_response['scene_sentiments'] = Counter([k.get('sentiment','') for k in flat_response['scene_sentiments']]).most_common(1)[0][0]
    elif 'scenes' in flat_response.keys():
        flat_response['scene_sentiments'] = Counter([k.get('sentiment','') for k in flat_response['scenes']]).most_common(1)[0][0]
    else:
        return None
    #print(2)

    if 'scenes' in flat_response.keys():
        if type(flat_response['scenes']) != str:
            flat_response['scenes'] = " | ".join([k.get('description','') for k in flat_response['scenes']])
        #print(3)


    if type(flat_response['transcript']) != str:
        flat_response['transcript'] = " | ".join([k if type(k)!=dict else k.get('text','') for k in flat_response['transcript']])
    #print(4)

    flat_response['objects'] = " | ".join(flat_response['objects'])


    flat_response['symbols_and_brands'] = " | ".join([s for s in flat_response['symbols_and_brands'] if type(s)==str])


    flat_response['text_overlays'] = " | ".join([s for s in flat_response['text_overlays'] if type(s)==str])
    flat_response['content_category'] = " | ".join([s for s in flat_response['content_category'] if type(s)==str])

    #print(5)

    # sometimes audio summary hasn't been converted to json
    # not sure why this happens, this is trying to do something about that
    audio_summary_ok = True
    if isinstance(flat_response['audio_summary'],str):
        try:
            flat_response['audio_summary'] = eval(flat_response['audio_summary'])
        except:
            audio_summary_ok = False
    
    #print(6)

    if audio_summary_ok:
        for k in flat_response['audio_summary']:
            try:
                audio_detail = flat_response['audio_summary'][k]
            except Exception as e:
                print(e,"|",k,"|",flat_response['audio_summary'])
                return None
            if isinstance(audio_detail,list):
                flat_response[k] = " | ".join([s for s in audio_detail if type(s)==str])
            else:
                flat_response[k] = audio_detail
    del flat_response['audio_summary']

    #print(7)

    if type(flat_response['faces']) != str:
        if type(flat_response['faces']) != list:
            flat_response['faces'] = [flat_response['faces']]
        for face in flat_response['faces']:
            for k in face:
                if not "faces_"+k in flat_response.keys():
                    flat_response["faces_"+k] = ""
                flat_response["faces_"+k] += str(face[k]) + " | "
        

    #print(8)

    del flat_response['faces']

    #print(9)

    for k in flat_response:
        if (k.startswith("faces_")) and (flat_response[k].endswith(" | ")):
            flat_response[k] = flat_response[k][:-3]    

    return flat_response










def save_machine_annotations(json_list: list, the_path:str):
    from pandas import DataFrame
    from os.path import join
    from json import dump
    from datetime import datetime

    fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])

    with open(join(the_path,f"machine_annotations_{fine_ts}.json"),'w') as f:
        dump(json_list,f)
    print(f"saved results as 'machine_annotations_{fine_ts}.json'")






# I only use this for the transcriptions which often tend to be a bit repetitive
def remove_repetitions(some_string):
    from copy import deepcopy

    new_string = deepcopy(some_string.replace("-"," "))

    res = fyp.check_repetitive_patterns(
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





def prettify_string(a_string):
    from copy import deepcopy
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



def load_machine_annotations(
        the_path:str = fyp.cf['paths']['machine_annotations'],
        include_failed_calls:bool = False,
        verbose = True
    ):
    from pandas import DataFrame, concat, read_pickle
    from os import listdir
    from os.path import join


    machine_file_names = [fn for fn in listdir(the_path) if fn.endswith(".pkl") and fn.startswith("machine_annotations")]

    all_results = concat([read_pickle(join(the_path,fn)) for fn in machine_file_names])

    all_results.reset_index(drop=True, inplace=True)
    all_results['error'] = all_results['error'].map(lambda x:"-" if x=={} else x)

    if verbose:
        print(f"Loaded {len(all_results):,} rows from {len(machine_file_names)} machine annotation files")

    all_results = all_results.sort_values("inference_ts").copy()
    all_results.drop_duplicates(inplace=True, keep='last')

    if verbose:
        print(f"After full-row-dedup there are {len(all_results):,} rows, {len(all_results.columns)} cols, and {all_results.item_id.nunique():,} unique videos")

    if include_failed_calls:
        if verbose:
            print(f"Including failed machine annotation calls")
            print("------------------------------------------------------------------------------------------------------------------")
    else:
        # assuming the 'scenes' variable is not na if things have gone well
        all_results = all_results[~all_results["scenes"].isna()].copy()
        if verbose:
            print(f"Excluding failed machine annotation calls, which gives {len(all_results):,} rows, and {all_results.item_id.nunique():,} unique videos")
            print("------------------------------------------------------------------------------------------------------------------")

    return all_results





def call_machine(
        video_id: int, 
        testing: bool = False,
        use_local_video_file = False,
        local_path: str = '/Users/<user>/Downloads/',
        the_machine_client = fyp.cf["machine"]["client"],
        the_machine_model = fyp.cf['machine']['model'],
        the_machine_config = fyp.cf["machine"]["global_generation_config"]
    ) -> dict:

    from datetime import datetime
    from json import dump
    from os.path import join, basename
    from google.genai import types
    from time import sleep
    from random import randint
    from copy import copy

    if not testing:
        # The AI annotator doesn't like too many requests at once
        sleep(randint(1,100)/50)

    times = [datetime.now()]
    output = {
        "item_id" : video_id,
        "inference_ts" : int(times[-1].timestamp()),
        "inference_duration" : -1,
        "model" : fyp.cf['machine']['model'],
        "prompt_fn" : basename(fyp.cf['machine']['new_prompt']),
        "error" : "-",
        "finish_reason":"did not even start",
        "response" : "",
    }

    temp_fn = join(fyp.temp_path(f"temp_machine_annotations_{output['item_id']}_{output['inference_ts']}.json"))


    # initialise the contents for the model
    try:
        if use_local_video_file:
            print(f"Using local video file for video id {video_id}")
            with open(join(local_path,f"{video_id}.mp4"),'rb') as f:
                video_bytes = f.read()
            contents = [
                types.Part(
                    inline_data=types.Blob(data=video_bytes, 
                    mime_type='video/mp4')
                ),
                types.Part.from_text(text="Analyze this video")
            ]
        else:
            contents = [
                types.Part.from_uri(
                    file_uri=f"gs://{fyp.cf['media_storage']['GCP_bucket']}/{video_id}.mp4",
                    mime_type="video/mp4"
                ),
                types.Part.from_text(text="Analyze this video")
            ]
    
    except Exception as e:
        output["error"] = str(e)
        with open(temp_fn, 'w') as file:
            dump(output,file)
        return output

    # run the model
    try:
        start_ts = datetime.now()
        resp = the_machine_client.models.generate_content(
            model=the_machine_model,
            config=the_machine_config,
            contents=contents,
        )
    except Exception as e:
        times += [datetime.now()]

        video_found = fyp.cf["media_storage"]["bucket"].blob(f"{video_id}.mp4").exists()

        output["error"] = str(e)
        output["inference_duration"] = (times[-1] - times[-2]).total_seconds()

        if not video_found:
            output["finish_reason"] = "DNF - file not found in storage"
        #elif output["inference_duration"] > 360:
        #    output["finish_reason"] = "DNF - timeout limit exceeded"
        else:
            output["finish_reason"] = "DNF - see error msg"

        with open(temp_fn, 'w') as file:
            dump(output,file)
        return output


    try:
        the_finish_reason = str(resp.candidates[0].finish_reason)
    except:
        the_finish_reason = "Finished, but don't know why"
    
    times += [datetime.now()]

    try:
        machine_annotations = copy(resp.text)
    except Exception as e:
        output["error"] = str(e)
        output["inference_duration"] = (times[-1] - times[-2]).total_seconds()
        output["finish_reason"] = the_finish_reason
        output["response"] = resp

        with open(temp_fn, 'w') as file:
            dump(output,file)
        return output

    output["inference_duration"] = (times[-1] - times[-2]).total_seconds()
    output["finish_reason"] = the_finish_reason
    output["response"] = machine_annotations

    # save the json just in case everything crashes
    with open(temp_fn, 'w') as file:
        dump(output,file)

    return output




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
            remaining = total - done
            eta = (remaining / throughput) if throughput > 0 else None

            bar = _bar(done, total, width=bar_width)

            line = (
                f"[{label}] {bar}  "
                f"done {done:,}/{total:,}  running {running}  pending {pending:,}  "#failed {failed}  "
                f"elapsed {_fmt_secs(elapsed)}  "#avg {_fmt_secs(avg_turnaround)}  "
                f"rate {throughput:.2f}/s  ETA {_fmt_secs(eta)}     "
            )

            # trim to terminal width if needed
            try:
                term_width = shutil.get_terminal_size(fallback=(160, 20)).columns
            except Exception:
                term_width = 160
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





def call_machine_threads(
        interesting_videos,
        max_workers=32,
        the_machine_client = fyp.cf["machine"]["client"],
        the_machine_model = fyp.cf['machine']['model'],
        the_machine_config = fyp.cf["machine"]["global_generation_config"]
    ):

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime
    from time import sleep, time
    from random import random

    results_by_index = {}

    def worker(idx_video):
        idx, video = idx_video

        # Gemini doesn't like to get to many request at once
        # sleeping for a bit with the first ones solves the problem
        if idx < max_workers:
            sleep(3+random()*max_workers/2)

        t1 = datetime.now()
        rr = call_machine(
            video,
            testing = False,
            the_machine_client = the_machine_client,
            the_machine_model = the_machine_model,
            the_machine_config = the_machine_config
        )
        inference_duration = (datetime.now()-t1).total_seconds()
        if False and isinstance(rr,dict):
            print(f"{idx:05} {rr['item_id']} - {rr['finish_reason']} - {inference_duration:.2f}s",end="", flush=True)
            if 'error' in rr and len(rr['error'])>5:
                print(f"{rr['error']}")
            else:
                print(f"")
        return idx, rr


    print(f"Calling {fyp.cf['machine']['model']} to code {len(interesting_videos)} videos with {max_workers} threads.")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:

        futures = []
        submit_times = {}
        for iv in enumerate(interesting_videos):
            fut = ex.submit(worker, iv)
            futures.append(fut)
            submit_times[fut] = time()
        #futures = [ex.submit(worker, iv) for iv in enumerate(interesting_videos)]

        monitor_thread = start_monitor(futures, submit_times, interval=5, label="machine", bar_width=32)

        for fut in as_completed(futures):
            #try:
            idx, res = fut.result()
            results_by_index[idx] = res
            #except Exception as e:
            #    idx = next(i for i, f in enumerate(futures) if f is fut)  
            #    print("dead wrong")
            #    results_by_index[idx] = str(e)  

        monitor_thread.join()


    print(f"items processed: {len(results_by_index)}")

    if len(results_by_index)>0:
        save_machine_annotations(
            results_by_index,
            fyp.cf['paths']['machine_annotations']
        )

    # this function returns the results but the pipeline is using the json
    # that is saved to disk 
    return results_by_index



# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
