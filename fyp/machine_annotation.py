#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""

import fyp.fyp_main as fyp




# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# functions for loading and saving machine annotation files
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************








def load_machine_annotations(
        include_failed_calls:bool = False,
        consolidate:bool = False,
        verbose:bool = False,
        completely_quiet:bool = False
    ):
    from pandas import DataFrame, concat, read_pickle
    from os import listdir, rename
    from os.path import join, basename
    from shutil import move
    from datetime import datetime


    machine_file_names = [join(fyp.cf['paths']['machine_annotations'], fn) for fn in listdir(fyp.cf['paths']['machine_annotations']) if fn.endswith(".pkl") and fn.startswith("machine_annotations")]

    all_results = concat([read_pickle(fn) for fn in machine_file_names])

    all_results.reset_index(drop=True, inplace=True)
    all_results['error'] = all_results['error'].map(lambda x:"-" if x=={} else x)

    if verbose or not completely_quiet:
        print(f"Loaded {len(all_results):,} rows from {len(machine_file_names)} machine annotation files")

    all_results = all_results.sort_values("inference_ts").copy()
    all_results.drop_duplicates(inplace=True, keep='last')

    if verbose:
        print(f"After full-row-dedup there are {len(all_results):,} rows, {len(all_results.columns)} cols, and {all_results.item_id.nunique():,} unique videos")

    if include_failed_calls:
        if verbose:
            print(f"Including failed machine annotation calls")
    else:
        # assuming the 'scenes' variable is not na if things have gone well
        all_results = all_results[~all_results["scenes"].isna()].copy()
        if verbose:
            print(f"Excluding failed machine annotation calls, which gives {len(all_results):,} rows, and {all_results.item_id.nunique():,} unique videos")


    if consolidate and len(machine_file_names) > 1:

        # consolidating the files to a single file using the latest file name
        # the reason for this is to not kick off potential secondary processes that are monitoring the folder
        # for new files. I want such processes to ignore files that are consolidations of other files

        latest_filename = sorted(machine_file_names)[-1]
        #fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
        if verbose:
            print(f"The machine annotation pkl files will be consolidated into a single file: '{basename(latest_filename)}'")
            print(f"The raw json files will remain untouched")

        all_results.to_pickle(latest_filename+".temp")

        for fn in machine_file_names:
            move(fn,join(fyp.cf['paths']['machine_annotations'], 'archive',basename(fn)))
            if verbose:
                print(f"Moved {basename(fn)} to archive")

        rename(latest_filename+".temp", latest_filename)


    if verbose or not completely_quiet:
        print("--"*60)



    return all_results









def save_machine_annotations_json(json_list: list, the_path:str, verbose = False):
    from pandas import DataFrame
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
    import json
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
        interesting_videos,
        max_workers=32,
        the_machine_client = fyp.cf["machine"]["client"],
        the_machine_model = fyp.cf['machine']['model'],
        the_machine_config = fyp.cf["machine"]["global_generation_config"],
        verbose: bool = False
    ):

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime
    from time import sleep, time
    from random import random
    from os import environ


    # --- TEST MODE ---
    if environ.get("FYP_TESTING") and environ.get("FYP_TESTING") == "true":
        import time
        import random
        print("!!! TEST MODE ENABLED - SKIPPING API CALL !!!")
        time.sleep(2) # Simulate network delay
        
        mock_response = {}
        for idx, vid in enumerate(interesting_videos):
            mock_response[idx] = {
                "response": {"description": "Test description", "transcript": "Test transcript | Test"},
                "finish_reason": "TEST"
            }
        return mock_response
    # -----------------



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
        #inference_duration = (datetime.now()-t1).total_seconds()
        #if False and isinstance(rr,dict):
        #    print(f"{idx:05} {rr['item_id']} - {rr['finish_reason']} - {inference_duration:.2f}s",end="", flush=True)
        #    if 'error' in rr and len(rr['error'])>5:
        #        print(f"{rr['error']}")
        #    else:
        #        print(f"")
        return idx, rr




    if verbose:
        print(f"Calling {fyp.cf['machine']['model']} to annotate {len(interesting_videos):,} videos with {max_workers} threads.")



    with ThreadPoolExecutor(max_workers=max_workers) as ex:

        futures = []
        submit_times = {}
        for iv in enumerate(interesting_videos):
            fut = ex.submit(worker, iv)
            futures.append(fut)
            submit_times[fut] = time()
        #futures = [ex.submit(worker, iv) for iv in enumerate(interesting_videos)]

        #if verbose:
        monitor_thread = _start_monitor(futures, submit_times, interval=5, label="machine", bar_width=32)


        for fut in as_completed(futures):
            idx, res = fut.result()
            results_by_index[idx] = res

        #if verbose:
        monitor_thread.join()



    if verbose:
        print(f"Items processed: {len(results_by_index)}")


    if len(results_by_index)>0:
        save_machine_annotations_json(
            results_by_index,
            fyp.cf['paths']['machine_annotations']
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
        verbose = False
    ):
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

    if verbose:
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





def _flatten_one_machine_response(some_response, verbose=False):
    """
    Flattens a machine response into a single level dictionary.
    NOTE: This is directly dependent on the prompt you are using. 
    Changes to the prompt will require changes to this function
    """


    from copy import deepcopy
    from collections import Counter

    # if the response is not a dictionary, something is wrong - return it as is
    if some_response is None or type(some_response) != dict:
        if verbose:
            print(type(some_response))
        return some_response

    flat_response = deepcopy(some_response)

    # check if required keys are present
    required_things = [
        'transcript', 'objects', 'content_category', 'symbols_and_brands',
        'text_overlays', 'faces', 'scenes', 'audio_summary',
    ]
    for rt in required_things:
        if not rt in flat_response.keys():
            if verbose:
                print(f"required key '{rt}' is missing in response. Returning None")
            return None

    # it is expected that there is a sentiment value for each scene. This can be a string or a list of strings
    if 'scene_sentiments' in flat_response.keys():
        if type(flat_response['scene_sentiments']) != str:
            flat_response['scene_sentiments'] = Counter([k.get('sentiment','') for k in flat_response['scene_sentiments']]).most_common(1)[0][0]
    elif 'scenes' in flat_response.keys():
        flat_response['scene_sentiments'] = Counter([k.get('sentiment','') for k in flat_response['scenes']]).most_common(1)[0][0]
    else:
        return None

    if 'scenes' in flat_response.keys():
        if type(flat_response['scenes']) != str:
            flat_response['scenes'] = " | ".join([k.get('description','') for k in flat_response['scenes']])

    if type(flat_response['transcript']) != str:
        flat_response['transcript'] = " | ".join([k if type(k)!=dict else k.get('text','') for k in flat_response['transcript']])

    flat_response['objects'] = " | ".join(flat_response['objects'])
    flat_response['symbols_and_brands'] = " | ".join([s for s in flat_response['symbols_and_brands'] if type(s)==str])
    flat_response['text_overlays'] = " | ".join([s for s in flat_response['text_overlays'] if type(s)==str])
    flat_response['content_category'] = " | ".join([s for s in flat_response['content_category'] if type(s)==str])

    # sometimes audio summary hasn't been converted to json
    # not sure why this happens, this is trying to do something about that
    audio_summary_ok = True
    if isinstance(flat_response['audio_summary'],str):
        try:
            flat_response['audio_summary'] = eval(flat_response['audio_summary'])
        except:
            audio_summary_ok = False
    
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

    if type(flat_response['faces']) != str:
        if type(flat_response['faces']) != list:
            flat_response['faces'] = [flat_response['faces']]
        for face in flat_response['faces']:
            for k in face:
                if not "faces_"+k in flat_response.keys():
                    flat_response["faces_"+k] = ""
                flat_response["faces_"+k] += str(face[k]) + " | "
    del flat_response['faces']

    for k in flat_response:
        if (k.startswith("faces_")) and (flat_response[k].endswith(" | ")):
            flat_response[k] = flat_response[k][:-3]    

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

    
    
def _fuzzy_load_of_json_from_string(resp_text_in: str, testing = False):
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
            if testing:
                print("Fail!!")
                return refined_text
            print(e, "--- Returning 'None'")
            return None
    else:
        return None








def flatten_and_fix_machine_outputs(
        raw_outputs_from_machine, verbose=False
    ):
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

    outputs_from_machine_step_1 = {}
    for h in raw_outputs_from_machine:
        flattened_response = None
        outputs_from_machine_step_1[h] = copy(raw_outputs_from_machine[h])
        if raw_outputs_from_machine[h]['response'] is None or raw_outputs_from_machine[h]['response']=='':
            if verbose:
                print(raw_outputs_from_machine[h]['finish_reason'])
        else:
            if verbose:
                print("fuzzy loading response into json")
            json_response = _fuzzy_load_of_json_from_string(raw_outputs_from_machine[h]['response'])
            if verbose:
                print("flattening json")
            flattened_response = _flatten_one_machine_response(json_response, verbose=verbose)
            if type(flattened_response)==dict:
                for rk in flattened_response:
                    outputs_from_machine_step_1[h][rk] = copy(flattened_response[rk])


    # convert the dict to a DF, reset the index and drop the old response structure 
    outputs_from_machine_df = DataFrame(outputs_from_machine_step_1).T
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
        verbose=False
    ):

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
        print("\nprettifying all strings")
    outputs_from_machine_df = outputs_from_machine_df.map(lambda x:x if not isinstance(x,str) else _prettify_string(x)).copy()

    return outputs_from_machine_df







# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# the highest level functions
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************


def _post_process_raw_annotations(raw_outputs_from_machine, verbose = False):
    from datetime import datetime
    from os.path import join
    from os import environ

    if verbose:
        print("Flattening raw machine annotations")
    outputs_from_machine_df = flatten_and_fix_machine_outputs(raw_outputs_from_machine, verbose=verbose)
    if verbose:
        print("Consolidating rare columns from machine annotations")
    outputs_from_machine_df = consolidate_rare_columns_from_gemini_output(outputs_from_machine_df, verbose=verbose)
    if verbose:
        print("Removing repetitions from machine annotation transcripts")

    outputs_from_machine_df = remove_repetitions_from_transcripts(outputs_from_machine_df, verbose=verbose)
    if verbose:
        print("Ready to save processed results")

    fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
    
    file_prefix = "machine_annotations"
    if environ.get("FYP_TESTING") and environ.get("FYP_TESTING") == "true":
        file_prefix = "machine_annotations_TEST"
        
    outputs_from_machine_df.to_pickle(join(fyp.cf["paths"]["machine_annotations"],f"{file_prefix}_{fine_ts}.pkl"))
    if verbose:
        print(f"Saved processed results to '{file_prefix}_{fine_ts}.pkl'")





def annotate_from_list(fine_list, verbose = False):
    """
    This function takes a list of video IDs and calls the machine to annotate them.
    It also performs the necessary post processing of the raw outputs from the machine.
    """

    from os import environ
    if environ.get("FYP_TESTING") and environ.get("FYP_TESTING") == "true":
        print("This is a test - just pretending")
    else:

        if isinstance(fine_list, list) and len(fine_list) > 0:
            if not all(map(lambda video_id:type(video_id)==int and len(str(video_id))==19, fine_list)):
                if verbose:
                    print("Some videoIDs in the list were corrupt. Cannot process this list.")
                return None

            if verbose:
                print("Annotating videos:")

            raw_outputs_from_machine = call_machine_threads(
                    fine_list,
                    max_workers=32,
                    the_machine_client = fyp.cf["machine"]["client"],
                    the_machine_model = fyp.cf['machine']['model'],
                    the_machine_config = fyp.cf["machine"]["global_generation_config"],
                    verbose = verbose
                )

            _post_process_raw_annotations(raw_outputs_from_machine, verbose=verbose)

        else:
            if verbose:
                print(f"No videos to process")





def annotate_from_scrape_metadata_file(scrape_metadata_filename, verbose = False):
    """
    This is a wrapper that is reading a scrape metadata file and extracts a list of video IDs
    to process. It then calls annotate_from_list.
    """
    from pandas import read_pickle
    from os.path import exists



    if not exists(scrape_metadata_filename):
        if verbose:
            print(f"File {scrape_metadata_filename} does not exist. Cannot process this file.")
        return None

    df = read_pickle(scrape_metadata_filename)

    # we're only annotating the videos that are downloaded and shorter than a certain max duration
    work_with_these_videos_list = df[(df["video_downloaded"]) & (df["video_duration"]<fyp.cf["machine"]["max_duration_for_annotation"])]["item_id"].tolist()

    annotate_from_list(work_with_these_videos_list, verbose = verbose)






def post_process_raw_annotations_from_json_file(json_file, verbose = False):
    """
    This is useful when the post_processing crashes. It's expensive to call the machine so
    it's preferrable to use the raw json and try to fix whatever might be causing the trouble
    """
    from datetime import datetime
    from json import load
    from os.path import exists, join

    if not exists(json_file):
        if verbose:
            print(f"File {json_file} does not exist. Cannot process this file.")
        return None

    with open(json_file, 'r') as f:
        raw_outputs_from_machine = load(f)

    _post_process_raw_annotations(raw_outputs_from_machine, verbose=verbose)









def create_a_new_dataset_just_for_annotating_downloaded_videos(study_name, batch_size = 500):

    from datetime import datetime
    from fyp.organize_datasets_OPTIMIZED import load_datasets, calculate_all_unique_video_subsets, save_selected_unique_video_subsets
    from os import environ
    from os.path import join
    import json

    print(f"Annotating downloaded videos, study '{study_name}', batch size: {batch_size}")
    print(f"Now: {datetime.now()}")
    print("##"*60)

    # --- TEST MODE ---
    if environ.get("FYP_TESTING") and environ.get("FYP_TESTING") == "true":
        print("this thing doesn't run in test mode")
        return
    # -----------------

    print("Building datasets to initiate loop. Might take a minute...\n")
    tutti = load_datasets(
        study_name,
        use_half_baked = True,
        delete_all_half_baked_files = True,
        consolidate = True,
        verbose = False
        )
    first_iteration = True

    print("##"*60)
    print()


    selected_videos = [0] # just a list that contains anything and that is longer than zero elements to get things started

    print("Starting loop...")
    while len(selected_videos)>0:
        if not first_iteration:
            print("##"*60)
            print()

            tutti = load_datasets(
                study_name,
                use_half_baked = True,
                delete_all_half_baked_files = False,
                verbose = False)
        else:
            first_iteration = False

        print("Calculating video subsets...")
        video_subsets = calculate_all_unique_video_subsets(study_name, tutti, verbose = False)

        selected_videos = save_selected_unique_video_subsets(
            study_name,
            tutti,
            video_subsets,
            file_label = "ANNOTATE",
            INCLUDE_UNSEEN_VIDEOS_IN_EXPORT = False,
            INCLUDE_FAILED_SCRAPES_IN_EXPORT = False,
            INCLUDE_SCRAPED_BUT_NOT_DOWNLOADED_IN_EXPORT = False,
            INCLUDE_DOWNLOADED_BUT_NOT_ANNOTATED_IN_EXPORT = True,
            INCLUDE_FAILED_ANNOTATIONS_IN_EXPORT = False,
            INCLUDE_DOWNLOADED_AND_ANNOTATED_IN_EXPORT = False,
            INCLUDE_LONG_VIDEOS_IN_EXPORT = False,
            verbose = True
        )

        if len(selected_videos) > 0:
            work_with_these_videos_list_raw = [int(k) for k in selected_videos.item_id.to_list()]
            work_with_these_videos_list = work_with_these_videos_list_raw.copy()

            print(f"{len(work_with_these_videos_list):,} videos to process for study '{study_name}'")

            _ = annotate_from_list(work_with_these_videos_list[:batch_size], verbose = True)
        
        if selected_videos is None:
            selected_videos = []

    print(f"Loop ended: {datetime.now()}")





# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************


