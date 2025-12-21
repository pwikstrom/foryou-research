



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




def _try_eval(s):
    try:
        return eval(s)
    except:
        return s



def _is_emoji(s: str) -> bool:
    from emoji import EMOJI_DATA

    """Return True if the string is a valid emoji (including multi-char ones)."""
    return s in EMOJI_DATA




def get_factors_and_features_from_var_scheme(cf = None, some_events_df = None, verbose = False):
    import pandas as pd
    from os.path import join
    from fyp.fyp_main import init_config

    if cf is None:
        cf = init_config()
    
    var_scheme = cf["var_scheme"]
    
    the_factors = list(set(var_scheme[var_scheme["role"].isin(['factor','group_factor'])].variable_name) & set(some_events_df.columns))
    the_features = list(set(var_scheme[var_scheme["role"]=='feature'].variable_name) & set(some_events_df.columns))

    if verbose:
        print("Factors:",", ".join(the_factors))
        print("Features:",", ".join(the_features))

    return the_factors, the_features







def recode_descriptions(
    a_description : str, 
    recoding_policy : dict = {}):
    """
    Extract hashtags, mentions, and other words from a description string.

    Parameters
    ----------
    a_description : str
        The description text to parse.

    Returns
    -------
    dict
        Dictionary with keys:
            - 'hashtags': list of cleaned hashtags (without '#')
            - 'mentions': list of cleaned mentions (without '@')
            - 'not_hashtags': list of other cleaned words
    """
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
    #return words
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
    a_text : str, 
    recoding_policy : dict = {}):
    """
    Extract words from a string.

    Parameters
    ----------
    a_description : str
        The text to parse.

    Returns
    -------
    dict
        Dictionary with keys:
            - 'words': list of cleaned words
    """
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
    a_string: str, 
    recoding_policy : dict = {}) -> float | None:
    """
    Extracts the percentage of speech from a string formatted like "50% speech, 50% music".
    Returns the fraction as a float between 0 and 1, or None if parsing fails.

    Parameters
    ----------
    a_string : str
        Input string, e.g. "50% speech, 50% music".

    Returns
    -------
    float or None
        Fraction of speech (0-1), or None if not parseable.
    """

    from numpy import array



    if not isinstance(a_string, str):
        return a_string

    some_list = a_string.split(",")
    some_list_check = [[1 * ("speech" in h), 1 * ("music" in h)] for h in some_list]
    if len(some_list) == 2 and all(array(some_list_check).sum(axis=0) == 1):
        try:
            some_list = [{h.split("%")[1].strip(): int(h.split("%")[0])} for h in some_list]
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
    a_string: str, 
    recoding_policy : dict = {}) -> int | None:
    """
    takes a string of this template: "<numeral><, ><text>" and returns the numeral split by 100
    It assumes that the stringified numeral is ranging between 0-100 so it splits it by 100
    to return a float between 0 and 1
    """

    from numpy import nan as np_nan


    if isinstance(a_string,str):
        the_val = a_string.split(", ")[0]
        try:
            the_val = int(the_val)
            return the_val / 100
        except:
            return np_nan
    else:
        return a_string




def recode_long_strings(
    
    s: str | list, 
    recoding_policy):

    from copy import copy

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

    from numpy import nan as np_nan



    if not isinstance(a_string,str):
        return {"valence":np_nan,"energy":np_nan}

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
    an_age_range_list: str, 
    recoding_policy : dict = {}) -> float:

    from pandas import isna
    from numpy import nan as np_nan, mean as np_mean


    def single_age_range_str_to_float(an_age_range: str) -> float:
        if isna(an_age_range):
            return np_nan

        try:
            return float(an_age_range)
        except:
            pass

        if isinstance(an_age_range,str) and an_age_range.count("-")==1:
            try:
                age_limits = [int(i) for i in an_age_range.split("-")]
                if age_limits[1]<age_limits[0]:
                    return np_nan
                return float(np_mean(age_limits))
            except:
                return np_nan
        return np_nan

    if isinstance(an_age_range_list,str):
        return np_mean(list(map(single_age_range_str_to_float, an_age_range_list.split(" | "))))
    else:
        return an_age_range_list




def recode_challenges(
    challenges : str,
    recoding_policy : dict = {}):
    """
    Split the 'challenges' string into a list of cleaned challenge names.
    Each string of challenges from Zeeschuimer is separated by " | ". 
    Strips extra spaces and ignores empty entries.
    If the value is not a string, returns an empty list.

    Parameters
    ----------
    challenges : str or any
        The string containing challenges separated by ' | ', or another 'splitter'.

    Returns
    -------
    list
        List of cleaned challenge names, or an empty list if input is not a string.
    """
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
    fine_actitivies_string : str, 
    recoding_policy : dict = {}):
    #recoding_policies_most_vars["G_main_activity"]

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
    timestamp, 
    recoding_policy : dict = {}):
    return int(timestamp.timestamp())
    


def recode_stringified_list(
    a_string_representing_a_list, 
    recoding_policy
    ) -> list:

    from pandas import isna


    no_data_fallback = UNABLE_TO_DETECT 
    ignore_strings = recoding_policy["ignore_strings"]
    splitter = recoding_policy["splitter"]
    mapper = recoding_policy["mapper"]


    mini_mapper = {1: "yes", 0: "no", True: "yes", False: "no"}


    list_of_the_words = [] # i know this is a stupid variable name

    # if the string that is representing a list is na, assume that it hasn't been coded
    if isna(a_string_representing_a_list):
        list_of_the_words += [NOT_CODED]

    # if there is a string, but the length is zero - assume it was coded but nothing was found
    # for instance, no people in the video present, which makes it irrelevant to talk about
    # the ages or genders of the faces in the video. Or there are people in the video but it was
    # not possible to detect their age. 
    elif len(str(a_string_representing_a_list)) < 1 or str(a_string_representing_a_list) in ["-"," "]:
        list_of_the_words += [no_data_fallback]

    else:
        # there are some cases where the expected string is not a string but an int {0/1} or a bool {True/False}
        # so we need to map it to the expected string format
        a_string_representing_a_list = mini_mapper.get(a_string_representing_a_list,a_string_representing_a_list)

        # most of the strings represent lists where the elements are separated with some set of characters ("splitter")
        # but the code can deal with strings without such characters as well. It just treats them as single-element lists.
        # If the splitter is a space, it can be used to filter normal sentences and so on... 
        if not isna(splitter):
            for an_element in a_string_representing_a_list.lower().split(splitter):
                if len(an_element)>0:
                    an_element = an_element.replace("//", "").replace("&", " and ").replace("/", " or ")
                    #if (len(an_element)>1 and not an_element in ignore_strings) or _is_emoji(an_element):
                    #    clean_word = [mapper.get(an_element.lower(),an_element.lower())]
                    #else:
                    #    clean_word = an_element
                    clean_word = "".join([j for j in an_element.lower() if not j in ",.:;!)(*/&|^%$#@<>?'`’1234567890"])
                    if (len(clean_word)>1 and not clean_word in ignore_strings)  or _is_emoji(clean_word):
                        list_of_the_words += [mapper.get(clean_word,clean_word)]
        else:
            pass
            #a_string_representing_a_list
        
    if len(list_of_the_words) == 0:
        list_of_the_words += [no_data_fallback]

    return list_of_the_words




def implement_missing_data_policy(x, missing_data_policy, the_median=0):

    from pandas import isna
    from numpy import nan as np_nan


    if (isinstance(x,list) and len(x)==1 and x[0]==NOT_CODED) or (isinstance(x,str) and x==NOT_CODED) or ((not isinstance(x,list)) and isna(x)):
        if missing_data_policy == "empty":
            return []
        elif missing_data_policy == "drop":
            return np_nan
        elif missing_data_policy == "median":
            return the_median
        elif missing_data_policy == "keep":
            if isna(x):
                return [NOT_CODED]
            else:
                return x
        elif missing_data_policy == "zero":
            gg = x if not isinstance(x,list) else x[0]
            if isinstance(gg,(int,float)):
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
    from pandas import isna
    from numpy import nan as np_nan

    if (isinstance(x,list) and len(x)==1 and x[0]==UNABLE_TO_DETECT) or (isinstance(x,str) and x==UNABLE_TO_DETECT) or ((not isinstance(x,list)) and isna(x)):
        if unable_to_detect_policy == "empty":
            return []
        elif unable_to_detect_policy == "drop":
            return np_nan
        elif unable_to_detect_policy == "median":
            return the_median
        elif unable_to_detect_policy == "keep":
            if isna(x):
                return [UNABLE_TO_DETECT]
            else:
                return x
        elif unable_to_detect_policy == "zero":
            gg = x if not isinstance(x,list) else x[0]
            if isinstance(gg,(int,float)):
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



def recode_events_df(
    cf = None,
    study_name = None,
    cool_events_in = None,
    verbose = False,
    save_it = True):

    import pandas as pd
    from os.path import join, getctime, exists
    from datetime import datetime
    from copy import copy
    from fyp.fyp_main import init_config
    import fyp.data_io as data_io

    if cf is None:
        cf = init_config()
    
    if study_name is None:
        raise ValueError("study_name must be specified")
    
    file_format = cf['misc']['file_format']

    print(f"Recoding variables, implementing missing data policy and a whole range of other things: Study:{study_name}")


    if cool_events_in is None:
        log_path = join(cf['paths']['exports'],f"{study_name}_LOG{file_format}")
        if exists(log_path):
            nice_time = datetime.fromtimestamp(getctime(log_path)).strftime('%Y-%m-%d %H:%M:%S')
            print(f"Loading events file in export folder, created at: {nice_time}", end=" ", flush=True)
            cool_events_in = data_io.load_dataset(log_path)
            print(f"Shape: {cool_events_in.shape}")
        else:
            print("This process required a LOG file to be generated first. Log file not found at: ", log_path)
            return None


    cool_events = cool_events_in.copy()

    var_scheme = cf["var_scheme"].copy()

    var_scheme.set_index("variable_name", inplace=True)

    var_scheme[['mapper','ignore_strings','recode_func']] = var_scheme[['mapper','ignore_strings','recode_func']].map(_try_eval)

    FYP_FACTORS = list(set(var_scheme[var_scheme["scale"].isin(['factor','group_factor'])].index) & set(cool_events.columns))
    cool_events[FYP_FACTORS] = cool_events[FYP_FACTORS].astype(str)
    cool_events["session_id"] = cool_events["session_id"].map(lambda x:f"S{int(x):05}")

    variables_not_found_in_var_scheme = list(set(cool_events.columns) - set(var_scheme.index))
    if verbose:
        join_str = "\n - "
        print(f"Dropping {len(variables_not_found_in_var_scheme)} columns not found in the variable scheme:\n - {join_str.join(variables_not_found_in_var_scheme)}")
    cool_events = cool_events.drop(columns=variables_not_found_in_var_scheme).copy()
    if verbose:
        print(cool_events.shape)

    single_value_columns = [c for c in cool_events.columns if cool_events[c].nunique()==1 and c not in FYP_FACTORS]
    if verbose:
        join_str = "\n - "
        print(f"Dropping {len(single_value_columns)} single value columns:\n - {join_str.join(single_value_columns)}")
    cool_events = cool_events.drop(columns=single_value_columns).copy()
    if verbose:
        print(cool_events.shape)

    if verbose:
        print("Recoding variables")
    
    cool_columns = copy(cool_events.columns)
    # iterate over the columns in the events df
    for c in cool_columns:
        if verbose:
            print(c, end=f"{' '*(40-len(c))}")

        # if this is in the var_scheme...
        if c in var_scheme.index:
            this_var_scheme = var_scheme.loc[c].to_dict() 

            if this_var_scheme.get("role", "undefined") != "skip":

                if this_var_scheme.get("scale", "undefined") == "raw":
                    if c+"_raw" in var_scheme.index:
                        if verbose:
                            print(f"Copied raw variable: {c}")
                        cool_events[c+"_raw"] = cool_events[c].copy()

                # check outcomes that should be single values and pop the value out of entries that happen to be single element lists, e.g. ["yes"] -> "yes"
                cool_types = cool_events[c].dropna().map(lambda x:type(x)).value_counts()
                top_type = cool_types.index[0]
                n_types = len(cool_types)

                if n_types > 1 and top_type == list:
                    cool_events[c].map(lambda x:x if isinstance(x,list) else [x])
                elif n_types > 1 and top_type == str:
                    cool_events[c].map(lambda x:x if isinstance(x,str) else x[0])
                if n_types > 1:
                    raise ValueError(f" has {n_types} multiple types of values. Only a single type is allowed. {cool_types.to_dict()}")


                if not pd.isna(this_var_scheme.get("recode_func", None)):
                    cool_events[c] = cool_events[c].map(lambda x:this_var_scheme["recode_func"](x,this_var_scheme))
                    if verbose: print(f"recoded successfully ({this_var_scheme.get('scale', 'unknown scale')})")
                else:
                    if verbose: print(f"has no recode func, so no change ({this_var_scheme.get('scale', 'unknown scale')})")


                # implement missing data and unable to detect policies
                if (this_var_scheme.get('unable_to_detect_policy', 'unknown') == "median") or (this_var_scheme.get('missing_data_policy', 'unknown') == "median"):
                    a_fine_median = cool_events[c].median()
                else:
                    a_fine_median = None

                cool_events[c] = cool_events[c].map(lambda x:implement_unable_to_detect_policy(
                    x,
                    this_var_scheme.get("unable_to_detect_policy","No policy"),
                    a_fine_median))

                cool_events[c] = cool_events[c].map(lambda x:implement_missing_data_policy(
                    x,
                    this_var_scheme.get("missing_data_policy","No policy"),
                    a_fine_median))


                cool_types = cool_events[c].dropna().map(lambda x:type(x)).value_counts()
                top_type = cool_types.index[0]
                n_types = len(cool_types)



                if (this_var_scheme["scale"] in ["categorical","dichotomous","ordinal","ratio","interval"]) and top_type == list:
                    these_rows_have_multiple_values = cool_events[c].map(lambda x: isinstance(x,list) and len(x)>1)
                    if these_rows_have_multiple_values.sum() > 0:
                        print(f"{c} has {these_rows_have_multiple_values.sum()} values with more than one entry.")
                        raise ValueError(f"{c} has {these_rows_have_multiple_values.sum()} values with more than one entry. Only a single value is allowed for categorical, dichotomous, ordinal, ratio, and interval variables.")

                    cool_events.loc[(~these_rows_have_multiple_values).index, c] = cool_events.loc[(~these_rows_have_multiple_values).index, c].map(lambda x:x[0] if not pd.isna(x) else x)



                # for dichotomous variables, I only accept "yes" and "no" as values 
                if (this_var_scheme["scale"] in ["dichotomous"]):

                    if not set(cool_events[c].dropna().unique()) | {'yes','no'} == {'yes','no'}:
                        raise ValueError(f"{c} is a dichotomous variable. Only 'yes', 'no' are accepted values. {c} has {cool_events[c].dropna().unique()}")
                    

                # for dict variables, I unpack the dicts into separate columns
                if top_type == dict:
                    new_thing = pd.json_normalize(cool_events[c])
                    new_thing = new_thing.add_prefix(f"{c}_")
                    new_thing.index = cool_events.index
                    if verbose:
                        print(f"   - {c} recoded to new variables {', '.join(new_thing.columns)}")

                    new_thing_cols = copy(new_thing.columns)
                    for new_thing_c in new_thing_cols:
                        if not new_thing_c in var_scheme.index or var_scheme.loc[new_thing_c, "role"] == "skip":
                            if verbose:
                                print(f"   - Skipping {new_thing_c}")
                            new_thing = new_thing.drop(columns=new_thing_c)

                    # drop the original column or not
                    if var_scheme.loc[c,"role"] == "raw":
                        cool_events = pd.concat([cool_events.drop(columns=[c]), new_thing], axis=1)
                    else:
                        cool_events = pd.concat([cool_events, new_thing], axis=1)
            else:
                if verbose:
                    print(f"Skipping {c}")
                cool_events = cool_events.drop(columns=[c]).copy()
        else:
            if verbose:
                print(f"not found in the variable scheme, skipping")
            cool_events = cool_events.drop(columns=[c]).copy()


    cool_events['plays_per_day'] = cool_events['S_stats_playCount'] / cool_events['T_days_since_created'].map(lambda x:max(1,x))


    # Clean the recoded dataset - drop rows with NaN values and constant columns
    if verbose:
        print(cool_events.shape, "shape of recoded dataset before cleaning")
    cool_events = cool_events.loc[~cool_events.isna().any(axis=1)]                      # drop rows with NaN values
    if verbose:
        print(cool_events.shape, "shape of recoded dataset after dropping rows with NaN values")
        print("----------------------------------------------")


    if verbose:
        print("Cleaning up Gemini annotations - replacing categories/labels that are very rare")
        print(f"Shape of the events DF: {cool_events.shape}")
    cool_events = clean_up_machine_annotations(cool_events, verbose = verbose)
    if verbose:
        print(f"Shape of the events DF: {cool_events.shape}")
        print(cool_events.shape)

    cool_events = cool_events[sorted(cool_events.columns)]


    if save_it:
        recoded_filename = f"{study_name}_RECODED{file_format}"
        export_sub_folder_name = cf["paths"]["exports"].replace(cf["paths"]["main"],"")
        data_io.save_dataset(cool_events, join(cf['paths']['exports'],recoded_filename))
        print(f"Exported {len(cool_events):,} events in {join(export_sub_folder_name,recoded_filename)}.")
    
    print(f"Now: {datetime.now()}")
    #print("--"*60)

    return cool_events 




def _flatten_and_filter(items, exclude = []):
    """
    items: list containing strings and/or lists of strings
    exclude: set or list of strings to remove
    """
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
    """
    tuples_list: list of (item, count), sorted descending by count.
    share: float in (0,1], cumulative proportion required.
    min_count: minimum number of tuples to return.
    """

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
    """
    L: list containing strings and/or sublists of strings
    filter_list: list of strings to keep
    replacement: string to use as substitute if not in filter
    
    Returns a new list with identical structure,
    replacing any matching strings.
    """
    filt = set(filter_list)  # faster lookups

    out = []
    append = out.append

    for x in L:
        if isinstance(x, list):
            # preserve nested list shape
            sub = []
            sub_append = sub.append
            for y in x:
                if not y in filt:
                    sub_append(replacement)
                else:
                    sub_append(y)
            append(sub)
        else:
            append(replacement if not x in filt else x)

    return out


def clean_up_machine_annotations(some_events, verbose = False):
    
    from collections import Counter
    import numpy as np 

    some_cleaned_up_events = some_events.copy()

    # iterate over all object type columns in the events DF that starts w G_, i.e. are machine annotations
    for c in [k for k in some_events.select_dtypes(object).columns if k.startswith("G_")]:

        # Step 1 of 3: Flatten and filter the column
        flattened_column = _flatten_and_filter(some_events[c], exclude=["DDP","BASELINE", "unable to detect", "", OTHER_THINGS])

        mean_length = np.mean(list(map(lambda x:len(x), flattened_column)))

        if mean_length < 60:

            # Step 2 of 3: Identify the smallest number of labels required to cover at least a certain share of the label space
            label_counts = Counter(flattened_column).most_common() # a list of tuples (label, count), ordered desc based on count
            okay_list = [i[0] for i in _cutoff_by_share(label_counts, 0.98, 3)]

            # replace the smallest labels with an OTHER_THINGS label
            some_cleaned_up_events[c] = _replace_in_structure(
                some_events[c],
                ["DDP","BASELINE", "unable to detect", "", OTHER_THINGS] + okay_list,
                OTHER_THINGS
            )

            if verbose:
                print(
                    f"   {c}: Reduced {len(Counter(_flatten_and_filter(some_events[c])).most_common()):,} labels to"
                    f" {len(Counter(_flatten_and_filter(some_cleaned_up_events[c])).most_common()):,}"
                )
        else:
            if verbose:
                print(f"Avg string length > 60, not consolidating rare labels {c}")

    return some_cleaned_up_events
