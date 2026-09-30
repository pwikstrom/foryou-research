"""Turning raw model responses into flat annotation rows.

Tolerant JSON loading (fuzzy repair, unicode-escape decoding, collapsing
runaway repeats), flattening one response or a batch into columns, stripping
repetition loops from transcripts, and folding rare columns into their
closest common counterpart.
"""

import collections
import json
import re
from copy import copy, deepcopy

import fuzzy_json
import numpy as np
import pandas as pd

import fyp.core.utils as fyp_utils
from fyp.annotation.annotation_schema import (
    flatten_structured,
)
from fyp.core.logging_setup import get_logger
from fyp.core.types import scrub_surrogates_nested

logger = get_logger(__name__)

# *********************************************************************************************************
# Rare-column consolidation. Free-text (non-structured) responses, such as legacy
# raw batches written before structured output became the live path, can drift
# from the expected JSON shape and introduce stray keys. This pass merges
# sparsely populated stray columns back into the dominant column they were
# meant for; on schema-constrained structured output it normally finds nothing.
# *********************************************************************************************************


# Minimum difflib name-similarity required to merge a rare (<10% populated)
# column into a dominant (>90% populated) one. Genuine stray-key variants score
# ~0.85+, while unrelated pairs (e.g. a real column vs item_id) score <0.35, so
# 0.6 cleanly separates them and prevents collapsing mostly-failed batches.
RARE_COLUMN_MERGE_MIN_SIMILARITY = 0.6


def consolidate_rare_columns_from_gemini_output(
    outputs_from_machine_df_in, verbose=False, notebook_mode=False
):

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

    nonnull_ratio = (len(outputs_from_machine_df) - outputs_from_machine_df.isna().sum()) / len(
        outputs_from_machine_df
    )

    if notebook_mode:
        logger.info(outputs_from_machine_df.shape)
        logger.info(len(nonnull_ratio[nonnull_ratio < 0.1]))
        logger.info(nonnull_ratio[nonnull_ratio < 0.1])
        logger.info(len(nonnull_ratio[nonnull_ratio < 0.5]))
        logger.info(nonnull_ratio[nonnull_ratio < 0.5])
        logger.info(len(nonnull_ratio[nonnull_ratio < 0.8]))
        logger.info(nonnull_ratio[nonnull_ratio < 0.8])

    nonnull_ratio = (len(outputs_from_machine_df) - outputs_from_machine_df.isna().sum()) / len(
        outputs_from_machine_df
    )

    little_counter = 0

    while (len(nonnull_ratio[nonnull_ratio < 0.1])) > 0 and little_counter < 5:
        if verbose:
            logger.info(little_counter)
            logger.info(len(nonnull_ratio[nonnull_ratio < 0.1]))

        for unusual_col_name in nonnull_ratio[nonnull_ratio < 0.1].index:
            try:
                if verbose:
                    logger.info(
                        len(outputs_from_machine_df)
                        - outputs_from_machine_df[unusual_col_name].isna().sum()
                    )
                dominant_col_name, similarity = fyp_utils.best_similarity_match(
                    unusual_col_name, nonnull_ratio[nonnull_ratio > 0.9].index
                )

                # Only merge a rare column into a dominant one when their names are
                # genuinely similar (a stray-key variant). Without this guard a
                # mostly-failed batch — where the only well-populated column is
                # item_id — merges every real column into item_id and clears the
                # values, collapsing the batch to item_id alone.
                if dominant_col_name is not None and similarity >= RARE_COLUMN_MERGE_MIN_SIMILARITY:
                    rows_w_nonnull_value_in_unusual_col = outputs_from_machine_df[
                        ~outputs_from_machine_df[unusual_col_name].isna()
                    ].loc[:, [dominant_col_name, unusual_col_name]]

                    for ii in rows_w_nonnull_value_in_unusual_col.index:
                        if outputs_from_machine_df.loc[ii, dominant_col_name] is np.nan:
                            if verbose:
                                logger.info(f"******* {ii} {dominant_col_name}")
                            outputs_from_machine_df.loc[ii, dominant_col_name] = (
                                outputs_from_machine_df.loc[ii, unusual_col_name]
                            )
                        else:
                            outputs_from_machine_df.loc[ii, unusual_col_name] = np.nan
            except KeyError:
                if verbose:
                    logger.error(f"ERROR: {unusual_col_name} doesn't seem to be among the columns")

            little_counter += 1

        nonnull_ratio = (len(outputs_from_machine_df) - outputs_from_machine_df.isna().sum()) / len(
            outputs_from_machine_df
        )
        outputs_from_machine_df.drop(
            nonnull_ratio[nonnull_ratio == 0].index, axis=1, inplace=True, errors="ignore"
        )
        if verbose:
            logger.info(outputs_from_machine_df.shape)
            logger.info("------------------------------------------------------")

    return outputs_from_machine_df


# Functions in this section flatten and transform raw output JSON into a dataframe.
# The main entry point is at the bottom of the section.
def flatten_one_machine_response(some_response, verbose=False, notebook_mode=False):

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
            logger.info(type(some_response))
        return some_response

    flat_response = deepcopy(some_response)

    # #######################
    # scenes
    if "scenes" in flat_response:
        if isinstance(flat_response["scenes"], str):
            flat_response["scenes"] = re.sub(
                r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response["scenes"]
            )
            try:
                flat_response["scenes"] = fuzzy_json.loads(flat_response["scenes"])
            except Exception:
                return None
        if isinstance(flat_response["scenes"], list):
            try:
                description_list = []
                sentiment_list = []
                for k in flat_response["scenes"]:
                    if isinstance(k, dict):
                        description_list += [k.get("description", "")]
                        sentiment_list += [k.get("sentiment", "")]
                flat_response["scenes"] = " | ".join(description_list)
                tt1 = collections.Counter(sentiment_list).most_common(1)
                if len(tt1) == 0:
                    flat_response["scene_sentiments"] = ""
                else:
                    flat_response["scene_sentiments"] = tt1[0][0]
            except Exception:
                return None
        else:
            return None

    # #######################
    # transcript
    if "transcript" in flat_response:
        if isinstance(flat_response["transcript"], str):
            flat_response["transcript"] = re.sub(
                r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response["transcript"]
            )
            try:
                flat_response["transcript"] = fuzzy_json.loads(flat_response["transcript"])
            except Exception:
                return None
        if isinstance(flat_response["transcript"], list):
            try:
                text_list = []
                for k in flat_response["transcript"]:
                    if isinstance(k, dict):
                        text_list += [k.get("text", "")]
                    elif isinstance(k, str):
                        text_list += [k]
                flat_response["transcript"] = " | ".join(text_list)
            except Exception:
                return None
        else:
            return None

    # #######################
    # objects
    for res_key in ["objects", "symbols_and_brands", "text_overlays", "content_category"]:
        if res_key in flat_response:
            if isinstance(flat_response[res_key], str):
                flat_response[res_key] = re.sub(
                    r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response[res_key]
                )
                try:
                    flat_response[res_key] = fuzzy_json.loads(flat_response[res_key])
                except Exception:
                    if verbose:
                        logger.info(flat_response[res_key])
                    return None
            if isinstance(flat_response[res_key], list):
                try:
                    res_list = []
                    for k in flat_response[res_key]:
                        if isinstance(k, dict):
                            res_list += [k.get(res_key, "")]
                        elif isinstance(k, str):
                            res_list += [k]
                    flat_response[res_key] = " | ".join(res_list)
                except Exception:
                    return None
            else:
                return None

    # #######################
    # audio_summary sometimes arrives as a JSON string rather than an object;
    # parse it before unpacking its fields
    if "audio_summary" in flat_response:
        if isinstance(flat_response["audio_summary"], str):
            flat_response["audio_summary"] = re.sub(
                r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response["audio_summary"]
            )
            try:
                flat_response["audio_summary"] = fuzzy_json.loads(flat_response["audio_summary"])
            except Exception:
                if verbose:
                    logger.info(flat_response["audio_summary"])
                return None

        for k in flat_response["audio_summary"]:
            try:
                audio_detail = flat_response["audio_summary"][k]
            except Exception as e:
                if verbose:
                    logger.warning(f"{e} | {k} | {flat_response['audio_summary']}")
                return None
            if isinstance(audio_detail, list):
                flat_response[k] = " | ".join([s for s in audio_detail if type(s) == str])
            elif isinstance(audio_detail, str):
                flat_response[k] = audio_detail
            else:
                return None
        del flat_response["audio_summary"]

    # #######################
    # faces
    if "faces" in flat_response:
        if isinstance(flat_response["faces"], str):
            flat_response["faces"] = re.sub(
                r"([a-zA-Z])'([a-zA-Z])", r"\1\2", flat_response["faces"]
            )
            try:
                flat_response["faces"] = fuzzy_json.loads(flat_response["faces"])
            except Exception:
                if verbose:
                    logger.info(flat_response["faces"])
                return None

        if isinstance(flat_response["faces"], list):
            for face in flat_response["faces"]:
                if isinstance(face, dict):
                    for k in face:
                        if "faces_" + k not in flat_response:
                            flat_response["faces_" + k] = ""
                        try:
                            flat_response["faces_" + k] += str(face[k]) + " | "
                        except Exception:
                            return None
                else:
                    return None
        else:
            return None
        del flat_response["faces"]

        for k in flat_response:
            if (
                (k.startswith("faces_"))
                and (isinstance(flat_response[k], str))
                and (flat_response[k].endswith(" | "))
            ):
                flat_response[k] = flat_response[k][:-3]

    # #######################
    # Any remaining list values are collapsed to their first element.
    for k in flat_response:
        if isinstance(flat_response[k], list):
            if verbose:
                logger.info(flat_response[k])
            # An empty list carries no value; collapse it to None rather than
            # indexing [0] (which raised IndexError on the occasional response).
            flat_response[k] = flat_response[k][0] if flat_response[k] else None

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
            unit = s[i : i + unit_len]
            # Count contiguous repeats of this unit starting at i
            k = 1
            j = i + unit_len
            while j + unit_len <= n and s[j : j + unit_len] == unit:
                k += 1
                j += unit_len
            if k >= min_repeats:
                covered = k * unit_len
                # Choose the candidate that covers the most chars; if tie, prefer shorter unit
                if best is None or covered > best[0] or (covered == best[0] and unit_len < best[2]):
                    best = (covered, k, unit_len)

        if best:
            covered, k, unit_len = best
            unit = s[i : i + unit_len]
            if len(unit) == 1:
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
        if text[i : i + 2] == r"\u" and i + 6 <= len(text):
            candidate = text[i + 2 : i + 6]
            if _hex.match(candidate):
                parts.append(chr(int(candidate, 16)))
                i += 6
                continue
            elif drop_invalid:
                i += 2  # skip the bad escape entirely
                continue
        if text[i : i + 2] == r"\u":
            # broken escape: either double the backslash to keep it literal…
            parts.append(r"\\u")
            i += 2
            continue
        parts.append(text[i])
        i += 1
    return "".join(parts)


def fuzzy_load_of_json_from_string(resp_text_in: str, notebook_mode=False):
    """
    The model output is a bit unpredictable so this function is doing what it can to figure
    out the json structure in the string and load it
    """

    resp_text = copy(resp_text_in)

    if type(resp_text) == str and len(resp_text) > 0:
        resp_text = resp_text.replace("\n", "")
        resp_text = resp_text.replace("```", "")
        if resp_text[:4] == "json":
            resp_text = resp_text[4:]

        try:
            if resp_text.strip()[0] != "{":
                return None

            refined_text = _compress_embedded_repeats(resp_text, min_repeats=3, max_unit_len=12)
            refined_text = refined_text.replace(": null,", ": ---,")
            refined_text = refined_text.replace(":null,", ": ---,")
            refined_text = refined_text.replace('"null"', "---")
            refined_text = refined_text.replace('\\"', "'")
            refined_text = refined_text.replace("''", "'")
            if "\\u" in refined_text:
                refined_text = _decode_valid_unicode_escapes(refined_text)
                refined_text = refined_text.encode("unicode_escape").decode("ascii")

            machine_annotations = fuzzy_json.loads(refined_text)

            return machine_annotations
        except Exception as e:
            if notebook_mode:
                logger.warning(f"{e} {refined_text}")
                return refined_text
            return None
    else:
        return None


def flatten_and_fix_machine_outputs(raw_outputs_from_machine, verbose=False, notebook_mode=False):

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
        if (
            raw_outputs_from_machine[h]["response"] is None
            or raw_outputs_from_machine[h]["response"] == ""
        ):
            bad_count += 1
            print("!", end="", flush=True)
        else:
            entry = raw_outputs_from_machine[h]
            if entry.get("structured"):
                # Structured responses are schema-constrained valid JSON: parse
                # directly and use the deterministic structured flattener. Falls
                # back to the fuzzy loader only if the strict parse somehow fails.
                try:
                    json_response = json.loads(entry["response"])
                except (json.JSONDecodeError, TypeError):
                    json_response = fuzzy_load_of_json_from_string(
                        entry["response"], notebook_mode=notebook_mode
                    )
                # The model can emit a malformed \uD8xx escape (half an emoji);
                # json.loads keeps it as a lone surrogate, which would crash the
                # parquet write downstream. Scrub all strings before flattening.
                json_response = scrub_surrogates_nested(json_response)
                if isinstance(json_response, dict):
                    flattened_response = flatten_structured(json_response)
                else:
                    flattened_response = None
            else:
                json_response = scrub_surrogates_nested(
                    fuzzy_load_of_json_from_string(entry["response"], notebook_mode=notebook_mode)
                )
                flattened_response = flatten_one_machine_response(
                    json_response, verbose=False, notebook_mode=notebook_mode
                )
            if type(flattened_response) == dict:
                good_count += 1
                print(".", end="", flush=True)
                for rk in flattened_response:
                    flattened_outputs_from_machine[h][rk] = copy(flattened_response[rk])
            else:
                bad_count += 1
                print("X", end="", flush=True)
                if notebook_mode:
                    logger.error("Error when postprocessing response -> bad response")
                    logger.error(raw_outputs_from_machine[h])
        if (good_count + bad_count) % 100 == 0:
            print()

    if (good_count + bad_count) % 100 != 0:
        print()

    logger.info(
        f"...extracted {good_count} good responses from the file. Unable to use {bad_count} responses."
    )

    if good_count == 0:
        return None

    # convert the dict to a DF, reset the index and drop the old response structure
    outputs_from_machine_df = pd.DataFrame(flattened_outputs_from_machine).T
    outputs_from_machine_df.reset_index(drop=True, inplace=True)
    outputs_from_machine_df.drop("response", axis=1, inplace=True)

    return outputs_from_machine_df


# the functions in this section clean up repetititions in the transcripts
# the main function is at the end of the section
def _check_repetitive_patterns(
    text: str, min_pattern_length: int = 5, min_repetitions: int = 5, max_text_length: int = 1000
) -> str:
    """
    Check for repetitive patterns in a string
    """

    if not isinstance(text, str):
        return "Not a string"

    if len(text) > max_text_length:
        return "String too long"

    words = text.split()
    n = len(words)

    pattern_counts = collections.defaultdict(int)

    # Check for all possible pattern lengths from min_pattern_length to half of the total number of words
    for length in range(min_pattern_length, n // 2 + 1):
        for i in range(n - length + 1):
            pattern = tuple(words[i : i + length])
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
    """Collapse runs of a repeated phrase to its first occurrence.

    Used for transcripts, which are prone to repetitive generation.
    """

    new_string = deepcopy(some_string.replace("-", " "))

    res = _check_repetitive_patterns(
        new_string, min_pattern_length=4, min_repetitions=12, max_text_length=10000
    )

    if len(res[1]) > 0:
        # sort the results with longest repeated pattern first
        most_repeated = sorted(res[1], key=lambda x: len(x[0]), reverse=True)

        # Iterate over the patterns, keeping the first occurrence in the string
        # and removing all others. This heuristic occasionally mangles text that
        # legitimately repeats a phrase.
        for _i, mr in enumerate(most_repeated):
            the_phrase = " ".join(mr[0])

            # register the position of the first occurrence of the pattern
            first_occurance = new_string.find(the_phrase)

            # remove all occurrences of the pattern
            new_string = deepcopy(new_string.replace(the_phrase, ""))

            # put back the pattern at the position of the first occurrence
            new_string = new_string[:first_occurance] + the_phrase + new_string[first_occurance:]

            # remove double spaces
            new_string = " ".join([k for k in new_string.split(" ") if len(k) > 0])

        # split the string on spaces and remove repetitions of words
        # again, this gives some probems, but is generally a good thing
        list_of_words = []
        for k in new_string.split(" "):
            if len(list_of_words) == 0 or list_of_words[-1] != k:
                list_of_words += [k]

        return new_string

    return some_string


def _prettify_string(a_string):
    new_string = deepcopy(a_string)
    things_to_remove = ["| |"]
    gh = 0
    while gh > -1:
        new_string = " ".join([g for g in new_string.split(" ") if len(g) > 0]).strip()
        for ttr in things_to_remove:
            gh = new_string.find(ttr)
            if gh > -1:
                new_string = new_string.replace(ttr, "")
    return new_string


def remove_repetitions_from_transcripts(
    outputs_from_machine_df_in,  # expecting a dataframe with a column called "transcript". Elements should be a pipe-separated stringified list.
    verbose=False,
    notebook_mode=False,
):

    if notebook_mode:
        verbose = True

    if verbose:
        logger.info("Removing repeated patterns in the transcripts - this may take a little while")

    outputs_from_machine_df = outputs_from_machine_df_in.copy()

    new_transcripts = []
    for transcript in outputs_from_machine_df["transcript"].tolist():
        if type(transcript) != str or len(transcript) < 50:
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

            if len(new_transcript) >= 50:
                might_be_shorter = _remove_repetitions(new_transcript)
                if len(might_be_shorter) < len(new_transcript):
                    new_transcript = copy(might_be_shorter)

            new_transcripts += [copy(new_transcript)]

    outputs_from_machine_df["transcript_no_repetitions"] = new_transcripts

    if verbose:
        logger.info("Prettifying all strings")
    outputs_from_machine_df = outputs_from_machine_df.map(
        lambda x: x if not isinstance(x, str) else _prettify_string(x)
    ).copy()

    return outputs_from_machine_df
