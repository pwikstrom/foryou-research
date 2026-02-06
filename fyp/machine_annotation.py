#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""

import datetime as _dt
import pandas as pd
import numpy as np
import os
import re
import sys
import shutil
import json
import fuzzy_json

import google.genai

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import time
from random import random
from copy import deepcopy, copy
import collections
 
from fyp.types import convert_dtypes_to_pyarrow
#from fyp.organize_datasets import select_videos_from_study_dataset
from fyp.recode_variables import rename_columns, recode_events_df, recode_fuzzy_match
import fyp.utils as fyp_utils
import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf




REQUIRED_KEYS = [
    "transcript", "objects", "content_category", "symbols_and_brands",
    "text_overlays", "scenes"]










# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# functions in this section call the machine and get the raw responses
# *********************************************************************************************************
# *********************************************************************************************************



def initialize_machine():
    global fyp_cf

    if fyp_cf["machine"].get("client", None) is not None:
        return fyp_cf

    fyp_cf["machine"]["client"] = None

    if fyp_utils.online_ok():
        try:
            with open(fyp_cf['machine']['prompt'], 'r') as file:
                machine_prompt = file.read()

            fyp_cf["machine"]["client"] = google.genai.Client(
                vertexai=fyp_cf["machine"]["vertexai"],
                project=fyp_cf["machine"]["project"],
                location=fyp_cf["machine"]["location"],
                http_options=google.genai.types.HttpOptions(
                    api_version=fyp_cf["machine"]["http_options_api_version"],
                    timeout=fyp_cf["machine"]["http_options_timeout"]
                )
            )

            fyp_cf["machine"]["global_generation_config"] = google.genai.types.GenerateContentConfig(
                system_instruction=machine_prompt,
                temperature=fyp_cf["machine"]["temperature"],
                max_output_tokens=fyp_cf["machine"]["max_output_tokens"],
                response_mime_type=fyp_cf["machine"]["response_mime_type"],
                presence_penalty=fyp_cf["machine"]["presence_penalty"],
                frequency_penalty=fyp_cf["machine"]["frequency_penalty"],
                thinking_config=google.genai.types.ThinkingConfig(thinking_budget=fyp_cf["machine"]["thinking_budget"]),
            )

            print("Google Gemini initialized successfully")
            

        except Exception as e:
            print(f"Error Gemini API key. Gemini won't be available. {e}")
            
    else:
        print("I'm offline. Can't initialize Google Gemini.")
        





def call_machine(
        video_id: str = None, 
        use_local_video_file = False,
        local_path: str = '/Users/<user>/Downloads/',
        verbose = False,
        dry_run = False,
    ) -> dict:


    initialize_machine()


    if dry_run:
        time.sleep(1)
        if verbose:
            print(f"Dry run: would have annotated video {video_id}")
        return {
            "item_id" : video_id,
            "error" : "dry run",
            "finish_reason": "dry run",
            "response" : "dry run",
        }
            


    times = [_dt.datetime.now()]
    output = {
        "item_id" : video_id,
        "inference_ts" : int(times[-1].timestamp()),
        "inference_duration" : -1,
        "model" : fyp_cf['machine']['model'],
        "prompt_fn" : os.path.basename(fyp_cf['machine']['prompt']),
        "error" : "unknown error",
        "finish_reason": "did not even start",
        "response" : "",
    }

    temp_fn = f"temp_machine_annotations_{output['item_id']}_{output['inference_ts']}.json"


    # initialise the contents for the model
    try:
        if use_local_video_file:
            if verbose:
                print(f"Using local video file for video id {video_id}")
            with open(join(local_path,f"{video_id}.mp4"),'rb') as f:
                video_bytes = f.read()
            contents = [
                google.genai.types.Part(
                    inline_data=google.genai.types.Blob(data=video_bytes, 
                    mime_type='video/mp4')
                ),
                google.genai.types.Part.from_text(text="Analyze this video")
            ]
        else:
            contents = [
                google.genai.types.Part.from_uri(
                    file_uri=f"gs://{fyp_cf['data_io']['GCS_bucket_name']}/{fyp_cf['data_io']['gcs_media_prefix']}/{video_id}.mp4",
                    mime_type="video/mp4"
                ),
                google.genai.types.Part.from_text(text="Analyze this video")
            ]
    
    except Exception as e:
        output["error"] = str(e)
        with open(os.path.join(fyp_cf["paths"]["temp"], temp_fn), 'w') as file:
            json.dump(output, file)

        return output


    # run the model
    try:
        start_ts = _dt.datetime.now()
        resp = fyp_cf["machine"]["client"].models.generate_content(
            model = fyp_cf['machine']['model'],
            config = fyp_cf["machine"]["global_generation_config"],
            contents=contents,
        )
    except Exception as e:
        times += [_dt.datetime.now()]

        video_found = fyp_cf["data_io"]["bucket"].blob(f"{fyp_cf['data_io']['gcs_media_prefix']}/{video_id}.mp4").exists()

        output["error"] = str(e)
        output["inference_duration"] = (times[-1] - times[-2]).total_seconds()

        if not video_found:
            output["finish_reason"] = "DNF - file not found in storage"
        else:
            output["finish_reason"] = "DNF - see error msg"

        with open(os.path.join(fyp_cf["paths"]["temp"], temp_fn), 'w') as file:
            json.dump(output, file)
        return output


    try:
        the_finish_reason = str(resp.candidates[0].finish_reason)
    except:
        the_finish_reason = "Finished, but don't know why"
    
    times += [_dt.datetime.now()]

    try:
        machine_annotations = copy(resp.text)
    except Exception as e:
        output["error"] = str(e)
        output["inference_duration"] = (times[-1] - times[-2]).total_seconds()
        output["finish_reason"] = the_finish_reason
        output["response"] = resp

        with open(os.path.join(fyp_cf["paths"]["temp"], temp_fn), 'w') as file:
            json.dump(output, file)
        return output

    output["inference_duration"] = (times[-1] - times[-2]).total_seconds()
    output["finish_reason"] = the_finish_reason
    output["response"] = machine_annotations

    # save the json just in case everything crashes
    with open(os.path.join(fyp_cf["paths"]["temp"], temp_fn), 'w') as file:
        json.dump(output, file)

    return output







def _start_monitor(
    futures, 
    submit_times, 
    interval=5, 
    label="monitor", 
    bar_width=30,
    ):
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
            if "WEB_INTERFACE" in os.environ:
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
        interesting_videos = None,
        max_workers=50,
        verbose=False,
        notebook_mode = False,
        dry_run = False):

    if notebook_mode:
        verbose = True


    initialize_machine()

    results_by_index = {}

    def worker(idx_video):
        idx, video = idx_video

        # Maybe Gemini doesn't like to get to many request at once.
        # Sleeping for a bit with the first ones solves the problem
        if idx < max_workers:
            time.sleep(3+random()*max_workers/2)

        t1 = _dt.datetime.now()
        rr = call_machine(
            video_id = video,
            dry_run = dry_run,
            verbose = verbose,

        )

        return idx, rr


    if verbose:
        if dry_run:
            print("  [dry run] - ", end="", flush=True)
        print(f"Calling {fyp_cf['machine']['model']} to annotate {len(interesting_videos):,} videos with {max_workers} threads.")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:

        futures = []
        submit_times = {}
        for iv in enumerate(interesting_videos):
            fut = ex.submit(worker, iv)
            futures.append(fut)
            submit_times[fut] = time.time()

        monitor_thread = _start_monitor(futures, submit_times, interval=5, label="machine", bar_width=32)


        for fut in as_completed(futures):
            idx, res = fut.result()
            results_by_index[idx] = res

        monitor_thread.join()


    if verbose:
        print(f"Items processed: {len(results_by_index)}")


    if len(results_by_index)>0 and not dry_run:

        fine_ts = "".join([k for k in str(_dt.datetime.now()) if k in "0123456789"])

        filename = f"machine_annotations_{fine_ts}.json"

        data_io.save_json(data=results_by_index, storage_location="machine_annotations_raw", filename=filename, verbose=verbose)
        if verbose:
            print(f"Saved raw machine annotations to '{filename}'")



    return results_by_index, filename






# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# I'm not using structured outputs because I understand that the machine is calling all the required bits
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
                flat_response['scenes'] = fuzzy_json.loads(flat_response['scenes'])
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
                tt1 = collections.Counter(sentiment_list).most_common(1)
                if len(tt1) == 0:
                    flat_response['scene_sentiments'] = ""
                else:
                    flat_response['scene_sentiments'] = tt1[0][0]
            except Exception as e:
                return None
        else:
            return None

    # #######################
    # transcript
    if 'transcript' in flat_response.keys():
        if isinstance(flat_response['transcript'], str):
            flat_response['transcript'] = re.sub(r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response['transcript'])
            try:
                flat_response['transcript'] = fuzzy_json.loads(flat_response['transcript'])
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
                    flat_response[res_key] = fuzzy_json.loads(flat_response[res_key])
                except Exception as e:
                    if verbose:
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
                flat_response['audio_summary'] = fuzzy_json.loads(flat_response['audio_summary'])
            except Exception as e:
                if verbose:
                    print(flat_response['audio_summary'])
                return None
        
        for k in flat_response['audio_summary']:
            try:
                audio_detail = flat_response['audio_summary'][k]
            except Exception as e:
                if verbose:
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
                flat_response['faces'] = fuzzy_json.loads(flat_response['faces'])
            except Exception as e:
                if verbose:
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
            if verbose:
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
            
            machine_annotations = fuzzy_json.loads(refined_text)
            
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


    bad_count = 0
    good_count = 0

    flattened_outputs_from_machine = {}
    for h in raw_outputs_from_machine:
        flattened_response = None
        flattened_outputs_from_machine[h] = copy(raw_outputs_from_machine[h])
        if raw_outputs_from_machine[h]['response'] is None or raw_outputs_from_machine[h]['response']=='':
            bad_count += 1
            print("!", end="", flush=True)
        else:
            json_response = fuzzy_load_of_json_from_string(raw_outputs_from_machine[h]['response'], notebook_mode = notebook_mode)
            flattened_response = flatten_one_machine_response(json_response, verbose = False, notebook_mode = notebook_mode)
            if type(flattened_response)==dict:
                good_count += 1
                print(".", end="", flush=True)
                for rk in flattened_response:
                    flattened_outputs_from_machine[h][rk] = copy(flattened_response[rk])
            else:
                bad_count += 1
                print("X", end="", flush=True)
                if notebook_mode:
                    print("Error when postprocessing response -> bad response")
                    print(raw_outputs_from_machine[h])
        if (good_count + bad_count) % 100 == 0:
            print()

    if (good_count + bad_count) % 100 != 0:
        print()

    print(f"...extracted {good_count} good responses from the file. Unable to use {bad_count} responses.")

    if good_count == 0:
        return None

    # convert the dict to a DF, reset the index and drop the old response structure 
    outputs_from_machine_df = pd.DataFrame(flattened_outputs_from_machine).T
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


    if not isinstance(text,str):
        return "Not a string"

    if len(text) > max_text_length:
        return "String too long"

    words = text.split()
    n = len(words)
    
    pattern_counts = collections.defaultdict(int)
    
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












def clean_up_machine_annotations(some_events, verbose = False):
    



    some_cleaned_up_events = some_events.copy()

    # iterate over all object type columns in the events DF that starts w G_, i.e. are machine annotations
    g_cols = [k for k in some_events.select_dtypes(exclude=["number"]).columns if k.startswith("G_")]
    
    exclude_set = {"DDP", "BASELINE", fyp_cf['labels']['UNABLE_TO_DETECT'], "", fyp_cf['labels']['OTHER_THINGS']}

    for c in g_cols:
        # Step 1: Flatten and filter efficiently
        series = some_events[c]
        
        # explode lists to rows
        exploded = series.explode().dropna()
        
        if exploded.empty:
            continue


        # exclude set filtering
        # check against set is fast
        valid_mask = ~exploded.isin(exclude_set)
        valid_items = exploded[valid_mask]
        
        if valid_items.empty:
            continue

        accepted = fyp_cf['var_schema'].set_index('variable_name').loc[c,'accepted_labels']
        accepted_labels = pd.NA
        if pd.notna(accepted) and accepted.lower() != 'nan' and accepted.startswith('[') and accepted.endswith(']'):
            accepted = accepted[1:-1]
            accepted_labels = [x.strip().replace("//", "").replace("&", " and ").replace("/", " or ") for x in accepted.split(',')]

            pre_fuzzy_nunique = valid_items.nunique()

            valid_items = recode_fuzzy_match(
                list_a=valid_items, 
                list_b=accepted_labels, 
                threshold=0.8, 
                verbose=verbose
            )

            if verbose:
                print(f"    {c}: Recoded against accepted labels with fuzzy matching... {valid_items.nunique()} ({pre_fuzzy_nunique})")



        # Check mean length
        # Vectorized string length based on a sample of 500 items

        sample_size = min(500, len(valid_items))
        avg_len = valid_items.sample(sample_size, replace = False).astype(str).str.len().mean()
        
        if avg_len < 60:
            # Step 2: Cutoff logic
            # frequency of unique valid items
            counts = valid_items.value_counts()

            total_count = counts.sum()

            # if we have an accepted list, we want to keep all of them
            if pd.notna(accepted):
                target = total_count * 1
            else:
                target = total_count * 0.95
            
            # cumulative sum
            cum_counts = counts.cumsum()
            
            # find how many labels needed to cross target
            # we keep labels where cumsum < target, plus the one that crosses it
            cutoff_idx = cum_counts.searchsorted(target)
            # ensure at least 3 if possible?
            num_keep = max(3, cutoff_idx + 1)
            # clamp to length
            num_keep = min(num_keep, len(counts))



            # Heuristic: If we are keeping a huge portion of the labels to satisfy the coverage, 
            # or the absolute number of kept labels is huge (e.g. 90k out of 100k), then consolidation is inefficient/useless.
            # User guideline: "if the sum of occurrences of top X labels constitute more than y% ... and there still are a lot of small labels" -> consolidate.
            # But "100k rare labels -> 90k" -> don't consolidate.
            # Logic: If num_keep is > 80% of len(counts) and len(counts) > 1000, skip.
            
            if (len(counts) > 1000) and (num_keep > len(counts) * 0.80):
                 if verbose:
                     print(f"    {c}: Skipping consolidation. Tail is too thick/flat (would keep {num_keep}/{len(counts)}).")
                 continue

            
            okay_list = counts.index[:num_keep].tolist()
            
            # fast lookup set
            keep_set = set(okay_list).union(exclude_set)

            # Step 3: Replacement
            # We need to iterate rows since we want to preserve list structure [[a, b], [c]] -> [[a, OTHER], [c]]
            # A simple map with set lookup is fastest for object columns with lists
            def _fast_replace(x):
                if isinstance(x, (list, np.ndarray)):
                    return [y if y in keep_set else fyp_cf['labels']['OTHER_THINGS'] for y in x]
                if isinstance(x, str):
                    return x if x in keep_set else fyp_cf['labels']['OTHER_THINGS']
                return x # keep NA or other
                
            some_cleaned_up_events[c] = series.apply(_fast_replace)


            if verbose:
                # approximated stats
                print(f"    {c}: Cleaned up rare labels (kept top {num_keep})")

        else:
            if verbose:
                print(f"    {c}: Avg string length > 60, not consolidating rare labels")
        




    return some_cleaned_up_events








# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# the highest level functions
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************


def refine_one_raw_annotation_batch(
    raw_outputs_from_machine = None,
    raw_json_filename = None,
    verbose = False,
    notebook_mode = False):

    if notebook_mode:
        verbose = True


    if raw_json_filename is None:
        raise ValueError("raw_json_filename cannot be None")



    if raw_outputs_from_machine is None:
        if verbose:
            print(f"Loading raw annotations from {raw_json_filename}")
        raw_outputs_from_machine = data_io.load_json(
            storage_location="machine_annotations_raw",
            filename=raw_json_filename,
            verbose=verbose
        )

    if raw_outputs_from_machine is None:
        raise ValueError("raw_outputs_from_machine cannot be None")


    print(f"Refining {len(raw_outputs_from_machine):,} raw annotations in this file...")

    # ---------------------------------------------------------------
    # 1. Flatten the json to a dataframe. Using fuzzy json for this
    # ---------------------------------------------------------------
    print("Transforming the messy json into a flat dataframe")
    outputs_from_machine_df = flatten_and_fix_machine_outputs(raw_outputs_from_machine, verbose = verbose, notebook_mode = notebook_mode)

    if outputs_from_machine_df is None:
        print("I was unable to extract a single good response from this file. Returning None.")
        print("Consider deleting this raw file from the raw_annotations folder.")
        return None

    # ---------------------------------------------------------------
    # 2. Check if required keys are present
    # ---------------------------------------------------------------
    found_all_required_keys = True
    for rk in REQUIRED_KEYS:
        if not rk in outputs_from_machine_df.columns:
            print(f"WARNING: Essential column '{rk}' is missing in machine output")
            found_all_required_keys = False

    # ---------------------------------------------------------------
    # 3. Consolidate rare columns
    # ---------------------------------------------------------------
    print("Consolidating rare columns from machine annotations.")
    outputs_from_machine_df = consolidate_rare_columns_from_gemini_output(outputs_from_machine_df, verbose = verbose, notebook_mode = notebook_mode)
    print("...done")

    # ---------------------------------------------------------------
    # 4. Remove repetitions from transcripts
    # ---------------------------------------------------------------
    if 'transcript' in outputs_from_machine_df.columns:
        print("Removing repetitions from machine annotation transcripts...")
        outputs_from_machine_df = remove_repetitions_from_transcripts(outputs_from_machine_df, verbose = verbose, notebook_mode = notebook_mode)
        print("...done")

    

    # ---------------------------------------------------------------
    # implement the rules from the variable scheme - recoding lists, strings and other complex data
    # ---------------------------------------------------------------
    # (and a simple renaming of columns to make them easier to identify and read)
    outputs_from_machine_df = rename_columns(outputs_from_machine_df.rename(columns={c:"G_"+c if not c=="item_id" and not c.startswith("G_") else c for c in outputs_from_machine_df.columns})).copy()
    outputs_from_machine_df = recode_events_df(
            study_dataset = outputs_from_machine_df,
            drop_single_value_cols = False,
            verbose = verbose
            )



    # ---------------------------------------------------------------
    # consolidate some labels in non-numeric columns where that makes sense 
    # ---------------------------------------------------------------
    outputs_from_machine_df = clean_up_machine_annotations(some_events=outputs_from_machine_df, verbose=verbose)




    # ---------------------------------------------------------------
    # add flags for annotated ok and fail
    # ---------------------------------------------------------------
    outputs_from_machine_df["annotated_ok"] = ~outputs_from_machine_df.G_type_of_story.isna().astype("bool[pyarrow]")
    outputs_from_machine_df["annotated_fail"] = outputs_from_machine_df.G_type_of_story.isna().astype("bool[pyarrow]")
    outputs_from_machine_df.loc[outputs_from_machine_df[outputs_from_machine_df.annotated_fail].index,[c for c in outputs_from_machine_df.columns if c.startswith("G_")]] = pd.NA


    # ---------------------------------------------------------------
    # Convert dtypes to pyarrow and reset index
    # ---------------------------------------------------------------
    outputs_from_machine_df.reset_index(drop=True, inplace=True)
    outputs_from_machine_df = convert_dtypes_to_pyarrow(outputs_from_machine_df, verbose=verbose)


    if verbose:
        print("Ready to save processed results")

    parquet_filename = raw_json_filename.replace(".json", ".parquet")

    data_io.save_parquet(
        df = outputs_from_machine_df,
        storage_location="machine_annotations_refined",
        filename=parquet_filename,
        verbose=verbose
    )
    print(f"Saved processed the df - shape {outputs_from_machine_df.shape} - results to '{parquet_filename}'")
    print("--"*60)
    
    return outputs_from_machine_df
    




def refine_and_save_all_raw_annotation_files(verbose = False, notebook_mode = False):

    result = {}
    
    raw_annotation_files = [fn for fn in data_io.listdir(storage_location="machine_annotations_raw") if fn.startswith("machine_annotations") and fn.endswith(".json")]
    result["raw_files"] = len(raw_annotation_files)

    refined_annotation_files = [fn for fn in data_io.listdir(storage_location="machine_annotations_refined") if fn.startswith("machine_annotations") and fn.endswith(".parquet")]
    result["refined_files_before"] = len(refined_annotation_files)

    raw_files_up_for_refinement = [g for g in raw_annotation_files if not g.replace(".json",".parquet") in refined_annotation_files]
    if verbose:
        print(f"{len(refined_annotation_files)} raw annotation files have already been refined")
        print(f"{len(raw_files_up_for_refinement)} files are up for refinement")
    
    for i,fn in enumerate(raw_files_up_for_refinement):
        if verbose:
            print(f"\n{i+1}/{len(raw_files_up_for_refinement)} {fn}")
        refine_one_raw_annotation_batch(
            raw_outputs_from_machine = None,
            raw_json_filename = fn,
            verbose = verbose,
            notebook_mode = notebook_mode
            )

    refined_annotation_files = data_io.listdir(
        storage_location="machine_annotations_refined",
        return_absolute_path=False,
        verbose=False)
    refined_annotation_files = [u for u in refined_annotation_files if u.endswith(".parquet")]
    result["refined_files_after"] = len(refined_annotation_files)

    return result










def consolidate_and_save_refined_annotations(
    force_consolidation = False,
    return_saved_data = True,
    verbose = False,
    ):


    top_verbose = True

    # ---------------------------------------------------------------
    if top_verbose:
        print("Checking for raw annotation batches that needs refining...")
    # check if there are any raw files that need refining and refine those
    result = refine_and_save_all_raw_annotation_files(verbose = verbose, notebook_mode = False)
    if top_verbose:
        if result["refined_files_after"] == result["refined_files_before"]:
            print("    ...all files already refined.")
        else:
            print(f"    ...refined {result["refined_files_after"] - result["refined_files_before"]} files.")


    # ---------------------------------------------------------------
    # check if there are any changes in the relevant folder compared to last time this process was run.    
    if data_io.exists(storage_location="recoded",filename="dataset_meta.json",verbose=verbose):
        dataset_meta = data_io.load_json(storage_location="recoded",filename="dataset_meta.json",verbose=verbose)
        if verbose:
            print("Dataset meta loaded")
    else:
        dataset_meta = {"machine_annotations": {"filenames": []}}

    files_to_concatenate = []
    for fn in data_io.listdir(storage_location="machine_annotations_refined"):
        if fn.startswith("machine_annotations_") and fn.endswith(".parquet"):
            files_to_concatenate.append(fn)

    latest_filename_list = dataset_meta.get("machine_annotations", {}).get("filenames", [])

    # if all files found in the refine folder are already registered in the dataset meta, then no need to consolidate
    if not force_consolidation and set(files_to_concatenate) <= set(latest_filename_list):
        if top_verbose:
            print("No new refined machine annotations files found. No need to consolidate.")
            if return_saved_data:
                if verbose: print("Returning existing file.")
                return False, data_io.load_parquet(storage_location="recoded", filename="machine_annotations_recoded.parquet")
        return False, None
    
 
    # ---------------------------------------------------------------
    # load all refined files
    if top_verbose:
        print("Loading refined annotation files...")
    refined_annotation_dfs = []
    for fn in files_to_concatenate:
        df = data_io.load_parquet(storage_location="machine_annotations_refined", filename=fn)
        refined_annotation_dfs.append(df)
        if verbose:
            print(fn, df.shape)

    
    # ---------------------------------------------------------------
    if top_verbose:
        print(f"Consolidating {len(refined_annotation_dfs):,} refined files (keeping latest version of each item_id)...")
    consolidated_annotations = pd.concat(refined_annotation_dfs, ignore_index=True)

    # ---------------------------------------------------------------
    # dropping duplicates, only keeping the most recent annotation for each item_id
    consolidated_annotations = consolidated_annotations.drop_duplicates(subset=["item_id"], keep="last").reset_index(drop=True)

    memory_per_column = consolidated_annotations.memory_usage(deep=True) 
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    if top_verbose:
        print(f"Shape: {consolidated_annotations.shape} | Memory usage: {total_memory_mb:.2f} MB")

    # ---------------------------------------------------------------
    # save the consolidated annotations
    if top_verbose:
        print("Saving consolidated annotations...")
    data_io.save_parquet(
        df=consolidated_annotations, 
        storage_location="recoded", filename="machine_annotations_recoded.parquet", verbose=verbose)   
    if top_verbose:
        print("...done")

    # ---------------------------------------------------------------
    # update the dataset meta file
    if not "machine_annotations" in dataset_meta:
        dataset_meta["machine_annotations"] = {}
    dataset_meta["machine_annotations"]["filenames"] = files_to_concatenate
    _ = data_io.save_json(data = dataset_meta, storage_location="recoded", filename="dataset_meta.json")

    return True, consolidated_annotations






def annotate_from_video_id_list(
    fine_list = None,
    max_workers = 50,
    refine_after_annotation = True,
    verbose = False,
    notebook_mode = False,
    dry_run = False):

    if notebook_mode:
        verbose = True
    """
    This function takes a list of video IDs and calls the machine to annotate them.
    It also performs the necessary post processing of the raw outputs from the machine.
    """

    initialize_machine()


    if dry_run:
        print("********* This is a dry run. It's all fake. No data io action at all. *********")


    if isinstance(fine_list, list) and len(fine_list) > 0:

        if not all(map(lambda video_id:type(video_id)==str and video_id.isnumeric() and len(video_id)==19, fine_list)):
            raise ValueError("Some videoIDs in the list were corrupt. Cannot process this list.")

        print("Annotating videos...")

        raw_outputs_from_machine, raw_json_fn = call_machine_threads(
                interesting_videos = fine_list,
                max_workers=max_workers,
                verbose = verbose, 
                notebook_mode = notebook_mode,
                dry_run = dry_run
            )

        print("...video annotation completed.")

        if dry_run:
            print("Since this is a dry run I'm skipping the refinement step.")
            return None

        if refine_after_annotation:
            _ = refine_one_raw_annotation_batch(
                raw_outputs_from_machine = raw_outputs_from_machine,
                raw_json_filename = raw_json_fn,
                verbose = verbose, notebook_mode = notebook_mode)
        

    else:
        if verbose:
            print(f"No videos to process")








def annotate_from_scrape_data_file(
    scrape_data_filename = None,
    verbose = False,
    notebook_mode = False):

    if notebook_mode:
        verbose = True
    """
    This is a wrapper that is reading a scrape metadata file and extracts a list of video IDs
    to process. It then calls annotate_from_video_id_list.
    """

    initialize_machine()


    if scrape_data_filename is None or not data_io.exists(storage_location="scrape", filename=scrape_data_filename):
        if verbose:
            print(f"File {scrape_data_filename} does not exist. Cannot process this file.")
        return None

    df = data_io.load_parquet(storage_location="scrape", filename=scrape_data_filename, columns=["item_id", "video_downloaded", "video_duration"], verbose=verbose)


    # we're only annotating the videos that are downloaded and shorter than a certain max duration
    work_with_these_videos_list = df[(df["video_downloaded"]) & (df["video_duration"] < fyp_cf["machine"]["max_duration_for_annotation"])]["item_id"].tolist()

    annotate_from_video_id_list(
        fine_list = work_with_these_videos_list,
        verbose = verbose, 
        notebook_mode = notebook_mode
        )










def annotate_videos_loop_from_list(
    video_list = None,
    batch_size = 500,
    max_batches = None,
    verbose = False,
    dry_run = False
    ):



    max_batches = max_batches if max_batches is not None else np.inf

    if video_list is None:
        print("    ERROR: The annotation loop cannot run without a video list as input. Process failed.")
        return None

    initialize_machine()
    


    print(f"    Annotating selected videos, batch size: {batch_size}, max batches: {max_batches}")
    print(f"    Now: {_dt.datetime.now()}")

    batch_number = 1

    batch_target = min(max_batches, len(video_list) // batch_size + 1)

    print(f"  Starting loop... There are {len(video_list):,} videos to process in {batch_target:,} batches")

    for batch in fyp_utils.chunk_list(video_list, batch_size):

        print(f"  Batch {batch_number} of {max_batches:,}")

        _ = annotate_from_video_id_list(
            fine_list = batch,
            verbose = verbose,
            dry_run = dry_run
        )

        if max_batches is not None and batch_number >= max_batches:
            break

        batch_number += 1

        if dry_run:
            break

    print(f"Loop ended: {_dt.datetime.now()}")









def annotate_videos_loop(
    study_name = None,
    study_dataset = None,
    load_from_cache = True,
    batch_size = 500,
    max_batches = None,
    verbose = False,
    notebook_mode = False,
    dry_run = False
    ):

    if notebook_mode:
        verbose = True


    max_batches = max_batches if max_batches is not None else np.inf

    if study_name is None and study_dataset is None:
        print("    ERROR: The annotation loop cannot run without a study name or a study dataset as input. Process failed.")
        return None


    initialize_machine()


    if study_name is not None:
        if load_from_cache and data_io.exists(storage_location = "cache", filename = f"{study_name}_recoded.parquet"):
            if verbose:
                print("    Loading study dataset from cache", end=" ", flush=True)
            study_dataset = data_io.load_parquet(storage_location="cache", filename=f"{study_name}_recoded.parquet", verbose=verbose)
            #print(study_dataset.attrs['study_name'])
            if verbose:
                print(f" | Shape: {study_dataset.shape}")
        else:
            print("@@  No cached recoded study dataset found. I must run the process to create it. Please wait a moment...")
            study_dataset = create_study_recoded_dataset(
                study_name = study_name,
                load_from_cache = True,
                save_to_cache = True,
                verbose = verbose
            )
            print("@@  I'm back after creating the recoded study dataset. I'm now resuming the annotation loop.")

    if study_dataset is None:
        print("    ERROR: This process cannot run without a study dataset. Process failed.")
        return None


    print(f"    Annotating downloaded videos, study '{study_name}', batch size: {batch_size}, max batches: {max_batches}")
    print(f"    Now: {_dt.datetime.now()}")

    selected_videos_df = select_videos_from_study_dataset(
        study_dataset = study_dataset,
        query_string = "scraped_ok & ~annotated_ok & ~annotated_fail & duration_ok_to_annotate",
        verbose = verbose,
        notebook_mode = False
    )

    batch_number = 1

    batch_target = min(max_batches, len(selected_videos_df.index) // batch_size + 1)

    print(f"  Starting loop... There are {len(selected_videos_df):,} videos to process in {batch_target:,} batches")

    for batch in fyp_utils.chunk_list(selected_videos_df.index.to_list(), batch_size):

        print(f"  Batch {batch_number} of {max_batches:,}")

        _ = annotate_from_video_id_list(
            fine_list = batch,
            verbose = verbose,
            notebook_mode = notebook_mode,
            dry_run = dry_run
            )


        if max_batches is not None and batch_number >= max_batches:
            break

        batch_number += 1

        if dry_run:
            break



    print(f"Loop ended: {_dt.datetime.now()}")






# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************


