#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


REQUIRED_KEYS = [
    'transcript', 'objects', 'content_category', 'symbols_and_brands',
    'text_overlays', 'scenes',
]



# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# functions for loading and saving machine annotation files
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************








def load_machine_annotations(
        cf = None,
        include_failed_calls:bool = False,
        consolidate:bool = False,
        verbose=False,
        notebook_mode = False):

    if notebook_mode:
        verbose = True

    from pandas import DataFrame, concat
    from os import listdir, rename
    from os.path import join, basename, exists
    from shutil import move
    from datetime import datetime
    import shutil

    from fyp.fyp_main import init_config, connect_to_google, convert_dtypes_to_pyarrow
    import fyp.data_io as data_io

    if cf is None:
        cf = init_config()


    file_format = cf['misc']['file_format']


    machine_file_names = []
    for fn in listdir(cf['paths']['machine_annotations']):
        if fn.startswith("machine_annotations") and (fn.endswith(".parquet") or fn.endswith(".pkl")):
            machine_file_names.append(join(cf['paths']['machine_annotations'], ".".join(fn.split(".")[:-1])))
            
    #machine_file_names = [join(cf['paths']['machine_annotations'], ".".join(fn.split(".")[:-1])) for fn in listdir(cf['paths']['machine_annotations']) if fn.startswith("machine_annotations")]
    machine_file_names = list(set(machine_file_names))

    all_results = concat([data_io.load_dataset(fn, verbose=verbose) for fn in machine_file_names])

    all_results.reset_index(drop=True, inplace=True)
    all_results['error'] = all_results['error'].map(lambda x:"-" if x=={} else x)

    if verbose:
        print(f"Loaded {len(all_results):,} rows from {len(machine_file_names)} machine annotation files")

    all_results = all_results.sort_values("inference_ts").copy()
    quick_check = 1 - all_results.isna().sum()/len(all_results)
    all_results = all_results.loc[:,quick_check[quick_check>=0.1].index].copy()
    all_results.drop_duplicates(inplace=True, keep='last')

    if verbose:
        print(f"After full-row-dedup there are {len(all_results):,} rows, {len(all_results.columns)} cols, and {all_results.item_id.nunique():,} unique videos")



    if consolidate and len(machine_file_names) > 1:

        # consolidating the files to a single file using the latest file name
        # the reason for this is to not kick off potential secondary processes that are monitoring the folder
        # for new files. I want such processes to ignore files that are consolidations of other files
        # this has to happen before we potentially drop all failed annotations. We want to keep them in the
        # consolidated file  

        latest_filename = sorted(machine_file_names)[-1]
        #fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
        if verbose:
            print(f"The machine annotation files will be consolidated into a single file: '{basename(latest_filename)}'")
            print(f"The raw json files will remain untouched")

        all_results = convert_dtypes_to_pyarrow(all_results, verbose=verbose)
        data_io.save_dataset(all_results, latest_filename, verbose=verbose)


        for fn in machine_file_names:
            if not fn == latest_filename:
                if exists(fn+".parquet"):
                    shutil.move(fn+".parquet",join(cf['paths']['machine_annotations'],'archive',basename(fn)+".parquet"))
                    if verbose:
                        print(f"Moved {basename(fn)+'.parquet'} to archive")
                if exists(fn+".pkl"):
                    shutil.move(fn+".pkl",join(cf['paths']['machine_annotations'],'archive',basename(fn)+".pkl"))
                    if verbose:
                        print(f"Moved {basename(fn)+'.pkl'} to archive")




    if include_failed_calls:
        if verbose:
            print(f"Including failed machine annotation calls")
    else:
        # assuming the 'scenes' variable is not na if things have gone well
        all_results = all_results[~all_results["scenes"].isna()].copy()
        if verbose:
            print(f"Excluding failed machine annotation calls, which gives {len(all_results):,} rows, and {all_results.item_id.nunique():,} unique videos")


    if notebook_mode:
        print("--"*60)



    return all_results









def save_machine_annotations_json(
    json_list: list, 
    the_path:str, 
    verbose=False,
    notebook_mode = False):

    if notebook_mode:
        verbose = True
    from os.path import join
    from json import dump
    from datetime import datetime

    fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])

    with open(join(the_path,f"machine_annotations_{fine_ts}.json"),'w') as f:
        dump(json_list,f)
    if verbose:
        print(f"Saved raw machine annotations to 'machine_annotations_{fine_ts}.json'")






# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# functions in this section call the machine and get the raw responses
# *********************************************************************************************************
# *********************************************************************************************************









def call_machine(
        cf = None,
        video_id: int = None, 
        testing: bool = False,
        use_local_video_file = False,
        local_path: str = '/Users/<user>/Downloads/',
    ) -> dict:

    from datetime import datetime
    from json import dump
    import json
    from os.path import join, basename
    from google.genai import types
    from time import sleep
    from random import randint
    from copy import copy

    from fyp.fyp_main import init_config, connect_to_google, temp_path

    if cf is None:
        cf = init_config()
    if cf["machine"]["client"] is None:
        cf = connect_to_google(cf)



    if not testing:
        # The AI annotator doesn't like too many requests at once
        sleep(randint(1,100)/50)

    times = [datetime.now()]
    output = {
        "item_id" : video_id,
        "inference_ts" : int(times[-1].timestamp()),
        "inference_duration" : -1,
        "model" : cf['machine']['model'],
        "prompt_fn" : basename(cf['machine']['new_prompt']),
        "error" : "-",
        "finish_reason":"did not even start",
        "response" : "",
    }

    temp_fn = temp_path(cf, f"temp_machine_annotations_{output['item_id']}_{output['inference_ts']}.json")


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
                    file_uri=f"gs://{cf['media_storage']['GCP_bucket']}/{video_id}.mp4",
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
        resp = cf["machine"]["client"].models.generate_content(
            model = cf['machine']['model'],
            config = cf["machine"]["global_generation_config"],
            contents=contents,
        )
    except Exception as e:
        times += [datetime.now()]

        video_found = cf["media_storage"]["bucket"].blob(f"{video_id}.mp4").exists()

        output["error"] = str(e)
        output["inference_duration"] = (times[-1] - times[-2]).total_seconds()

        if not video_found:
            output["finish_reason"] = "DNF - file not found in storage"
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







def _start_monitor(futures, submit_times, interval=5, label="monitor", bar_width=30):
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








def call_machine_threads(
        cf = None,  
        interesting_videos = None,
        max_workers=50,
        verbose=False,
        notebook_mode = False):

    if notebook_mode:
        verbose = True

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime
    from time import sleep, time
    from random import random
    from os import environ

    from fyp.fyp_main import init_config, connect_to_google

    if cf is None:
        cf = init_config()
    if cf["machine"]["client"] is None:
        cf = connect_to_google(cf)



    results_by_index = {}

    def worker(idx_video):
        idx, video = idx_video

        # Maybe Gemini doesn't like to get to many request at once.
        # Sleeping for a bit with the first ones solves the problem
        if idx < max_workers:
            sleep(3+random()*max_workers/2)

        t1 = datetime.now()
        rr = call_machine(
            cf = cf,
            video_id = video,
            testing = False,
        )

        return idx, rr


    if verbose:
        print(f"Calling {cf['machine']['model']} to annotate {len(interesting_videos):,} videos with {max_workers} threads.")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:

        futures = []
        submit_times = {}
        for iv in enumerate(interesting_videos):
            fut = ex.submit(worker, iv)
            futures.append(fut)
            submit_times[fut] = time()

        monitor_thread = _start_monitor(futures, submit_times, interval=5, label="machine", bar_width=32)


        for fut in as_completed(futures):
            idx, res = fut.result()
            results_by_index[idx] = res

        monitor_thread.join()


    if verbose:
        print(f"Items processed: {len(results_by_index)}")


    if len(results_by_index)>0:
        save_machine_annotations_json(
            results_by_index,
            cf['paths']['machine_annotations'],
            verbose=verbose,
            notebook_mode=notebook_mode
        )

    return results_by_index






# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# I'm not using structured outputs because I believe that the machine is calling all the required bits
# and pieces in the request at the same time if I'd do that. I want it to think about it sequentially. So
# as a result it happens that the json like output structure is wrong and introduces labels and keys that
# I don't want. This funciton is trying to figure out which columns are rare and try to merge them back 
# into the dominant columns. 
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************




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
        print(outputs_from_machine_df.shape)
        print(len(nonnull_ratio[nonnull_ratio<0.1]))
        print(nonnull_ratio[nonnull_ratio<0.1])
        print(len(nonnull_ratio[nonnull_ratio<0.5]))
        print(nonnull_ratio[nonnull_ratio<0.5])
        print(len(nonnull_ratio[nonnull_ratio<0.8]))
        print(nonnull_ratio[nonnull_ratio<0.8])



    nonnull_ratio = (len(outputs_from_machine_df) - outputs_from_machine_df.isna().sum()) / len(outputs_from_machine_df)

    little_counter = 0

    while(len(nonnull_ratio[nonnull_ratio<0.1]))>0 and little_counter<5:
        if verbose:
            print(little_counter)
            print(len(nonnull_ratio[nonnull_ratio<0.1]))


        for unusual_col_name in nonnull_ratio[nonnull_ratio<0.1].index:
            try:
                if verbose:
                    print(len(outputs_from_machine_df) - outputs_from_machine_df[unusual_col_name].isna().sum())
                dominant_col_name = sort_by_similarity(unusual_col_name, nonnull_ratio[nonnull_ratio>0.9].index)[0]
                
                rows_w_nonnull_value_in_unusual_col = outputs_from_machine_df[~outputs_from_machine_df[unusual_col_name].isna()].loc[:,[dominant_col_name,unusual_col_name]]
                
                for ii in rows_w_nonnull_value_in_unusual_col.index:
                    if outputs_from_machine_df.loc[ii,dominant_col_name] is np.nan:
                        if verbose:
                            print("*******",ii,dominant_col_name)
                        outputs_from_machine_df.loc[ii,dominant_col_name] = outputs_from_machine_df.loc[ii,unusual_col_name]
                    else:
                        outputs_from_machine_df.loc[ii,unusual_col_name] = np.nan
            except:
                if verbose:
                    print(f"ERROR: {unusual_col_name} doesn't seem to be among the columns")


            little_counter += 1


        nonnull_ratio = (len(outputs_from_machine_df) - outputs_from_machine_df.isna().sum()) / len(outputs_from_machine_df)
        outputs_from_machine_df.drop(nonnull_ratio[nonnull_ratio==0].index,axis=1,inplace=True, errors='ignore')
        if verbose:
            print(outputs_from_machine_df.shape)
            print("------------------------------------------------------")
        
        
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


    from copy import deepcopy, copy
    from collections import Counter
    from fuzzy_json import loads
    import re

    # if the response is not a dictionary, something is wrong - return it as is
    if some_response is None or type(some_response) != dict:
        if notebook_mode:
            print(type(some_response))
        return some_response

    flat_response = deepcopy(some_response)

    # check if required keys are present
    for rk in REQUIRED_KEYS:
        if not rk in flat_response.keys():
            if verbose:
                print(f"WARNING: Required key '{rk}' is missing in response. Returning None")
            return None

    # #######################
    # scenes
    if 'scenes' in flat_response.keys():
        if isinstance(flat_response['scenes'], str):
            flat_response['scenes'] = re.sub(r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response['scenes'])
            try:
                flat_response['scenes'] = loads(flat_response['scenes'])
            except Exception as e:
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
                tt1 = Counter(sentiment_list).most_common(1)
                if len(tt1) == 0:
                    flat_response['scene_sentiments'] = ""
                else:
                    flat_response['scene_sentiments'] = tt1[0][0]
            except Exception as e:
                return None
            #elif isinstance(flat_response['scenes'], str):
            #    aa = re.sub(r"\{.*?\|", ' | ', flat_response['scenes'].replace("'description':"," | "))
            #    aa = re.sub(r"\'sentiment.*?\|", ' |', aa)
            #    aa = re.sub(r"\'sentiment.*?\}", ' ', aa)
            #    keep_it = copy(flat_response['scenes']) 
            #    flat_response['scenes'] = aa.replace("',  "," ").replace(" '"," ").replace("  "," ")[3:-3].strip()
            #    tt1 = Counter(list(map(lambda x: x.split("'}")[0], keep_it.split("sentiment':'")[1:]))).most_common(1)
            #    if len(tt1) == 0:
            #        flat_response['scene_sentiments'] = ""
            #    else:
            #        flat_response['scene_sentiments'] = tt1[0][0]
        else:
            return None
            #flat_response['scenes'] = ""
            #flat_response['scene_sentiments'] = ""

    # #######################
    # transcript
    if 'transcript' in flat_response.keys():
        if isinstance(flat_response['transcript'], str):
            flat_response['transcript'] = re.sub(r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response['transcript'])
            try:
                flat_response['transcript'] = loads(flat_response['transcript'])
            except Exception as e:
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
            except Exception as e:
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
                    flat_response[res_key] = loads(flat_response[res_key])
                except Exception as e:
                    print(flat_response[res_key])
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
                except Exception as e:
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
                flat_response['audio_summary'] = loads(flat_response['audio_summary'])
            except Exception as e:
                print(flat_response['audio_summary'])
                return None
        
        for k in flat_response['audio_summary']:
            try:
                audio_detail = flat_response['audio_summary'][k]
            except Exception as e:
                print(e,"|",k,"|",flat_response['audio_summary'])
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
                flat_response['faces'] = loads(flat_response['faces'])
            except Exception as e:
                print(flat_response['faces'])
                return None

        if isinstance(flat_response['faces'], list):
            for face in flat_response['faces']:
                if isinstance(face, dict):
                    for k in face:
                        if not "faces_"+k in flat_response.keys():
                            flat_response["faces_"+k] = ""                    
                        try:
                            flat_response["faces_"+k] += str(face[k]) + " | "
                        except Exception as e:
                            return None
                else:
                    return None
        else:
            return None
        del flat_response['faces']

        for k in flat_response:
            if (k.startswith("faces_")) and (flat_response[k].endswith(" | ")):
                flat_response[k] = flat_response[k][:-3]    


    # #######################
    # get rid of pesky lists that are still lingering - just pick the first element. This is a bit of a hack, but it works.
    for k in flat_response:
        if isinstance(flat_response[k],list):
            print(flat_response[k])
            flat_response[k] = flat_response[k][0]

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

    import re

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
    from copy import copy
    from fuzzy_json import loads

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
            
            machine_annotations = loads(refined_text)
            
            return machine_annotations
        except Exception as e:
            if notebook_mode:
                print(e, refined_text)
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

    from pandas import DataFrame
    from copy import copy

    bad_count = 0
    good_count = 0

    flattened_outputs_from_machine = {}
    for h in raw_outputs_from_machine:
        flattened_response = None
        flattened_outputs_from_machine[h] = copy(raw_outputs_from_machine[h])
        if raw_outputs_from_machine[h]['response'] is None or raw_outputs_from_machine[h]['response']=='':
            bad_count += 1
        else:
            json_response = fuzzy_load_of_json_from_string(raw_outputs_from_machine[h]['response'], notebook_mode = notebook_mode)
            flattened_response = flatten_one_machine_response(json_response, verbose = verbose, notebook_mode = notebook_mode)
            if type(flattened_response)==dict:
                good_count += 1
                for rk in flattened_response:
                    flattened_outputs_from_machine[h][rk] = copy(flattened_response[rk])
            else:
                bad_count += 1
                if verbose:
                    print("Error when postprocessing response -> bad response")
                if notebook_mode:
                    print(raw_outputs_from_machine[h])
    
    print(f"Flattened and fixed {good_count} good responses, {bad_count} bad responses")


    # convert the dict to a DF, reset the index and drop the old response structure 
    outputs_from_machine_df = DataFrame(flattened_outputs_from_machine).T
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

    from collections import defaultdict

    if not isinstance(text,str):
        return "Not a string"

    if len(text) > max_text_length:
        return "String too long"

    words = text.split()
    n = len(words)
    
    pattern_counts = defaultdict(int)
    
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
    from copy import deepcopy

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






def remove_repetitions_from_transcripts(
    outputs_from_machine_df_in, # expecting a dataframe with a column called "transcript". Elements should be a pipe-separated stringified list.
    verbose = False,
    notebook_mode = False):

    if notebook_mode:
        verbose = True

    from copy import copy

    if verbose:
        print("Removing repeated patterns in the transcripts - this may take a little while")

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
        print("Prettifying all strings")
    outputs_from_machine_df = outputs_from_machine_df.map(lambda x:x if not isinstance(x,str) else _prettify_string(x)).copy()

    return outputs_from_machine_df







# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# the highest level functions
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************


def post_process_raw_annotations(
    cf = None,
    raw_outputs_from_machine = None,
    verbose = False,
    notebook_mode = False):

    if notebook_mode:
        verbose = True

    from datetime import datetime
    from os.path import join
    from fyp.fyp_main import init_config, connect_to_google, temp_path, convert_dtypes_to_pyarrow
    import fyp.data_io as data_io

    if raw_outputs_from_machine is None:
        raise ValueError("raw_outputs_from_machine cannot be None")

    if cf is None:
        cf = init_config()
    if cf["machine"]["client"] is None:
        cf = connect_to_google(cf)
    
    file_format = cf['misc']['file_format']

    print("Starting post-processing of raw annotations...")
    if verbose:
        print("Flattening raw machine annotations")
    outputs_from_machine_df = flatten_and_fix_machine_outputs(raw_outputs_from_machine, verbose = verbose, notebook_mode = notebook_mode)


    # check if required keys are present
    found_all_required_keys = True
    for rk in REQUIRED_KEYS:
        if not rk in outputs_from_machine_df.columns:
            print(f"WARNING: Essential column '{rk}' is missing in machine output")
            found_all_required_keys = False

    if verbose:
        print("Consolidating rare columns from machine annotations")
    outputs_from_machine_df = consolidate_rare_columns_from_gemini_output(outputs_from_machine_df, verbose = verbose, notebook_mode = notebook_mode)

    if 'transcript' in outputs_from_machine_df.columns:
        if verbose:
            print("Removing repetitions from machine annotation transcripts")
        outputs_from_machine_df = remove_repetitions_from_transcripts(outputs_from_machine_df, verbose = verbose, notebook_mode = notebook_mode)

    if verbose:
        print("Ready to save processed results")
    fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
    file_prefix = "machine_annotations"

    outputs_from_machine_df = convert_dtypes_to_pyarrow(outputs_from_machine_df, verbose=verbose)
    data_io.save_dataset(outputs_from_machine_df, join(cf["paths"]["machine_annotations"], f"{file_prefix}_{fine_ts}{file_format}"), verbose=verbose)
    if verbose:
        print(f"Saved processed results to '{file_prefix}_{fine_ts}{file_format}'")
    
    return outputs_from_machine_df
    





def annotate_from_list(
    cf = None,
    fine_list = None,
    max_workers = 50,
    verbose = False,
    notebook_mode = False):

    if notebook_mode:
        verbose = True
    """
    This function takes a list of video IDs and calls the machine to annotate them.
    It also performs the necessary post processing of the raw outputs from the machine.
    """

    from os import environ
    from fyp.fyp_main import init_config, connect_to_google, temp_path

    if cf is None:
        cf = init_config()
    if cf["machine"]["client"] is None:
        cf = connect_to_google(cf)
    



    if isinstance(fine_list, list) and len(fine_list) > 0:
        if not all(map(lambda video_id:type(video_id)==int and len(str(video_id))==19, fine_list)):
            if verbose:
                print("Some videoIDs in the list were corrupt. Cannot process this list.")
            return None

        print("Annotating videos...")

        raw_outputs_from_machine = call_machine_threads(
                cf = cf,
                interesting_videos = fine_list,
                max_workers=max_workers,
                verbose = verbose, notebook_mode = notebook_mode
            )

        print("...video annotation completed.")

        _ = post_process_raw_annotations(
            cf = cf,
            raw_outputs_from_machine = raw_outputs_from_machine,
            verbose = verbose, notebook_mode = notebook_mode)

    else:
        if verbose:
            print(f"No videos to process")





def annotate_from_scrape_metadata_file(
    cf = None,
    scrape_metadata_filename = None,
    verbose = False,
    notebook_mode = False):

    if notebook_mode:
        verbose = True
    """
    This is a wrapper that is reading a scrape metadata file and extracts a list of video IDs
    to process. It then calls annotate_from_list.
    """
    from os.path import exists
    import fyp.data_io as data_io

    from fyp.fyp_main import init_config, connect_to_google

    if cf is None:
        cf = init_config()
    if cf["machine"]["client"] is None:
        cf = connect_to_google(cf)


    if scrape_metadata_filename is None or not exists(scrape_metadata_filename):
        if verbose:
            print(f"File {scrape_metadata_filename} does not exist. Cannot process this file.")
        return None

    df = data_io.load_dataset(scrape_metadata_filename)

    # we're only annotating the videos that are downloaded and shorter than a certain max duration
    work_with_these_videos_list = df[(df["video_downloaded"]) & (df["video_duration"]<cf["machine"]["max_duration_for_annotation"])]["item_id"].tolist()

    annotate_from_list(
        cf = cf,
        fine_list = work_with_these_videos_list,
        verbose = verbose, notebook_mode = notebook_mode)






def post_process_raw_annotations_from_json_file(
    cf = None,
    json_file = None,
    verbose = False,
    notebook_mode = False):

    if notebook_mode:
        verbose = True
    """
    This is useful when the post_processing crashes. It's expensive to call the machine so
    it's preferrable to use the raw json and try to fix whatever might be causing the trouble
    """
    from datetime import datetime
    from json import load
    from os.path import exists

    from fyp.fyp_main import init_config, connect_to_google

    if cf is None:
        cf = init_config()

    if json_file is None or not exists(json_file):
        if verbose:
            print(f"File {json_file} does not exist. Cannot process this file.")
        return None

    with open(json_file, 'r') as f:
        raw_outputs_from_machine = load(f)

    #process raw_outputs_from_machine
    _ = post_process_raw_annotations(cf = cf, raw_outputs_from_machine = raw_outputs_from_machine, verbose = verbose, notebook_mode = notebook_mode)









def annotate_videos_loop(
    cf = None,
    study_name = None,
    batch_size = 500,
    max_batches = None,
    verbose = False,
    notebook_mode = False):

    if notebook_mode:
        verbose = True

    from datetime import datetime
    from os import environ
    from os.path import join
    import json

    from fyp.fyp_main import init_config, connect_to_google
    from fyp.organize_datasets_OPTIMIZED import select_videos_from_half_baked

    if cf is None:
        cf = init_config()
    if cf["machine"]["client"] is None:
        cf = connect_to_google(cf)

    if study_name is None:
        if verbose:
            print("No study name provided. Cannot process without a study name...")
        return None

    print(f"Annotating downloaded videos, study '{study_name}', batch size: {batch_size}, max batches: {max_batches}")



    selected_videos = [0] # just a list that contains anything and that is longer than zero elements to get things started
    batch_number = 1

    print()
    print("Starting loop...")
    while len(selected_videos)>0:
        print(f"Now: {datetime.now()}")
        print("--"*60)
        selected_videos = select_videos_from_half_baked(
            cf = cf,
            study_name = study_name,
            file_label = "ANNOTATE",
            INCLUDE_UNSEEN_VIDEOS_IN_EXPORT = False,
            INCLUDE_FAILED_SCRAPES_IN_EXPORT = False,
            INCLUDE_SCRAPED_BUT_NOT_DOWNLOADED_IN_EXPORT = False,
            INCLUDE_DOWNLOADED_BUT_NOT_ANNOTATED_IN_EXPORT = True,
            INCLUDE_FAILED_ANNOTATIONS_IN_EXPORT = False,
            INCLUDE_DOWNLOADED_AND_ANNOTATED_IN_EXPORT = False,
            verbose = verbose, notebook_mode = notebook_mode
        )


        if len(selected_videos) > 0:
            work_with_these_videos_list_raw = [str(k) for k in selected_videos.item_id.to_list()]
            work_with_these_videos_list = work_with_these_videos_list_raw.copy()

            print(f"{len(work_with_these_videos_list):,} videos selected")

            _ = annotate_from_list(
                cf = cf,
                fine_list = work_with_these_videos_list[:batch_size],
                verbose = verbose, notebook_mode = notebook_mode)
        
        if selected_videos is None:
            selected_videos = []

        if max_batches is not None and batch_number >= max_batches:
            break

        batch_number += 1



    print(f"Loop ended: {datetime.now()}")





# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************


