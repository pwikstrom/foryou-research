


import pandas as pd
from datetime import datetime
from copy import copy
import numpy as np

from fyp.fyp_config import fyp_cf
from fyp.types import convert_dtypes_to_pyarrow 

WEEKDAY_MAPPER = { 1:"monday", 2:"tuesday",3:"wednesday",4:"thursday",5:"friday",6:"saturday",7:"sunday"}
GENERIC_MAPPER = fyp_cf["labels"]["GENERIC_MAPPER"]
IRRELEVANT_WORDS = fyp_cf["labels"]["IRRELEVANT_WORDS"]

NOT_CODED =  fyp_cf["labels"]["NOT_CODED"]
UNABLE_TO_DETECT = fyp_cf["labels"]["UNABLE_TO_DETECT"]
OTHER_THINGS = fyp_cf["labels"]["OTHER_THINGS"]





def rename_columns(some_events):
    """
    This function is indempotent
    """
    some_eventsC = some_events.copy()

    fixer_upper = [
        ("B_local_","T_local_"),
        ("B_source_tz_name","T_tz_name"),
        ("D_local_","T_local_"),
        (".","_"),
        ("data_",""),
        ("source_url_","source_"),
        ("_collected",""),
        ("framing_analysis_","FA_"),
        ("cultural_representation_analysis_","CRA_"),
        ("ideological_analysis_","IA_"),

        ]

    pd.set_option('future.no_silent_downcasting', True)

    for fu in fixer_upper:
        mapper = {c:c.replace(fu[0],fu[1]) for c in some_eventsC.columns if (c != c.replace(fu[0],fu[1])) and (not c.replace(fu[0],fu[1]) in some_eventsC.columns)}
        some_eventsC = some_eventsC.rename(columns=mapper).copy()
    
    return some_eventsC





def _day_segment_from_hour(hour: int) -> str:
    if not (0 <= hour <= 23):
        raise ValueError(f"hour must be in 0..23, got {hour}")
    if hour <= 5:
        return "night"
    if hour <= 11:
        return "morning"
    if hour <= 17:
        return "afternoon"
    return "evening"







def infer_timezone_offset(timestamps: pd.Series) -> float:
    """
    Infers timezone offset by finding the 4-hour window with minimum activity.
    Assumes this quietest window centers around 04:00 local time.
    
    Args:
        timestamps: Series of UTC timestamps
        
    Returns:
        Offset in hours (float) from UTC. e.g. +10.0 for Brisbane.
    """
    if len(timestamps) < 10:
        return 0.0 # Not enough data to infer
        
    # Create a DataFrame to aggregate by hour
    df_ts = pd.DataFrame({'ts': timestamps})
    df_ts['hour'] = df_ts['ts'].dt.hour
    
    # Count activity per UTC hour (0-23)
    hourly_counts = df_ts.groupby('hour').size().reindex(range(24), fill_value=0)
    
    # We want a rolling 4-hour window sum. 
    # To handle wrap-around (e.g. 23:00 -> 02:00), we concat the counts 
    hourly_counts_ext = pd.concat([hourly_counts, hourly_counts.iloc[:3]], ignore_index=True)
    
    # Calculate rolling sum
    rolling_sum = hourly_counts_ext.rolling(window=4).sum()
    
    # We strip the first 3 (NaNs/partial from standard rolling if not min_periods=0) 
    # but we used concat so we have valid range. 
    # The result has length 24 + 3 = 27.
    # Indices 0,1,2 are NaNs (window size 4).
    # Valid indices start at 3.
    # Index 3 corresponds to window [0,1,2,3] of extended array = [0,1,2,3] of original.
    # Index 26 corresponds to window [23,0,1,2].
    
    # Extract only the 24 valid windows representing starts 0..23 (wrapped)
    # Window ending at i (where i >= 3) corresponds to hours ...?
    # Let's map rolling_sum index to "Center Hour".
    # We want indices 3 to 26 inclusive (24 values).
    valid_sums = rolling_sum.iloc[3:].reset_index(drop=True)
    # valid_sums now has indices 0 to 23.
    # Index k in valid_sums came from rolling_sum index k+3.
    # rolling_sum index k+3 sums extended array [k, k+1, k+2, k+3].
    # Which corresponds to hours [k%24, (k+1)%24, (k+2)%24, (k+3)%24].
    # Center is roughly k + 1.5.
    
    min_val = valid_sums.min()
    min_indices = valid_sums[valid_sums == min_val].index.tolist()
    
    # Calculate circular mean of these indices
    # Convert hours (indices) to angles, mean vector, convert back
    angles = [2 * np.pi * idx / 24.0 for idx in min_indices]
    y = np.sum(np.sin(angles))
    x = np.sum(np.cos(angles))
    avg_angle = np.arctan2(y, x)
    avg_idx = avg_angle * 24.0 / (2 * np.pi)
    
    if avg_idx < 0:
        avg_idx += 24
        
    # avg_idx represents the "Start Hour" of the window (k).
    # Center of window is k + 2.0 (Midpoint of 4 discrete hour buckets [k, k+3]).
    # e.g. Window [2,3,4,5] -> Center is 4.0.
    # We assume this center is 03:00 Local (Shifted -1 from original 04:00).
    
    center_utc = avg_idx + 2.0
    if center_utc >= 24:
        center_utc -= 24
        
    # Offset = Local - UTC = 3.0 - Center (Shifted -1 from 4.0)
    offset = 3.0 - center_utc
    
    # Normalize to -9 to 15 (User specified range to handle date line wrap)
    # "Add 24 hours to timezones calculated to UTC-11" -> Map -11 to +13.
    # Standard range [-9, 15] covers West Coast US (-8) to NZ (+12/13).
    while offset < -9:
        offset += 24
    while offset > 15:
        offset -= 24
        
    return round(offset) # Round to nearest hour for simplicity (or keeping half hours?)
                         # User said rough guess. 
                         













def extract_local_time_features(
    some_events_df_in = None,
    kind_of_log = None,
    verbose = False):
    """
    Integrates per-donation timezone offsets from persona_stats_cache.
    """




    df = some_events_df_in.copy()

    if verbose:
        print(f"Processing timestamps in dataset to extract local time features... ")

    # ---------------------------------------------------------------------
    # 1. Build local_timestamp depending on log type
    # ---------------------------------------------------------------------
    if kind_of_log == "baseline":
        # the 'baseline' timestamp is not utc - it is in the timezone of the device
        tz_col = "source_url.tz_name"
        ts_col = "timestamp_collected"

        from zoneinfo import ZoneInfo

        unique_tz = df[tz_col].dropna().unique()

        # 1. Rename to Local Timestamp (avoid copy)
        df = df.rename(columns={ts_col: "local_timestamp"})

        # 2. Derive UTC Timestamp using the renamed column
        if len(unique_tz) == 1:
            # Fast path: everything in same tz
            tz = ZoneInfo(unique_tz[0])
            # Localize -> Convert to UTC
            df["T_utc_timestamp"] = (
                df["local_timestamp"]
                .dt.tz_localize(tz, ambiguous='NaT', nonexistent='NaT')
                .dt.tz_convert("UTC")
            )
        else:
            print("slow extraction of local time based features")
            # Slower path: per-timezone blocks
            utc_parts = []
            for tz_name, block in df.groupby(tz_col, sort=False):
                tz = ZoneInfo(tz_name)
                # Localize -> Convert to UTC immediately
                part = (
                    block["local_timestamp"]
                    .dt.tz_localize(tz, ambiguous='NaT', nonexistent='NaT')
                    .dt.tz_convert("UTC")
                )
                utc_parts.append(part)
            # Concatenate identical Dtypes (all UTC)
            df["T_utc_timestamp"] = pd.concat(utc_parts).sort_index()

        # 3. Enforce PyArrow dtypes
        df["local_timestamp"] = df["local_timestamp"].astype("timestamp[ns][pyarrow]")
        df["T_utc_timestamp"] = df["T_utc_timestamp"].astype("timestamp[ns][pyarrow]")
        df["T_tz_offset"] = df["local_timestamp"] - df["T_utc_timestamp"]
        df["T_tz_offset"] = df["T_tz_offset"].dt.total_seconds() / 3600
        df["T_tz_offset"] = df["T_tz_offset"].astype("int64[pyarrow]")

        df = df.rename(columns={tz_col: "T_tz_name"}).astype({"T_tz_name": "string[pyarrow]"})

    elif kind_of_log == "ddp":

        # rename timestamp to T_utc_timestamp
        if "T_utc_timestamp" not in df.columns:
            df = df.rename(columns={"timestamp": "T_utc_timestamp"})
        
        # 2. Ensure UTC Timestamp is valid Datetime
        if not pd.api.types.is_datetime64_any_dtype(df['T_utc_timestamp']):
            df["T_utc_timestamp"] = pd.to_datetime(df["T_utc_timestamp"], unit='s', utc=True)

        # 3. Infer Timezone Offset
        df["T_tz_offset"] = infer_timezone_offset(df["T_utc_timestamp"])
        df["T_tz_offset"] = df["T_tz_offset"].astype("int64[pyarrow]")
        
        # 4. Calculate Local Timestamp
        # Add offset (hours) to UTC time
        offset_timedelta = pd.to_timedelta(df['T_tz_offset'], unit='h')
        df["local_timestamp"] = df["T_utc_timestamp"] + offset_timedelta
        
        # Convert to Naive Local Time (so it represents wall clock time in that timezone)
        df["local_timestamp"] = df["local_timestamp"].dt.tz_localize(None)

        # 5. Enforce PyArrow dtypes
        df["local_timestamp"] = df["local_timestamp"].astype("timestamp[ns][pyarrow]")
        df["T_utc_timestamp"] = df["T_utc_timestamp"].astype("timestamp[ns][pyarrow]")
 

    else:
        raise ValueError("kind_of_log can only be 'baseline' or 'ddp'")

    # ---------------------------------------------------------------------
    # 2. Derive local time features
    # ---------------------------------------------------------------------
    ts = df["local_timestamp"]
    
    iso = ts.dt.isocalendar()  # DataFrame: year, week, day
    iso["day"] = iso["day"].map(WEEKDAY_MAPPER)
    iso["year_week"] = iso["year"].astype(str) + "-" + iso["week"].astype(str)

    # these dtype fixes feel stupid, but I cannot seem to get around it any other way
    df["local_weekday"] = iso["day"].to_list()
    df["local_weekday"] = df["local_weekday"].convert_dtypes(dtype_backend="pyarrow")
    df["local_week"] = iso["year_week"].to_list()
    df["local_week"] = df["local_week"].convert_dtypes(dtype_backend="pyarrow")

    local_hour = ts.dt.hour.astype("uint8[pyarrow]")

    df["local_day_segment"] = local_hour.map(_day_segment_from_hour).convert_dtypes(dtype_backend="pyarrow")

    df["local_date"] = ts.dt.date
    df["local_date"] = pd.to_datetime(df["local_date"]).convert_dtypes(dtype_backend="pyarrow")

    if verbose:
        print("...done")

    return df








def get_factors_and_features_from_var_schema(some_events_df = None, verbose = False):
    
    var_schema = fyp_cf["var_schema"]
    
    the_factors = sorted(list(set(var_schema[var_schema["role"].isin(['factor','group_factor'])].variable_name)))
    the_features = sorted(list(set(var_schema[var_schema["role"]=='feature'].variable_name)))
    if some_events_df is not None:
        the_factors = [c for c in the_factors if c in some_events_df.columns]
        the_features = [c for c in the_features if c in some_events_df.columns]

    if verbose and len(the_factors) > 0:
        print("    Factors:",", ".join(the_factors))
    if verbose and len(the_features) > 0:
        print("    Features:",", ".join(the_features))

    return the_factors, the_features







def _try_eval(s):

    try:
        return eval(s)
    except:
        return s



def _is_emoji(s: str) -> bool:
    from emoji import EMOJI_DATA

    """Return True if the string is a valid emoji (including multi-char ones)."""
    return s in EMOJI_DATA






def get_grouping_factors_from_var_schema(some_events_df = None, verbose = False):
    
    var_schema = fyp_cf["var_schema"]
    
    the_grouping_factors = sorted(list(set(var_schema[var_schema["role"]=='group_factor'].variable_name)))
    if some_events_df is not None:
        the_grouping_factors = [c for c in the_grouping_factors if c in some_events_df.columns]
    
    if verbose  and len(the_grouping_factors) > 0:
        print("    Group Factors:",", ".join(the_grouping_factors))

    return the_grouping_factors







def recode_descriptions(
    a_description: str | pd.Series, 
    recoding_policy: dict = {}) -> dict | pd.DataFrame:
    """
    Extract hashtags, mentions, and other words from a description string or Series.
    """
    
    # Vectorized handling for Series
    if isinstance(a_description, pd.Series):
        # We'll use a fast regex approach to extract all relevant tokens once
        # Token pattern: #word or @word or word
        # We need to exclude IRRELEVANT_WORDS and handle emojis
        # Doing full logic in regex is hard, but we can extract all words and filter
        
        # NOTE: For complex logic like "exclude irrelevant words", a list comprehension
        # is often faster than pure pandas string ops if the ops are complex.
        # But let's try to be efficient. 
        
        # Actually, for 100k rows, a simple apply might be acceptable if the inner function is fast,
        # but let's try to speed it up.
        # The original logic splits by space, cleans chars, checks length/irrelevant/emoji.
        
        # Let's stick to the list comprehension for now as it's readable and Python 3.14 is fast.
        # Pre-compile translation table for fast cleaning
        import string
        # chars to remove: ",.:;!)(*/&|^%$#@<>?'`’1234567890"
        remove_chars = ",.:;!)(*/&|^%$#@<>?'`’1234567890"
        trans_table = str.maketrans("", "", remove_chars)
        
        # Optimized Apply
        def _fast_parse(text):
            if not isinstance(text, str) or not text:
                return {"hashtags": [], "mentions": [], "not_hashtags": []}
            
            hashtags = []
            mentions = []
            not_hashtags = []
            
            # fast split
            # text.split() is fast
            words = text.split()
            
            for w in words:
                # fast clean using translate
                # w.lower()
                clean_word = w.lower().translate(trans_table)
                
                if not clean_word: continue
                
                # logic
                if (len(clean_word) > 1 and clean_word not in IRRELEVANT_WORDS) or _is_emoji(clean_word):
                    if w.startswith("#"):
                        hashtags.append(clean_word)
                    elif w.startswith("@"):
                        mentions.append(clean_word)
                    else:
                        not_hashtags.append(clean_word)
                        
            return {"hashtags": hashtags, "mentions": mentions, "not_hashtags": not_hashtags}

        return a_description.apply(_fast_parse)

    # Legacy single string handling
    hashtags = []
    not_hashtags = []
    mentions = []
    if not isinstance(a_description,str) or len(a_description) == 0:
        return {
            "hashtags":[],
            "mentions":[],
            "not_hashtags":[]
        }
    words = a_description.split(" ")
    for w in words:
        if len(w)>0:
            first_char = w[0]
            clean_word = "".join([j for j in w.lower() if not j in ",.:;!)(*/&|^%$#@<>?'`’1234567890"])
            if (len(clean_word)>1 and not clean_word in IRRELEVANT_WORDS) or _is_emoji(clean_word):
                if first_char=="#":
                    hashtags += [clean_word]
                elif first_char=="@":
                    mentions += [clean_word]
                else:
                    not_hashtags += [clean_word]
        
    return {
        "hashtags":hashtags,
        "mentions":mentions,
        "not_hashtags":not_hashtags
    }


def recode_call_to_action(
    a_text: str | pd.Series, 
    recoding_policy: dict = {}) -> dict | pd.Series:
    
    #import pandas as pd
    
    if isinstance(a_text, pd.Series):
        def _fast_parse_cta(text):
            if not isinstance(text, str) or not text:
                return {"words": []}
            cta_words = []
            for w in text.split():
                if not w: continue
                clean_word = "".join([c for c in w.lower() if c not in ",.:;!)(*/&|^%$#@<>?'`’1234567890"])
                if (len(clean_word)>1 and clean_word not in IRRELEVANT_WORDS) or _is_emoji(clean_word):
                    cta_words.append(clean_word)
            return {"words": cta_words}
        return a_text.apply(_fast_parse_cta)

    if not isinstance(a_text, str):
        return a_text

    cta_words = []
    if not isinstance(a_text,str) or len(a_text) == 0:
        return {
            "words":[],
        }
    words = a_text.split(" ")
    
    for w in words:
        if len(w)>0:
            clean_word = "".join([j for j in w.lower() if not j in ",.:;!)(*/&|^%$#@<>?'`’1234567890"])
            if (len(clean_word)>1 and not clean_word in IRRELEVANT_WORDS) or _is_emoji(clean_word):
                cta_words += [clean_word]
        
    return {
        "words":cta_words,
    }




def recode_speech_vs_music(
    a_string: str | pd.Series, 
    recoding_policy: dict = {}) -> float | pd.Series | None:
    
    #import pandas as pd
    
    if isinstance(a_string, pd.Series):
        extracted = a_string.astype(str).str.extract(r'(\d+)% speech')[0]
        return pd.to_numeric(extracted, errors='coerce') / 100.0

    #from numpy import array, int64, float64

    if not isinstance(a_string, str):
        return a_string

    some_list = a_string.split(",")
    some_list_check = [[1 * ("speech" in h), 1 * ("music" in h)] for h in some_list]
    if len(some_list) == 2 and all(np.array(some_list_check).sum(axis=0) == 1):
        try:
            some_list = [{h.split("%")[1].strip(): np.int64(h.split("%")[0])} for h in some_list]
        except Exception:
            return None

        polished_list = []
        for d in some_list:
            the_key = list(d.keys())[0]
            if "speech" in the_key:
                polished_list += [{"speech": d[the_key]}]
            elif "music" in the_key:
                polished_list += [{"music": d[the_key]}]
        return [rr for rr in polished_list if "speech" in rr.keys()][0]["speech"] / 100
    else:
        return None
    


def recode_scores(
    a_string: str | pd.Series, 
    recoding_policy: dict = {}) -> float | pd.Series:
    """
    takes a string of this template: "<numeral><, ><text>" and returns the numeral split by 100
    """
    #import pandas as pd
    #from pandas import NA as pd.NA
    #from numpy import int64
    
    if isinstance(a_string, pd.Series):
        val_str = a_string.astype(str).str.split(", ", n=1).str[0]
        return pd.to_numeric(val_str, errors='coerce') / 100.0

    if isinstance(a_string,str):
        the_val = a_string.split(", ")[0]
        try:
            the_val = np.int64(the_val)
            return the_val / 100
        except:
            return pd.NA
    else:
        return a_string




def recode_long_strings(
    s: str | list | pd.Series, 
    recoding_policy) -> str | pd.Series:

    #import pandas as pd
    #from copy import copy
    
    if isinstance(s, pd.Series):
        def _get_first_if_list(x):
            if isinstance(x, list):
                return x[0] if len(x) > 0 else ""
            if isinstance(x, str):
                return x
            return ""
        
        new_s = s.map(_get_first_if_list)
        new_s = new_s.replace("-", "")
        return new_s.fillna(NOT_CODED)

    if not isinstance(s,(str,list)):
        return NOT_CODED
    if isinstance(s,list) and len(s)>0:
        new_string = s[0]
    elif isinstance(s,list) and len(s)==0:
        new_string = ""
    else:
        new_string = copy(s)
    
    if new_string == "-":
        new_string = ""

    return new_string





def recode_scene_sentiments(
    a_string: str, 
    recoding_policy : dict = {}) -> dict:
    """takes a string assumed to contain words that are describing positive/negative valence as well as high/low energy
    and returns a dict with two values (valence and energy) ranging between -1 and 1 

    TODO: check the word lists. They are probably not exhaustive.
    """



    if not isinstance(a_string,str):
        return {"valence":pd.NA,"energy":pd.NA}

    a_string = a_string.lower().replace("-","").replace(" ","")
    valence = 0
    for w in fyp_cf['labels']['POSITIVE_WORDS']:
        if w in a_string:
            valence = 1
    for w in fyp_cf['labels']['NEGATIVE_WORDS']:
        if w in a_string:
            valence = -1
    energy = 0
    for w in fyp_cf['labels']['HIGH_ENERGY_WORDS']:
        if w in a_string:
            energy = 1
    for w in fyp_cf['labels']['LOW_ENERGY_WORDS']:
        if w in a_string:
            energy = -1
    
    return {"valence":valence,"energy":energy}
    



# "G_faces_age_estimate" is a bit special since Gemini generates age ranges (strings)
# and I want to convert these to numeric values. I first convert the list of strings (age ranges) into actual
# list of age ranges (still strings). I then convert each age range to the mean (float) in the range. This list of floats
# can be aggregated using the function for continuous variables "calc_centre_and_entropy()".
def recode_faces_age_estimate(
    an_age_range_list: str | pd.Series, 
    recoding_policy : dict = {}) -> float | pd.Series:
    
    def _single_age_range_str_to_float(an_age_range: str) -> float:
        if pd.isna(an_age_range):
            return pd.NA

        try:
            return np.float64(an_age_range)
        except:
            pass

        if isinstance(an_age_range,str) and an_age_range.count("-")==1:
            try:
                age_limits = [np.int64(i) for i in an_age_range.split("-")]
                if age_limits[1]<age_limits[0]:
                    return pd.NA
                return np.float64(np.mean(age_limits))
            except:
                return pd.NA
        return pd.NA


    if isinstance(an_age_range_list, pd.Series):
        # Explode, parse range to mean, groupby index mean.
        # "20-30 | 40-50" -> ["20-30", "40-50"]
        s = an_age_range_list.astype(str).str.split(" | ")
        exploded = s.explode()
        
        # Parse "20-30"
        # regex capture
        ranges = exploded.str.extract(r'(\d+)-(\d+)')
        low = pd.to_numeric(ranges[0], errors='coerce')
        high = pd.to_numeric(ranges[1], errors='coerce')
        
        # Valid means
        means = (low + high) / 2.0
        # Filter invalid (where high < low or NaN)
        valid_mask = (high >= low) & (~means.isna())
        means = means.where(valid_mask)
        
        # Group back to original index
        # We need to handle the index properly (explode keeps index)
        # groupby(level=0).mean()
        final_means = means.groupby(level=0).mean()
        
        # Align with original index to ensure size
        return final_means.reindex(an_age_range_list.index)



    if isinstance(an_age_range_list,str):
        return np.mean(list(map(_single_age_range_str_to_float, an_age_range_list.split(" | "))))
    else:
        return an_age_range_list




def recode_challenges(
    challenges : str | pd.Series,
    recoding_policy : dict = {}) -> list | pd.Series:

    #import pandas as pd
    
    if isinstance(challenges, pd.Series):
        # Result should be list of strings
        # vector split
        # We need to strip spaces? " | " split usually handles the main separator.
        # But if we need exact "strip" behavior:
        # split(" | ") -> list.
        # Then clean elements? 
        # Fast path: str.split(" | ")
        # But cleaning "  " -> " " inside list elements is hard vectorized.
        # Assuming " | " is good enough separator.
        
        # If strict cleaning needed: replace "  " with " " on the full string first?
        # challenges.str.replace("  ", " ").str.split(" | ")
        
        s = challenges.astype(str).str.replace("  ", " ", regex=False).str.split(" | ")
        # Filter empty strings in list? 
        # `[v for v in ... if v.strip()]`
        # Using apply for simple list filtering is acceptable if not huge lists.
        # Or just return the split result if data is clean.
        
        # Preserving original logic strictly:
        def _clean_list(mod_list):
            if not isinstance(mod_list, list): return []
            return [v.strip() for v in mod_list if v.strip()]
            
        return s.map(_clean_list)

    if isinstance(challenges, str):
        return [
            v.strip().replace("  ", " ")
            for v in challenges.split(" | ")
            if v.strip()
        ]
    else:
        return []




# making a very rough simplification of main activity, picking the first word that
# ends with -ing. The assumption is that this is a verb (I know it isn't) and
# that it captures the video's main activity 
def recode_main_activity(
    fine_actitivies_string : str | pd.Series, 
    recoding_policy : dict = {}):
    
    #import pandas as pd
    #from pandas import NA as pd.NA
    
    if isinstance(fine_actitivies_string, pd.Series):
        # Parse stringified list
        # Then find first *ing
        # Vectorized:
        # 1. Parse stringified list (use vectorized logic if possible)
        # Assuming splitter from policy (usually ", " or similar?)
        # If we use recode_stringified_list in vectorized way...
        
        # Let's use the vectorized recode_stringified_list logic inline or helper.
        # Then apply *ing filter.
        
        # Step 1: get lists
        if 'splitter' in recoding_policy and not pd.isna(recoding_policy['splitter']):
            splitter = recoding_policy['splitter']
            # split
            # clean chars?
            # This is complex to fully vectorize without regex.
            # let's assume simple split for now or use the helper with apply if simpler.
            pass
        
        # Optimized Apply Approach
        # recode_stringified_list is complex.
        # But finding "word ending in ing" might be done with regex on original string!
        # Regex: r'\b(\w+ing)\b'
        # This is MUCH faster than parsing list.
        
        extracted = fine_actitivies_string.astype(str).str.lower().extract(r'\b([a-zA-Z]+ing)\b', expand=False)
        # If matched, return [match]. Else [UNABLE_TO_DETECT] or [NOT_CODED]?
        # Logic: if list empty -> NOT_CODED (if was empty string?) or fallback.
        # If no ing found -> UNABLE_TO_DETECT.
        
        # This regex approach is an approximation but likely accurate for "first word ending in ing".
        # Original code picked "first word that ends with -ing". 
        
        
        # extracted = extracted.fillna(mask_na=False) # don't mask yet
        # extract returns NaN for no match, which is what we want.

        
        # We need to handle NA inputs -> NOT_CODED.
        # Logic:
        # If string NA/empty -> NOT_CODED.
        # If no *ing found -> UNABLE_TO_DETECT.
        
        # result series
        res = extracted.apply(lambda x: [x] if pd.notna(x) else [UNABLE_TO_DETECT])
        
        # Fix NA inputs
        mask_na = fine_actitivies_string.isna() | (fine_actitivies_string == "")
        res[mask_na] = [[NOT_CODED] for _ in range(mask_na.sum())] # annoying assignment
        # simpler:
        # res = res.where(~mask_na, other=[[NOT_CODED]] * len(res)) # no, series assignment
        
        # Simple map for final fixup might be fast enough
        def _finalize(val, original):
             if pd.isna(original) or original == "": return [NOT_CODED]
             if pd.isna(val): return [UNABLE_TO_DETECT] # extract failed
             return [val]
             
        # extracted has the word or NaN.
        # original has the string.
        # We can reconstruct.
        
        # Actually, let's use the exact original logic via helper if regex is risky.
        # But regex is FAST.
        # Let's stick to regex `r'\b(\w+ing)\b'`.
        
        has_ing = extracted.notna()
        result = pd.Series([ [UNABLE_TO_DETECT] ] * len(fine_actitivies_string), index=fine_actitivies_string.index)
        
        # Set found
        # result[has_ing] = extracted[has_ing].apply(lambda x: [x]) # slow?
        result[has_ing] = extracted[has_ing].map(lambda x: [x])
        
        # Set NA/Not coded
        # Original: if string valid but list empty -> fallback?
        # If original string is NA -> [NOT_CODED]
        mask_original_na = fine_actitivies_string.isna() | (fine_actitivies_string == NOT_CODED)
        # Assigning list to series slice is tricky.
        # Use a generator/list comp for the whole series construction?
        
        final_list = []
        ext_arr = extracted.values
        orig_arr = fine_actitivies_string.values
        for i in range(len(fine_actitivies_string)):
             if pd.isna(orig_arr[i]) or orig_arr[i] == NOT_CODED:
                 final_list.append([NOT_CODED])
             elif pd.notna(ext_arr[i]):
                 final_list.append([ext_arr[i]])
             else:
                 final_list.append([UNABLE_TO_DETECT])
                 
        return pd.Series(final_list, index=fine_actitivies_string.index)

    fine_actitivies_list = recode_stringified_list(fine_actitivies_string, recoding_policy)

    if isinstance(fine_actitivies_list,list):
        jj = [q for q in fine_actitivies_list if isinstance(q,str) and q.endswith("ing")]
        if fine_actitivies_list == [NOT_CODED]:
            return [NOT_CODED]
        elif len(jj)>0:
            return [(jj[0])]
        else:
            return [UNABLE_TO_DETECT]
    else:
        return [NOT_CODED]




"""def recode_timestamp(
    timestamp : pd.Timestamp | pd.Series, 
    recoding_policy : dict = {}) -> int | pd.Series:

    #import pandas as pd
    #from numpy import int64
    
    if isinstance(timestamp, pd.Series):
        # Optimize for datetime series
        if pd.api.types.is_datetime64_any_dtype(timestamp):
           return timestamp.astype('int64') // 10**9
        else:
           # coerce
           return pd.to_datetime(timestamp, errors='coerce').astype('int64') // 10**9

    return np.int64(timestamp.timestamp())"""
    


def recode_stringified_list(
    a_string_representing_a_list, 
    recoding_policy
    ) -> list | pd.Series:

    #import pandas as pd
    #from pandas import isna, NA as pd.NA
    
    if isinstance(a_string_representing_a_list, pd.Series):
        # This function is the heavy lifter for parsing stringified lists.
        # Vectorizing:
        # If straightforward split:
        splitter = recoding_policy.get("splitter", pd.NA)
        mapper = recoding_policy.get("mapper", {})
        ignore_strings = recoding_policy.get("ignore_strings", [])
        
        # Helper for apply
        def _parse(val):
            if pd.isna(val): return [NOT_CODED]
            s_val = str(val)
            if len(s_val) < 1 or s_val in ["-", " "]: return [UNABLE_TO_DETECT] # no_data_fallback
            
            # mini mapper check (1->yes etc provided in original code but not robustly?)
            # The original code has `mini_mapper`.
            
            out = []
            if not pd.isna(splitter):
                parts = s_val.lower().split(splitter)
                for an_element in parts:
                    if len(an_element) > 0:
                        cleaned = an_element.replace("//", "").replace("&", " and ").replace("/", " or ")
                        # removal of chars
                        # strict char filter
                        # ".,:;!)(*/&|^%$#@<>?'`’1234567890"
                        # This loop is heavy.
                        # return cleaned part
                        out.append(cleaned) 
            else:
                 pass # implicit single?
            
            if not out: return [UNABLE_TO_DETECT]
            return out
            
        # Due to complexity of cleaning logic (char replacement loops), pure vectorization is hard.
        # But we can optimize the apply.
        # Or checking if we can use regex replacement.
        
        # For now, sticking to apply() logic but ensuring it accepts Series to fit the pattern.
        # But actually, implementing the EXACT logic of original using apply is safer than rewriting logic incorrectly.
        pass # use fallback processing or implement apply loop below

    # Legacy / Single Item logic
    no_data_fallback = UNABLE_TO_DETECT 
    
    # ... (rest of original function logic if called on single item)
    # Since we are modifying the function signature, we should support Series input by delegating to apply
    if isinstance(a_string_representing_a_list, pd.Series):
         return a_string_representing_a_list.apply(lambda x: recode_stringified_list(x, recoding_policy))

    ignore_strings = recoding_policy.get("ignore_strings", [])
    splitter = recoding_policy.get("splitter", None)
    mapper = recoding_policy.get("mapper", {})
    
    mini_mapper = {1: "yes", 0: "no", True: "yes", False: "no"}

    list_of_the_words = [] 

    # if the string that is representing a list is na, assume that it hasn't been coded
    if pd.isna(a_string_representing_a_list):
        list_of_the_words += [NOT_CODED]

    # if there is a string, but the length is zero
    elif len(str(a_string_representing_a_list)) < 1 or str(a_string_representing_a_list) in ["-"," "]:
        list_of_the_words += [no_data_fallback]

    else:
        a_string_representing_a_list = mini_mapper.get(a_string_representing_a_list,a_string_representing_a_list)

        if not pd.isna(splitter):
            for an_element in str(a_string_representing_a_list).lower().split(splitter):
                if len(an_element)>0:
                    an_element = an_element.replace("//", "").replace("&", " and ").replace("/", " or ")
                    clean_word = "".join([j for j in an_element.lower() if not j in ",.:;!)(*/&|^%$#@<>?'`’1234567890"])
                    if (len(clean_word)>1 and not clean_word in ignore_strings)  or _is_emoji(clean_word):
                        list_of_the_words += [mapper.get(clean_word,clean_word)]
        else:
            pass
        
    if len(list_of_the_words) == 0:
        list_of_the_words += [no_data_fallback]

    return list_of_the_words




def implement_missing_data_policy(x, missing_data_policy, the_median=0):
    
    if isinstance(x, pd.Series):
        # Vectorized implementation
        # Vectorized implementation
        # Replace apply(_is_missing) with vectorized mask
        
        # 1. Check direct scalar matches and NaNs
        # Note: x == NOT_CODED works for scalars. 
        # If x has mixed types (lists), equality comparison might be tricky but usually handles it (False for list!=scalar).
        # But to be safe and avoid "ambiguous truth value" errors for [NOT_CODED] == NOT_CODED comparisons:
        # We handle lists separately.
        
        mask_basic = x.isna()
        
        # Safe scalar comparison for "== NOT_CODED"
        # If x is object, it might contain lists. x == scalar might trigger elementwise check if x was an array, 
        # but x is a Series. Series == scalar is fine.
        # But if an element of Series is a list/array, `element == scalar` might return an array (if numpy) or False (if list).
        # If it returns an array, Series.eq converts it to boolean? No, it raises ValueError if valid boolean result is ambiguous.
        # So we must NOT use `x == NOT_CODED` blindly on object columns that might contain arrays.
        
        # Strategy:
        # Use simple map(type) check to isolate lists? No, map is slow.
        # Use `x.astype(str) == str(NOT_CODED)`? Slow string conversion.
        
        # Only do strict checks if object.
        if x.dtype == object:

            
            mask_scalar = x.isin([NOT_CODED])
            
            # Now list check:
            # We need to ensure we can use .str.
            
            try:
                # This works if at least some strings/lists or object dtype allows it?
                # Actually, .str accessor on object series works if it contains mixed types.
                # But if all are ints, it fails.
                mask_list = (x.str.len() == 1) & (x.str[0] == NOT_CODED)
                mask_list = mask_list.fillna(False)
            except AttributeError:
                # No str accessor means no lists/strings usually?
                mask_list = False
            
            mask = mask_basic | mask_scalar | mask_list
            
        else:
            # Numeric or specific type
            mask = mask_basic | (x == NOT_CODED)
        
        if not mask.any():
            return x
            
        result = x.copy()
        
        if missing_data_policy == "empty":
            result.loc[mask] = pd.Series([[] for _ in range(mask.sum())], index=result.index[mask])
        elif missing_data_policy == "drop":
            result[mask] = pd.NA
        elif missing_data_policy == "median":
            result[mask] = the_median
        elif missing_data_policy == "keep":
            # if isna -> [NOT_CODED], else keep x (which is what?)
            # The original logic: if isna(x) -> [NOT_CODED].
            # If x was NOT_CODED (str), it returns x (NOT_CODED).
            
            # Implementation: Replace NA with [NOT_CODED]. leave "not coded" alone.
            mask_na = x.isna()
            result.loc[mask_na] = pd.Series([[NOT_CODED] for _ in range(mask_na.sum())], index=result.index[mask_na])
            
        elif missing_data_policy == "zero":
            # Check type of first non-missing element to decide 0 vs "no"?
            # Or pass a hint. The original code checks x itself.
            # "numeric" string check is weird in original? `isinstance(gg,"numeric")` is probably wrong (string "numeric")?
            # actually `isinstance(gg,"numeric")` checks if class is string "numeric" which is false.
            # It likely meant `isinstance(gg, (int, float))`.
            
            # Let's assume numeric -> 0, else "no".
            # We can check dtype of series.
            if pd.api.types.is_numeric_dtype(x):
                val = 0
            else:
                val = "no"
                
            # If input was list, return [val]
            # This complex conditional typing is hard to vectorize perfectly without context.
            # For now, simplistic approach:
            result[mask] = val
        
        return result

    if (isinstance(x,list) and len(x)==1 and x[0]==NOT_CODED) or (isinstance(x,str) and x==NOT_CODED) or ((not isinstance(x,list)) and pd.isna(x)):
        if missing_data_policy == "empty":
            return []
        elif missing_data_policy == "drop":
            return pd.NA
        elif missing_data_policy == "median":
            return the_median
        elif missing_data_policy == "keep":
            if pd.isna(x):
                return [NOT_CODED]
            else:
                return x
        elif missing_data_policy == "zero":
            gg = x if not isinstance(x,list) else x[0]
            if isinstance(gg,(int, float, np.int64, np.float64)):
                gg_out = 0
            else:
                gg_out = "no"
            if isinstance(x,list):
                return [gg_out]
            return gg_out
        else:
            return x

    else:
        return x



def implement_unable_to_detect_policy(x, unable_to_detect_policy, the_median=0):


    if isinstance(x, pd.Series):
        # Vectorized implementation
        mask_basic = x.isna()
        
        if x.dtype == object:
             mask_scalar = x.isin([UNABLE_TO_DETECT])
             try:
                 mask_list = (x.str.len() == 1) & (x.str[0] == UNABLE_TO_DETECT)
                 mask_list = mask_list.fillna(False)
             except AttributeError:
                 mask_list = False
             
             mask = mask_basic | mask_scalar | mask_list
        else:
             mask = mask_basic | (x == UNABLE_TO_DETECT)

        if not mask.any():
            return x
            
        result = x.copy()
        
        if unable_to_detect_policy == "empty":
            result.loc[mask] = pd.Series([[] for _ in range(mask.sum())], index=result.index[mask])
        elif unable_to_detect_policy == "drop":
            result[mask] = pd.NA
        elif unable_to_detect_policy == "median":
            result[mask] = the_median
        elif unable_to_detect_policy == "keep":
             mask_na = x.isna()
             result.loc[mask_na] = pd.Series([[UNABLE_TO_DETECT] for _ in range(mask_na.sum())], index=result.index[mask_na])
        elif unable_to_detect_policy == "zero":
             if pd.api.types.is_numeric_dtype(x):
                val = 0
             else:
                val = "no"
             result[mask] = val
             
        return result

    if (isinstance(x,list) and len(x)==1 and x[0]==UNABLE_TO_DETECT) or (isinstance(x,str) and x==UNABLE_TO_DETECT) or ((not isinstance(x,list)) and pd.isna(x)):
        if unable_to_detect_policy == "empty":
            return []
        elif unable_to_detect_policy == "drop":
            return pd.NA
        elif unable_to_detect_policy == "median":
            return the_median
        elif unable_to_detect_policy == "keep":
            if pd.isna(x):
                return [UNABLE_TO_DETECT]
            else:
                return x
        elif unable_to_detect_policy == "zero":
            gg = x if not isinstance(x,list) else x[0]
            if isinstance(gg,(int, float, np.int64, np.float64)):
                gg_out = np.int64(0)
            else:
                gg_out = "no"
            if isinstance(x,list):
                return [gg_out]
            return gg_out
        else:
            return x
    else:
        return x







def recode_events_df(
    study_dataset: pd.DataFrame = None,
    drop_single_value_cols: bool = True,
    verbose: bool = False
    ):


    # Safe nunique for lists
    def _safe_nunique(s):
        try:
            return s.nunique()
        except TypeError:
            return s.astype(str).nunique()



    print(f"Recoding variables, implementing missing data policy and a whole range of other things...")

    # This thing now only works with a study dataset as input
    # It is not used in the web interface but only in the offline data prep

    if study_dataset is None:
        print("  This process cannot run without a study dataset as input. Process failed.")
        return None




    cool_events = study_dataset.copy()

    var_schema = fyp_cf["var_schema"].copy()

    var_schema.set_index("variable_name", inplace=True)


    # this is where evaluation of the test based variables in the csv takes place
    var_schema[['mapper','ignore_strings','recode_func']] = var_schema[['mapper','ignore_strings','recode_func']].map(_try_eval)

    fyp_factors, _ = get_factors_and_features_from_var_schema(some_events_df = cool_events, verbose = verbose)


    # this will be overwritten in at a later stage - I just want to turn it into a string for now
    try:
        if "session_id" in cool_events.columns:
            cool_events["session_id"] = cool_events["session_id"].map(lambda x:f"S{int(x):05}" if pd.notna(x) else pd.NA)
    except Exception as e:
        # it's not vital that this goes well
        pass

    # this is a bit redundant too - these variables checked (are dropped again if necessary) at another stage
    variables_not_found_in_var_schema = list(set(cool_events.columns) - set(var_schema.index))
    if len(variables_not_found_in_var_schema) > 0:
        if verbose:
            join_str = "\n    - "
            print(f"Step 1. Dropping {len(variables_not_found_in_var_schema)} columns not found in the variable scheme:\n    - {join_str.join(variables_not_found_in_var_schema)}")
        cool_events = cool_events.drop(columns=variables_not_found_in_var_schema).copy()



    if drop_single_value_cols:
        single_value_columns = [c for c in cool_events.columns if _safe_nunique(cool_events[c])==1 and c not in fyp_factors]
        if verbose:
            join_str = "\n    - "
            print(f"Step 2. Dropping {len(single_value_columns)} single value columns:\n    - {join_str.join(single_value_columns)}. Shape: {cool_events.shape}")
        cool_events = cool_events.drop(columns=single_value_columns).copy()



    if verbose:
        print(f"Executing recode policies from variable schema. Shape: {cool_events.shape}")


    cool_columns = copy(cool_events.columns)
    # iterate over the columns in the events df
    for i,c in enumerate(cool_columns):
        preamble = f"    {(i+1):02}/{len(cool_columns):02}. {c}{' '*(40-len(c))}"
        preamble2 = f"    {' '*6} {c}{' '*(40-len(c))}"
        #if verbose: 
        #    print(preamble, end="", flush=True)

        # if this is in the var_schema...
        if c in var_schema.index:
            this_var_schema = var_schema.loc[c].to_dict() 

            if this_var_schema.get("role", "undefined") != "skip":

                # ------------------------------------------------------
                # 1.
                # ------------------------------------------------------
                if this_var_schema.get("scale", "undefined") == "raw" and c+"_raw" in var_schema.index:
                    if verbose:
                        print(f"{preamble}Copied raw. ", end="", flush=True)
                    cool_events[c+"_raw"] = cool_events[c].copy()
                else:
                    if verbose:
                        print(preamble, end="", flush=True)

                # ------------------------------------------------------
                # 2. execute the recode function
                # ------------------------------------------------------
                func = this_var_schema.get("recode_func", None)
                if not pd.isna(func):
                   # Pass the full series
                    try:
                        cool_events[c] = func(cool_events[c], this_var_schema)
                        if verbose: print(f"Recoded successfully ({this_var_schema.get('scale', 'unknown scale')})")
                    except Exception as e:
                        # Fallback
                        print(f"Warning: Vectorized recode failed: ({e}). Falling back to map.")
                        try:
                            cool_events[c] = cool_events[c].map(lambda x: func(x, this_var_schema))
                        except Exception as e:
                            raise Exception(f"Error: Map recode also failed: ({e}).")
                else:
                    if verbose: print(f"Has no recode func, so no change ({this_var_schema.get('scale', 'unknown scale')})")


                # ------------------------------------------------------
                # 3. implement missing data and unable to detect policies
                # ------------------------------------------------------
                if (this_var_schema.get('unable_to_detect_policy', 'unknown') == "median") or (this_var_schema.get('missing_data_policy', 'unknown') == "median"):
                    # Check if numeric before median
                    if pd.api.types.is_numeric_dtype(cool_events[c]):
                        a_fine_median = cool_events[c].median()
                    else:
                        a_fine_median = 0 # fallback
                else:
                    a_fine_median = None

                cool_events[c] = implement_unable_to_detect_policy(
                    cool_events[c],
                    this_var_schema.get("unable_to_detect_policy","No policy"),
                    a_fine_median)

                cool_events[c] = implement_missing_data_policy(
                    cool_events[c],
                    this_var_schema.get("missing_data_policy","No policy"),
                    a_fine_median)
                
                
                # ------------------------------------------------------
                # 4. Check for multiple values/types logic
                # ------------------------------------------------------
                
                # If we expect single values (categorical, dichotomous, etc.), ensure no lists > 1
                if this_var_schema.get("scale", "") in ["categorical","dichotomous","ordinal","ratio","interval","datetime"]:
                    # Fast check: if object type, might contain lists
                    if cool_events[c].dtype == object:
                        # 'get first if list' logic normalization
                        def _normalize_single(x):
                            if isinstance(x, list):
                                if len(x) > 1: return x # leave as list for validation
                                return x[0] if x else pd.NA
                            return x
                            
                        # apply normalization
                        cool_events[c] = cool_events[c].map(_normalize_single)
                        
                        # Use a sample check or fast check for remaining lists (validation)
                        has_lists = cool_events[c].map(lambda x: isinstance(x, list)).any()
                        if has_lists:
                             # calculate count for error message
                             count = cool_events[c].map(lambda x: isinstance(x, list)).sum()
                             raise ValueError(f"{c} has {count} values with more than one entry. Only a single value is allowed for categorical, dichotomous, ordinal, ratio, and interval variables.")




                # ------------------------------------------------------
                # 4&half. for ratio variables, I only accept numeric values
                # ------------------------------------------------------
                if (this_var_schema["scale"] in ["ratio"]):
                    cool_events[c] = cool_events[c].astype("double[pyarrow]")
                    # Check for integers using numpy float64 - safer for NaNs and avoids pyarrow mod error
                    if not (cool_events[c].dropna().astype("float64") % 1 != 0).any():
                        cool_events[c] = cool_events[c].astype("int64[pyarrow]")



                # ------------------------------------------------------
                # 5. for dichotomous variables, I only accept "yes" and "no" as values 
                # ------------------------------------------------------
                if (this_var_schema["scale"] in ["dichotomous"]):
                    # Vectorized check with safe handling for unhashables (lists)
                    try:
                        uniques = set(cool_events[c].dropna().unique())
                    except TypeError:
                        # Likely lists remaining. Convert to string for uniqueness check
                        uniques = set(cool_events[c].dropna().astype(str).unique())
                        
                    if not uniques <= {'yes','no'}:
                        raise ValueError(f"{c} is a dichotomous variable. Only 'yes', 'no' are accepted values. Found: {uniques}")
                    

                # ------------------------------------------------------
                # 6. for dict variables, I unpack the dicts into new separate columns
                # ------------------------------------------------------
                # Check if first valid element is dict
                first_val = None
                try:
                    valid_c = cool_events[c].dropna()
                    if not valid_c.empty:
                        first_val = valid_c.iloc[0]
                except Exception:
                    pass

                if isinstance(first_val, dict):
                    # Proceed with unpacking
                    new_thing = pd.json_normalize(cool_events[c])
                    new_thing = new_thing.add_prefix(f"{c}_")
                    new_thing.index = cool_events.index
                    if verbose:
                        print(f"{preamble2}Recoded to new variables {', '.join(new_thing.columns)}")

                    new_thing_cols = copy(new_thing.columns)
                    for new_thing_c in new_thing_cols:
                        if not new_thing_c in var_schema.index or var_schema.loc[new_thing_c, "role"] == "skip":
                            if verbose:
                                print(f"{preamble2}Skipping new variable: {new_thing_c}")
                            new_thing = new_thing.drop(columns=new_thing_c)

                    # drop the original column or not
                    if var_schema.loc[c,"role"] == "raw":
                        cool_events = pd.concat([cool_events.drop(columns=[c]), new_thing], axis=1)
                    else:
                        cool_events = pd.concat([cool_events, new_thing], axis=1)
            else:
                if verbose:
                    print(f"{preamble}Skipping")
                cool_events = cool_events.drop(columns=[c]).copy()
        else:
            if verbose:
                print(f"{preamble}Not found in the variable scheme, skipping")
            cool_events = cool_events.drop(columns=[c]).copy()

    cool_events = convert_dtypes_to_pyarrow(cool_events, verbose=verbose)

    
    print(f"...done recoding variables at {datetime.now()}")

    return cool_events 









