# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""



############################################################################################################
###                     Initialize project
############################################################################################################

def create_dirs(this_cf: dict, clear_temp_dir: bool = False) -> None:
    from os import makedirs
    from os.path import join
    from os import listdir, remove


    for k in ["main", "zeeschuimer_raw", "zeeschuimer_refined", "ddp", "temp", "backup", "scrape", "exports"]:
        makedirs(this_cf["paths"][k], exist_ok=True)

    if clear_temp_dir:
        for fn in listdir(temp_path()):
            remove(join(temp_path(),fn))





def init_config(verbose=False, abs_project_root_path=None) -> dict:
    from os import environ
    from os.path import join, abspath
    import toml

    if abs_project_root_path is None:

        from os import getcwd
        from os.path import exists
        from sys import path as sys_path

        here = getcwd().split("/")
        while not exists(join("/".join(here),"__proj__.py")):
            here.pop()

        # this is the root folder for the project structure
        abs_project_root_path = join("/".join(here))
        print("Project root:",abs_project_root_path)

        # add project root path to PATH since the modules are located in the project structure
        sys_path.append(abs_project_root_path)


    where_to_start = toml.load(join(abs_project_root_path,"config","core.toml"))
    config_path = join(abs_project_root_path,"config",where_to_start["core"]["config_fn"])
    study_defs_path = join(abs_project_root_path,"config",where_to_start["core"]["study_defs_fn"])
    

    cf = toml.load(config_path)
    # Prefer env var for secrets; fall back to file if present (avoid committing real keys)
    gcp_bucket_name = environ.get("FYP_GCP_BUCKET_NAME")
    if gcp_bucket_name:
        cf["media_storage"]["GCP_bucket"] = gcp_bucket_name

    # Prefer env var for secrets; fall back to file if present (avoid committing real keys)
    gemini_env_key = environ.get("GEMINI_API_KEY")
    if gemini_env_key:
        cf["machine"]["key"] = gemini_env_key

    study_defs = toml.load(study_defs_path)
    for study_name in study_defs.keys():
        study_defs[study_name]["STUDY_NAME"] = study_name
    cf["study_defs"] = study_defs

    cf["paths"]["project_root"] = abs_project_root_path
    if cf["misc"]["local_mode"]:
        cf["machine"]["client"] = None
        cf['machine']['model'] = None
        cf["machine"]["global_generation_config"] = None

    cf["paths"]["main"] = abspath(join(abs_project_root_path, cf["paths"]["main"]))
    cf["paths"]["main_no_sync"] = abspath(join(abs_project_root_path, cf["paths"]["main_not_gdrive_synced"]))

    for p in cf["machine"].keys():
        if "prompt" in p:
            cf["machine"][p] = join(cf["paths"]["project_root"],"prompts",cf["machine"][p])


    if verbose:
        print(f"Initialising with main data directory: {cf['paths']['main']}")

    # paths to folders
    cf["paths"]["temp"] = join(cf["paths"]["main_no_sync"], "temp")
    cf["paths"]["backup"] = join(cf["paths"]["main"], "backup")
    cf["paths"]["ddp"] = join(cf["paths"]["main"],"activity_data", "participant_logs")
    cf["paths"]["zeeschuimer_raw"] = join(cf["paths"]["main"],"activity_data", "zeeschuimer_raw")
    cf["paths"]["zeeschuimer_refined"] = join(cf["paths"]["main"],"activity_data", "zeeschuimer_refined")
    cf["paths"]["scrape"] = join(cf["paths"]["main"], "scrape")
    cf["paths"]["ddp_raw"] = join(cf["paths"]["ddp"], "raw")
    cf["paths"]["ddp_processed"] = join(cf["paths"]["ddp"], "processed")
    cf["paths"]["ddp_main"] = join(cf["paths"]["ddp"], "main")
    cf["paths"]["ddp_participants"] = join(cf["paths"]["ddp"], "main", "participants_raw")
    cf["paths"]["machine_annotations"] = join(cf["paths"]["main"], "machine_annotations", "new_gen")
    cf["paths"]["exports"] = join(cf["paths"]["main"], "exports")


    return cf





def init_project(clear_temp_dir=False, verbose=False, local_mode=False) -> dict:

    from os import getcwd
    from os.path import join, exists
    from sys import path as sys_path
    from sys import exit as sys_exit
    from google import genai
    from google.genai import types
    from google.api_core.exceptions import Forbidden
    from google.cloud import storage
    import http.client as httplib


    # function to check internet connectivity
    def _checkInternetHttplib(url="www.qut.edu.au",
                            timeout=3):
        connection = httplib.HTTPConnection(url,
                                            timeout=timeout)
        try:
            # only header requested for fast operation
            connection.request("HEAD", "/")
            connection.close()  # connection closed
            print("Internet On")
            return True
        except Exception as exep:
            print(exep)
            return False



    here = getcwd().split("/")
    while not exists(join("/".join(here),"__proj__.py")):
        here.pop()

    # this is the root folder for the project structure
    abs_project_root_path = join("/".join(here))
    print("Project root:",abs_project_root_path)

    # add project root path to PATH since the modules are located in the project structure
    sys_path.append(abs_project_root_path)

    cf = init_config(verbose=verbose, abs_project_root_path=abs_project_root_path)

    local_mode = cf["misc"]["local_mode"]
    if not _checkInternetHttplib():
        local_mode = True
        cf["machine"]["client"] = None
        cf['machine']['model'] = None
        cf["machine"]["global_generation_config"] = None


    create_dirs(cf, clear_temp_dir)

    if local_mode:
        print("Local mode - no access to GCP bucket and not initializing Gemini")
        
    else:

        try:
            with open(cf['machine']['new_prompt'], 'r') as file:
                machine_new_prompt = file.read()

            cf["machine"]["client"] = genai.Client(
                vertexai=cf["machine"]["vertexai"],
                project=cf["machine"]["project"],
                location=cf["machine"]["location"],
                http_options=types.HttpOptions(
                    api_version=cf["machine"]["http_options_api_version"],
                    timeout=cf["machine"]["http_options_timeout"]
                )
            )

            cf["machine"]["global_generation_config"] = types.GenerateContentConfig(
                system_instruction=machine_new_prompt,
                temperature=cf["machine"]["temperature"],
                max_output_tokens=cf["machine"]["max_output_tokens"],
                response_mime_type=cf["machine"]["response_mime_type"],
                presence_penalty=cf["machine"]["presence_penalty"],
                frequency_penalty=cf["machine"]["frequency_penalty"],
                thinking_config=types.ThinkingConfig(thinking_budget=cf["machine"]["thinking_budget"]),
            )

            print("Gemini API, model and prompts initiated successfully")

        except:
            print("Error Gemini API key. Gemini won't be available.")



        # Initialize a GCP storage client
        try:
            bucket_client = storage.Client()

            # Get the GCP bucket
            bucket = bucket_client.get_bucket(cf["media_storage"]["GCP_bucket"])

            # Try to access the GCP bucket's metadata
            bucket.reload()
            cf["media_storage"]["bucket"] = bucket
            print(f"Access to the GCP bucket '{bucket.name}' is authorized.")
        except Forbidden:
            print(f"You don't have access to the GCP bucket '{bucket_name}'.")
        except Exception as e:
            print(f"A GCP error occurred: {e}")

    return cf
        





############################################################################################################
############################################################################################################
############################################################################################################
############################################################################################################
############################################################################################################
############################################################################################################
############################################################################################################

cf = init_project(verbose = True)

############################################################################################################
############################################################################################################
############################################################################################################
############################################################################################################
############################################################################################################
############################################################################################################
############################################################################################################









############################################################################################################
###                     File, directory mgmt
############################################################################################################




def temp_path(filename: str = "") -> str:
    import toml
    from os.path import join

    #cf = toml.load(CONFIG_PATH)
    temp_dir = join(cf["paths"]["main_not_gdrive_synced"],"temp")
    return join(temp_dir, filename)







def OLDOLD_back_this_up(the_file: str, move_the_file: bool = False) -> None:
    from os.path import join, exists, basename
    from datetime import datetime
    from shutil import copy, move

    #cf = init_config()

    if exists(the_file) == False:
        return

    nice_now = datetime.now().strftime("%Y%m%d_%H%M%S")

    backup_file = join(cf["paths"]["backup"],"backup_"+nice_now+"_"+basename(the_file))

    if move_the_file:
        print(f"Backing up (moving) {basename(the_file)}")
        move(the_file, backup_file)
    else:
        print(f"Backing up (copying) {basename(the_file)}")
        copy(the_file, backup_file)







############################################################################################################
###                     Utilities
############################################################################################################

def check_repetitive_patterns(text: str, min_pattern_length: int = 5, min_repetitions: int = 5, max_text_length: int = 1000) -> str:
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




def get_recent_files(directory, suffix=None, how_recent=10):
    from os import listdir
    from os.path import isfile, join, getmtime, getctime
    from datetime import datetime, timedelta

    current_time = datetime.now()
    recent_files = []

    for filename in listdir(directory):
        file_path = join(directory, filename)
        if isfile(file_path) and (suffix is None or file_path.endswith(suffix)):
            modified_time = datetime.fromtimestamp(getmtime(file_path))
            created_time = datetime.fromtimestamp(getctime(file_path))
            time_difference = current_time - max(modified_time, created_time)
            if time_difference < timedelta(minutes=how_recent):
                recent_files.append({"filename":file_path, "mtime":modified_time, "ctime":created_time})

    return sorted(recent_files,key=lambda x: x["mtime"], reverse=True)



def pretty_str_seconds(proc_time_seconds: float) -> str:
    minutes, seconds = divmod(proc_time_seconds, 60)
    out = ""
    if minutes > 0:
        out += f"{minutes:.0f}m"
    if seconds > 0:
        if minutes > 0:
            out += " and "
        out += f"{seconds:.0f}s"
    return out



def OLDOLD_get_item_id_from_video_uri(video_uri):
    if video_uri[-1] == "/":
        video_uri = video_uri[:-1]
    return video_uri.split("/")[-1]





def extract_and_join_subkeys(data, sub_keys: list):
    """
    Process a list of dictionaries or a single value, extracting and joining specified sub-keys.

    Args:
    data (list or any): The input data to process. If it's a list, each item is expected to be a dictionary.
    sub_keys (list): A list of keys to extract from each dictionary in the list.

    Returns:
    str or numpy.nan: A string of concatenated values from the specified sub-keys, 
                      or numpy.nan if the input is not a list or is empty.

    Description:
    This function extracts and concatenates values from specific keys in a list of dictionaries.
    If the input is not a list or is empty, it returns numpy.nan.
    For each dictionary in the list, it extracts the values of the specified sub-keys,
    joins them with "__", and then joins all these combined values with " | ".

    Example:
    >>> data = [
    ...     {"id": 1, "name": "John", "age": 30},
    ...     {"id": 2, "name": "Jane", "age": 25},
    ...     {"id": 3, "name": "Bob", "age": 35}
    ... ]
    >>> sub_keys = ["name", "age"]
    >>> result = extract_and_join_subkeys(data, sub_keys)
    >>> print(result)
    'John__30 | Jane__25 | Bob__35'
    """
    from numpy import nan as np_nan
    joined_values = []
    if isinstance(data, list) and len(sub_keys) > 0:
        for item in data:
            if isinstance(item, dict):
                subkey_values = []
                for sk in sub_keys:
                    if sk in item:
                        subkey_values.append(str(item[sk]))
                joined_values.append("__".join(subkey_values))
        return " | ".join(joined_values)
    else:
        return np_nan






def clean_url(the_url: str) -> dict:
    from urllib.parse import unquote
    outout = {}
    for u in the_url.split("?")[1].split("&"):
        v = u.split("=")
        v[1] = unquote(v[1]).replace(",","|")
        try:
            v1 = int(v1)
        except:
            pass
        outout.update({"source_url."+v[0]:v[1]})
    return outout



def OLDOLD_get_video_id_from_link(link):
    if isinstance(link, str):
        if link.endswith("/"):
            link = link[:-1]
        link_split = link.split("/")[-1]
        if len(link_split) != 19:
            return None
        else:
            return int(link_split)
    else:
        return None






def OLDOLD_boxplots_w_max_range(persona_distribution_stats, m_min, m_max):
    import pandas as pd
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    # Select only numeric rows (exclude lists/arrays)
    numeric_stats = persona_distribution_stats[~persona_distribution_stats['mean'].apply(lambda x: isinstance(x, list))].copy()
    numeric_stats = numeric_stats.drop(["most_freq_emoji"])
    numeric_stats = numeric_stats[(numeric_stats['max']>m_min) & (numeric_stats['max']<m_max)].copy()

    # Convert all columns to float
    for col in ['mean', 'q25', 'q75', 'median', 'min', 'max']:
        numeric_stats[col] = pd.to_numeric(numeric_stats[col], errors='coerce')

    # Prepare data for boxplot
    box_data = []
    labels = []
    for idx, row in numeric_stats.iterrows():
        box_data.append([row['min'], row['q25'], row['median'], row['q75'], row['max']])
        labels.append(idx)

    fig, ax = plt.subplots(figsize=(10, max(1 + len(numeric_stats) // 2,2)))
    bp = ax.boxplot(box_data, vert=False, tick_labels=labels, showmeans=True, meanline=True)

    ax.set_title('Persona stats')
    plt.tight_layout()

    # Custom legend with correct colors and explanation for rings
    legend_elements = [
        Line2D([0], [0], color='C0', lw=2, label='Box: 25th-75th percentile)'),
        Line2D([0], [0], color='C2', lw=2, linestyle='--', label='Mean (green line)'),
        Line2D([0], [0], color='orange', lw=2, label='Median (orange line)'),
        Line2D([0], [0], color='C0', lw=1, label='Whiskers: min/max'),
    ]
    #ax.legend(handles=legend_elements, loc='lower right')

    plt.show()




def OLDOLD_str_range_mean(ss):
    if " - " in ss:
        numb = ss.split(" - ")
        return (float(numb[0]) + float(numb[1])) / 2
    else:
        numb = "".join([c for c in ss if c in "1234567890"])
        return int(numb)






def OLDOLD_boost(x, a):
    from math import log
    if not (0 <= x <= 1):
        raise ValueError("x must be between 0 and 1 (inclusive).")
    if a <= 0:
        raise ValueError("Parameter a must be greater than 0.")
        
    return log(1 + a * x) / log(1 + a)







def OLDOLD_most_frequent_cooccurring(texts, keyword, *, max_ngram: int = 3):
    """
    texts       : list[str]   documents to search
    keyword     : str         word *or phrase* that must appear in a document
    max_ngram   : int         longest phrase length to count (default 3)

    returns     : list[(str, int)]  (token/phrase, frequency) with the
                   (keyword, keyword_count) tuple always first.
    """



    import re
    from collections import Counter
    from itertools import islice
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS




    kw_lower = keyword.lower().strip()
    # look for the *whole* keyword / phrase
    kw_pattern = re.compile(rf"\b{re.escape(kw_lower)}\b", flags=re.IGNORECASE)

    stop_words = set(ENGLISH_STOP_WORDS)
    counts = Counter()

    for text in texts:
        if not kw_pattern.search(text):
            continue                      # skip docs that lack the keyword

        # simple tokenisation on word boundaries
        tokens = re.findall(r"\b\w+\b", text.lower())

        # build n-grams of length 1…max_ngram
        for n in range(1, min(max_ngram, len(tokens)) + 1):
            for i in range(len(tokens) - n + 1):
                ngram_tokens = tokens[i : i + n]
                phrase = " ".join(ngram_tokens)

                # skip numbers anywhere in a candidate
                if any(tok[0].isdigit() for tok in ngram_tokens):
                    continue

                if n == 1:  # unigram filtering
                    tok = phrase
                    if tok in stop_words or len(tok) < 3:
                        continue
                else:       # n-gram (n>1) filtering
                    # drop n-grams that are *all* stop-words
                    if all(tok in stop_words for tok in ngram_tokens):
                        continue

                counts[phrase] += 1

    kw_count = counts.get(kw_lower, 0)
    # remove the keyword itself from the pool before sorting
    if kw_lower in counts:
        del counts[kw_lower]

    common = counts.most_common()
    return [(kw_lower, kw_count)] + common








def flatten_list(nested_list):
    """
    Flattens a nested list into a single list.
    """
    return [item for sublist in nested_list for item in sublist]




def OLDOLD_calc_focus_words_ratio_by_date(focus_word_list, analyse_these_events, analyse_these_videos, first_date):
    import pandas as pd

    analyse_these_videos['has_focus_words'] = analyse_these_videos.title.map(lambda x: 1*these_are_in_string(x, [c for c in focus_word_list]))
    #analyse_these_videos['focus_word_list'] = analyse_these_videos.title.map(lambda x: these_are_in_string_return_list(x, [c for c in focus_word_list]))

    #print(analyse_these_videos.has_focus_words.value_counts())

    ext_events_df = pd.merge(left=analyse_these_events, right=analyse_these_videos, how='left', left_on='primary_value', right_on='video_url')

    check_something = ext_events_df[ext_events_df["date"]>=first_date][['simple_date','has_focus_words']].dropna().groupby("simple_date").agg(
        events_w_focus_words=pd.NamedAgg(column="has_focus_words", aggfunc="sum"),
        n_events=pd.NamedAgg(column="simple_date", aggfunc="count"))

    check_something["focus_words_ratio"] = check_something["events_w_focus_words"] / check_something["n_events"]

    return check_something["focus_words_ratio"].to_dict()






def OLDOLD_calc_focus_words_in_donations(focus_word_list, analyse_these_events, analyse_these_videos):
    analyse_these_videos['has_focus_words'] = analyse_these_videos.title.map(lambda x: 1*these_are_in_string(x, [c for c in focus_word_list]))

    ext_events_df = pd.merge(left=analyse_these_events, right=analyse_these_videos, how='left', left_on='primary_value', right_on='video_url')

    fw_in_donations = ext_events_df.groupby("donation_id").agg(
        events_w_focus_words=pd.NamedAgg(column="has_focus_words", aggfunc="sum"),
        n_events=pd.NamedAgg(column="donation_id", aggfunc="count"))

    fw_in_donations = fw_in_donations[fw_in_donations.n_events > 20000].copy()

    fw_in_donations["focus_words_ratio"] = fw_in_donations.events_w_focus_words / fw_in_donations.n_events

    return fw_in_donations.focus_words_ratio.describe()[["count","mean","min","max","50%"]]







def OLDOLD_these_are_in_string_return_list(string, these):
    """
    Check if any of the strings in 'these' are in 'string'.
    """
    hoj = []
    for t in these:
        if t.lower() in string.lower():
            hoj += [t.lower()]
    return hoj






def OLDOLD_these_are_in_string(string, these):
    """
    Check if any of the strings in 'these' are in 'string'.
    """
    for t in these:
        if t.lower() in string.lower():
            return True
    return False



############################################################################################################
###                     manage media storage / GCP bucket
############################################################################################################

def DONT_USE_get_gcp_bucket(bucket_name, verbose = False):
    from google.api_core.exceptions import Forbidden
    from google.cloud import storage

    try:
        # Initialize a client
        client = storage.Client()

        # Get the bucket
        bucket = client.get_bucket(bucket_name)

        # Try to access the bucket's metadata
        bucket.reload()
        if verbose:
            print(f"Access to the bucket '{bucket_name}' is authorized.")
        return bucket
    except Forbidden:
        if verbose:
            print(f"You don't have access to the bucket '{bucket_name}'.")
        return None
    except Exception as e:
        if verbose:
            print(f"An error occurred: {e}")
        return None



def DONT_USE_init_media_storage(verbose=False):
    from os.path import join

    #cf = init_config()

    if cf["media_storage"]["storage_type"]=="GCP":
        if verbose:
            print("Connecting to GCP bucket...")
        main_media_storage = get_gcp_bucket(cf["media_storage"]["GCP_bucket"])
        if main_media_storage is None:
            print("Could not connect to GCP bucket. Exiting.")
            return None
    else:
        if verbose:
            print("Using local storage.")
        main_media_storage = cf["media_storage"]["local_storage_dir"]
    return main_media_storage





def DONT_USE_list_files_in_storage(storage_location, prefix="", include_sub_prefixes=True, suffix=""):
    from os.path import join
    from os import listdir

    if isinstance(storage_location,str): # if it's a string, it's a local directory
        files_in_storage = [fn for fn in listdir(join(storage_location,prefix)) if fn.endswith(suffix)]
    else:
        if suffix != "" and not suffix.startswith("."):
            suffix = "."+suffix
        blobs = storage_location.list_blobs(prefix=prefix)
        files_in_storage = [blob.name for blob in blobs]
        files_in_storage = [fn.replace(prefix,"") for fn in files_in_storage if fn.endswith(suffix)]
        files_in_storage = [fn[1:] if fn[0]=="/" else fn for fn in files_in_storage]
        if not include_sub_prefixes:
            files_in_storage = [fn for fn in files_in_storage if "/" not in fn]
    
    return files_in_storage



def DONT_USE_save_blob_to_storage(storage_location, filename, source_dir="", prefix=""):
    from os.path import join, exists
    from shutil import copyfile
    if isinstance(storage_location,str): # if it's a string, it's a local directory
        if exists(join(source_dir,filename)):
            copyfile(join(source_dir,filename), join(storage_location,prefix,filename))
        else:
            print(f"File '{filename}' not found in '{source_dir}'")
    else:
        blob = storage_location.blob(join(prefix,filename))
        blob.upload_from_filename(join(source_dir,filename))


def DONT_USE_load_blob_from_storage(storage_location, filename, prefix="", dest_dir=""):
    from os.path import join, exists
    from shutil import copyfile
    if isinstance(storage_location,str): # if it's a string, it's a local directory
        if exists(join(storage_location,prefix,filename)):
            copyfile(join(storage_location,prefix,filename), join(dest_dir,filename))
        else:
            print(f"File '{filename}' not found in '{join(storage_location,prefix)}'")
    else:
        blob = storage_location.blob(join(prefix,filename))
        blob.download_to_filename(join(dest_dir,filename))






if __name__ == "__main__":
    print("Module is being run directly.")
