


import pandas as pd
from datetime import datetime
from copy import copy
import numpy as np

from fyp.fyp_main import initialize, convert_dtypes_to_pyarrow
import fyp.data_io as data_io


NOT_CODED = "not coded"
UNABLE_TO_DETECT = "unable to detect"
OTHER_THINGS = "oThEr tHiNgS-+-"




POSITIVE_WORDS = ["positive"]
NEGATIVE_WORDS = ["negative"]
HIGH_ENERGY_WORDS = ["highenergy","reflective","playful"]
LOW_ENERGY_WORDS = ["lowenergy","calm","serene"]




CONTENT_CATEGORIES_FROM_CODEBOOK = ['comedy', 'daily life', 'film  and  tv', 'performance', 'drama',
       'art  and  creativity', 'society', 'news',
       'interpersonal relationships', 'technology design  and  reviews',
       'education', 'finance', 'diy  and  life hacks',
       'mental health  and  wellbeing', 'fashion  and  beauty', 'food',
       'travel', 'games', 'sports', 'animals', 
       'fitness  and  physical health',
       'anime  and  comics']




IRRELEVANT_WORDS = ['-year-old', 'about', 'actually', 'ad', 'add', 'after', 'all', 'also', 'always', 'am', 'an', 'and', 'any', 'are', 'as', 'at', 'back', 'be', 
'been', 'before', 'being', 'best', 'better', 'bio', 'blowthisup', 'but', 'by', 'can', 'cant', 'come', 'comment', 'could', 'day', 'de', 
'dear', 'did', 'didnt', 'do', 'doesnt', 'dont', 'during', 'edit', 'el', 'end', 'even', 'ever', 'every', 'evryone', 'everyone', 'find', 'first', 'follow', 
'for', 'foryou', 'foryoupage', 'foryourpage', 'free', 'from', 'full', 'funny', 'funnyvideos', 'fy', 'fyp', 'fypage', 'fyppppppppppppppppppppppp', 'fypシ', 
'fypシ゚viral', 'get', 'give', 'go', 'going', 'good', 'got', 'goviral', 'had', 'has', 'have', 'having', 'he', 'her', 'here', 'him', 'his', 'how', 
'ib', 'if', 'ill', 'im', 'in', 'into', 'is', 'it', 'its', 'ive', 'just', 'keep', 'know', 'last', 'left', 'let', 'lets', 'life', 'like', 'link', 
'look', 'love', 'made', 'make', 'many', 'may', 'me', 'meme', 'more', 'most', 'much', 'my', 'nan', 'need', 'never', 'new', 'next', 'no', 'not', 
'now', 'of', 'off', 'on', 'one', 'only', 'or', 'our', 'out', 'over', 'part', 'people', 'please', 'png', 'post', 'pov', 'real', 'replying', 
'right', 'room', 'run', 'say', 'see', 'she', 'should', 'so', 'some', 'someone', 'something', 'start', 'stay', 'still', 'stop', 'sure', 'take', 
'tbsp', 'text', 'than', 'thank', 'that', 'thats', 'the', 'their', 'them', 'then', 'there', 'these', 'they', 'things', 'think', 'this', 'tho', 
'through', 'tiktok', 'time', 'to', 'today', 'too', 'trend', 'trending', 'try', 'tsp', 'two', 'up', 'ur', 'us', 'use', 'various', 'very', 'vid', 
'video', 'viral', 'viralvideo', 'vs', 'want', 'wanted', 'was', 'way', 'we', 'were', 'what', 'whats', 'when', 'which', 'while', 'who', 'why', 
'will', 'with', 'without', 'wont', 'wontbeen', 'would', 'xyzbca', 'you', 'your', 'youre']



GENERIC_MAPPER = {
    "" : UNABLE_TO_DETECT,
    "-" : UNABLE_TO_DETECT,
    "/" : "or",
    "&" : "and",
    "none": UNABLE_TO_DETECT,
    "multiple" : UNABLE_TO_DETECT,
    "mixed" : UNABLE_TO_DETECT,
    "undefined" : UNABLE_TO_DETECT,
    "mixed-race" : UNABLE_TO_DETECT,
    "polynesian" : "pacific islander",
    "middle eastern or caucasian" : "middle eastern",
    "middle eastern or south asian" : "middle eastern",
    "polynesian or pacific islander" : "pacific islander",
    "southeast asian or pacific islander" : "pacific islander",
    "indigenous australian or pacific islander": "indigenous australian",
    "hispanic or latina" : "latinx",
    "hispanic or latino" : "latinx",
    "hispanic" : "latinx",
    "latino" : "latinx",
    "latina" : "latinx",
    "indigenous" : "indigenous australian",
    "no clear positioning":UNABLE_TO_DETECT,
    "no clear position":UNABLE_TO_DETECT,
}







def rename_columns(
    some_events
    ):
    
    some_eventsC = some_events.copy()

    fixer_upper = [
        ("B_local_","T_local_"),
        ("D_local_","T_local_"),
        (".","_"),
        ("data_",""),
        ("source_url_","source_"),
        ("_collected",""),
        ("framing_analysis_","FA_"),
        ("cultural_representation_analysis_","CRA_"),
        ("ideological_analysis_","IA_"),

        ]

    #from pandas import set_option
    pd.set_option('future.no_silent_downcasting', True)

    for fu in fixer_upper:
        mapper = {c:c.replace(fu[0],fu[1]) for c in some_eventsC.columns if (c != c.replace(fu[0],fu[1])) and (not c.replace(fu[0],fu[1]) in some_eventsC.columns)}
        some_eventsC = some_eventsC.rename(columns=mapper).copy()
    
    return some_eventsC











def extract_local_time_features(
    cf = None,
    some_events_df_in = None,
    kind_of_log = None,
    verbose = False):
    """
    Integrates per-donation timezone offsets from persona_stats_cache.
    """


    if cf is None:
        cf = initialize()



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

        unique_tz = df[tz_col].dropna().unique()
        if len(unique_tz) == 1:
            # Fast path: everything in same tz
            tz = ZoneInfo(unique_tz[0])
            df["local_timestamp"] = df[ts_col].dt.tz_localize(tz)
        else:
            # Slower path: per-timezone blocks
            local_parts = []
            for tz_name, block in df.groupby(tz_col, sort=False):
                tz = ZoneInfo(tz_name)
                part = block[ts_col].dt.tz_localize(tz)
                local_parts.append(part)
            df["local_timestamp"] = concat(local_parts).sort_index()

        df = df.drop(columns=[ts_col])
        
        # Convert baseline timestamps to naive local (remove timezone info but keep local wall clock)
        # This aligns with the new DDP strategy below
        df["local_timestamp"] = df["local_timestamp"].dt.tz_localize(None)

    elif kind_of_log == "ddp":
        # the 'ddp' timestamp is utc

        # Build item_id if missing
        if "item_id" not in df.columns:
            print("WARNING: item_id not found in ddp events df. Building it now...")
            # rsplit is cheaper than full split, only looks from the right
            extracted = (
                df["primary_value"]
                .astype("string")
                .str.rsplit("/", n=2)
                .str[-2]
            )

            # SAFE INTEGER PARSING: avoid float64 / to_numeric
            # keep only pure digit strings, everything else -> <NA>
            digits = extracted.str.fullmatch(r"\d+")
            ints = extracted.where(digits).astype("string[pyarrow]")

            mask = (
                df["primary_label"].eq("link")
                & df["feature_name"].notna()
            )
            df["item_id"] = ints.where(mask)
            # later we will convert it to string. One day I will make this more efficient.


        # normalise timestamp column name
        if "utc_timestamp" not in df.columns:
            print("WARNING: utc_timestamp not found in ddp events df. Renaming timestamp to utc_timestamp now...")
            df = df.rename(columns={"timestamp": "utc_timestamp"})


        # NEW LOGIC: Use per-donation timezone offsets
        
        # 1. Calculate Default Offset from static TIME_ZONE (as fallback)
        time_zone = cf["misc"]["TIME_ZONE"]
        try:
            now_local = datetime.now(ZoneInfo(time_zone))
            default_offset_hours = now_local.utcoffset().total_seconds() / 3600.0
        except Exception as e:
            if verbose: print(f"Warning: Could not determine offset for {time_zone}, defaulting to 0. {e}")
            default_offset_hours = 0.0
            
        # 2. Load Offsets from Cache
        stats_cache_path = "persona_stats_cache.parquet"
        
        df['tz_offset_hours'] = default_offset_hours # Initialize with default
        
        if data_io.exists(cf, "ddp_main", stats_cache_path):
            try:
                # Load only necessary columns
                stats_df = data_io.load_parquet(cf, "ddp_main", stats_cache_path, columns=['donation_id', 'inferred_tz_offset'])
                
                # Map offsets to main df
                # stats_df needs unique donation_ids. It should be unique per previous logic.
                if not stats_df['donation_id'].is_unique:
                    stats_df = stats_df.drop_duplicates(subset=['donation_id'])
                    
                offset_map = stats_df.set_index('donation_id')['inferred_tz_offset']
                
                # Map using donation_id column in df
                if 'donation_id' in df.columns:
                    mapped_offsets = df['donation_id'].map(offset_map)
                    # Update where not null
                    df.loc[mapped_offsets.notna(), 'tz_offset_hours'] = mapped_offsets[mapped_offsets.notna()]
                    if verbose:
                        print(f"Applied individual timezones to {mapped_offsets.notna().sum():,} events.")
                else:
                    if verbose: print("Warning: donation_id column missing, using default timezone.")
                    
            except Exception as e:
                print(f"Warning: Failed to load/apply timezone cache: {e}. Using default {TIME_ZONE}")
        else:
            if verbose: print("Timezone cache not found. Using default study timezone.")

        # 3. Calculate Local Timestamp (Naive Wall Clock)
        
        # Ensure utc_timestamp is numeric (seconds)
        utc_seconds = df["utc_timestamp"].astype("float64")
        
        # Add offset (hours * 3600)
        local_seconds = utc_seconds + (df['tz_offset_hours'] * 3600.0)
        
        # Convert to Naive Datetime
        df["local_timestamp"] = to_datetime(local_seconds, unit='s', utc=False)
        
        # Cleanup temp column
        df = df.drop(columns=['tz_offset_hours'], errors='ignore')
        
        # Pyarrow conversion
        df["local_timestamp"] = df["local_timestamp"].convert_dtypes(dtype_backend="pyarrow")


    else:
        raise ValueError("kind_of_log can only be 'baseline' or 'ddp'")

    # ---------------------------------------------------------------------
    # 2. Derive local time features
    # ---------------------------------------------------------------------
    ts = df["local_timestamp"]
    
    # Check if we still have timezone awareness (should be none for DDP, but maybe for baseline if not stripped)
    # The new logic strips it for baseline too.
    
    # If stored as object dtype, force conversion (should handle naive correctly)
    if ts.dtype == 'object':
        df["local_timestamp"] = to_datetime(ts)
        ts = df["local_timestamp"]

    iso = ts.dt.isocalendar()  # DataFrame: year, week, day
    iso["day"] = iso["day"].map(WEEKDAY_MAPPER)
    iso["year_week"] = iso["year"].astype("uint16").astype("string[pyarrow]") + "-" + iso["week"].astype("uint8").astype("string[pyarrow]")

    df["local_weekday"] = iso["day"].to_list()
    df["local_week"] = iso["year_week"].to_list()

    df["local_hour"] = ts.dt.hour.astype("uint8")

    df["local_day_segment"] = df["local_hour"].map(_day_segment_from_hour).astype("string[pyarrow]")

    # Optimization: Use .dt.date directly (faster than map)
    df["local_date"] = ts.dt.date.astype("timestamp[ns][pyarrow]")
    df["local_date_str"] = df["local_date"].astype("string[pyarrow]")

    if verbose:
        print("...done")

    return df











def _try_eval(s):
    try:
        return eval(s)
    except:
        return s



def _is_emoji(s: str) -> bool:
    from emoji import EMOJI_DATA

    """Return True if the string is a valid emoji (including multi-char ones)."""
    return s in EMOJI_DATA




def get_factors_and_features_from_var_schema(cf = None, some_events_df = None, verbose = False):
    #import pandas as pd
    #from os.path import join
    #from fyp.fyp_main import initialize

    if cf is None:
        cf = initialize()
    
    var_schema = cf["var_schema"]
    
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



def get_group_factors_from_var_schema(cf = None, some_events_df = None, verbose = False):
    #import pandas as pd
    #from os.path import join
    #from fyp.fyp_main import initialize

    if cf is None:
        cf = initialize()
    
    var_schema = cf["var_schema"]
    
    the_group_factors = sorted(list(set(var_schema[var_schema["role"]=='group_factor'].variable_name)))
    if some_events_df is not None:
        the_group_factors = [c for c in the_group_factors if c in some_events_df.columns]
    
    if verbose  and len(the_group_factors) > 0:
        print("    Group Factors:",", ".join(the_group_factors))

    return the_group_factors







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

    #from numpy import nan as np.nan
    #from pandas import NA as pd.NA



    if not isinstance(a_string,str):
        return {"valence":pd.NA,"energy":pd.NA}

    a_string = a_string.lower().replace("-","").replace(" ","")
    valence = 0
    for w in POSITIVE_WORDS:
        if w in a_string:
            valence = 1
    for w in NEGATIVE_WORDS:
        if w in a_string:
            valence = -1
    energy = 0
    for w in HIGH_ENERGY_WORDS:
        if w in a_string:
            energy = 1
    for w in LOW_ENERGY_WORDS:
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
    
    #import pandas as pd
    #import numpy as np
    #from pandas import NA as pd.NA

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
        
        extracted = fine_actitivies_string.astype(str).str.extract(r'\b([a-zA-Z]+ing)\b', expand=False)
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




def recode_timestamp(
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

    return np.int64(timestamp.timestamp())
    


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

    #import numpy as np
    #import pandas as pd
    #from pandas import isna, NA as pd.NA, Series
    #from numpy import int64, float64
    
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
    #import numpy as np
    #import pandas as pd
    #from pandas import isna, Series
    #from numpy import nan as np.nan, int64, float64
    #from pandas import NA as pd.NA

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




"""def _flatten_and_filter(items, exclude = []):
    ""-"
    items: list containing strings and/or lists of strings
    exclude: set or list of strings to remove
    "-""
    excl = set(exclude)
    out = []

    append = out.append  # local binding for speed

    for x in items:
        if isinstance(x, list):
            for y in x:
                if y not in excl:
                    append(y)
        else:
            if x not in excl:
                append(x)

    return out




def _cutoff_by_share(tuples_list, share, min_count=1):
    "-""
    tuples_list: list of (item, count), sorted descending by count.
    share: float in (0,1], cumulative proportion required.
    min_count: minimum number of tuples to return.
    "-""

    if not 0 < share <= 1:
        raise ValueError("share must be in (0,1].")

    n = len(tuples_list)
    if min_count > n:
        min_count = n

    total = sum(count for _, count in tuples_list)
    target = total * share

    out = []
    cum = 0
    append = out.append

    for idx, (item, count) in enumerate(tuples_list):
        append((item, count))
        cum += count

        # meet share AND minimum count
        if cum >= target and idx + 1 >= min_count:
            break

    return out




def _replace_in_structure(L, filter_list, replacement):
    "-""
    L: list containing strings and/or sublists of strings
    filter_list: list of strings to keep
    replacement: string to use as substitute if not in filter
    
    Returns a new list with identical structure,
    replacing any matching strings.
    "-""
    from pandas import Series
    filt = set(filter_list)  # faster lookups

    out = []
    append = out.append

    for x in L:
        if isinstance(x, list):
            # preserve nested list shape
            sub = []
            sub_append = sub.append
            for y in x:
                if y not in filt:
                    sub_append(replacement)
                else:
                    sub_append(y)
            append(sub)
        else:
            append(replacement if x not in filt else x)

    if hasattr(L, "dtype") and hasattr(L, "index"):
        return Series(out, index=L.index, dtype=L.dtype)

    return out"""






def clean_up_machine_annotations(some_events, verbose = False):
    
    #import pandas as pd
    #import numpy as np 
    #from pandas import Series
    #from datetime import datetime


    some_cleaned_up_events = some_events.copy()

    # iterate over all object type columns in the events DF that starts w G_, i.e. are machine annotations
    g_cols = [k for k in some_events.select_dtypes(exclude=["number"]).columns if k.startswith("G_")]
    
    exclude_set = {"DDP", "BASELINE", UNABLE_TO_DETECT, "", OTHER_THINGS}

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


        # Check mean length
        # Vectorized string length based on a sample of 500 items

        sample_size = min(500, len(valid_items))
        avg_len = valid_items.sample(sample_size, replace = False).astype(str).str.len().mean()
        
        if avg_len < 60:
            # Step 2: Cutoff logic
            # frequency of unique valid items
            counts = valid_items.value_counts()

            total_count = counts.sum()
            target = total_count * 0.98
            
            # cumulative sum
            cum_counts = counts.cumsum()
            
            # find how many labels needed to cross 98%
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
                if isinstance(x, list):
                    return [y if y in keep_set else OTHER_THINGS for y in x]
                if isinstance(x, str):
                    return x if x in keep_set else OTHER_THINGS
                return x # keep NA or other
                
            some_cleaned_up_events[c] = series.apply(_fast_replace)


            if verbose:
                # approximated stats
                print(f"    {c}: Cleaned up rare labels (kept top {num_keep})")

        else:
            if verbose:
                print(f"    {c}: Avg string length > 60, not consolidating rare labels")

    return some_cleaned_up_events






def recode_events_df(
    cf = None,
    study_name = None,
    study_dataset = None,
    drop_single_value_cols = True,
    verbose = False
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

    if study_name is None and study_dataset is None:
        print("  This process cannot run without a study name or a study dataset as input. Process failed.")
        return None


    if cf is None:
        cf = initialize()



    cool_events = study_dataset.copy()

    var_schema = cf["var_schema"].copy()

    var_schema.set_index("variable_name", inplace=True)

    var_schema[['mapper','ignore_strings','recode_func']] = var_schema[['mapper','ignore_strings','recode_func']].map(_try_eval)

    fyp_factors, _ = get_factors_and_features_from_var_schema(cf = cf, some_events_df = cool_events, verbose = verbose)


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
                         cool_events[c] = cool_events[c].map(lambda x: func(x, this_var_schema))
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











def recode_machine_annotations():
    #from fyp.fyp_main import initialize
    #import fyp.data_io as data_io
    #import fyp
    #import pandas as pd
    #from fyp.fyp_main import convert_dtypes_to_pyarrow

    cf = initialize()

    ma_df = data_io.load_parquet(cf=cf, storage_location="machine_annotations_refined", filename="*", verbose=True)

    ma_df = rename_columns(ma_df.rename(columns={c:"G_"+c if not c=="item_id" and not c.startswith("G_") else c for c in ma_df.columns})).copy()

    m1 = recode_events_df(
        cf = cf,
        study_dataset = ma_df,
        load_from_cache = False,
        save_to_cache = False,
        verbose = True
        )

    m1 = clean_up_machine_annotations(m1, verbose=True)

    m1["annotated_ok"] = ~m1.G_type_of_story.isna().astype("bool[pyarrow]")
    m1["annotated_fail"] = m1.G_type_of_story.isna().astype("bool[pyarrow]")

    m1 = convert_dtypes_to_pyarrow(m1, verbose=True)

    m1.reset_index(drop=True, inplace=True)

    m1.loc[m1[m1.annotated_fail].index,[c for c in m1.columns if c.startswith("G_")]] = pd.NA

    _ = data_io.save_parquet(cf, m1, "recoded", "annotations_recoded.parquet")







