"""Characterization tests for the Gemini-output JSON-repair / flatten helpers.

These pin the *current* behaviour of the fragile, hand-rolled functions in
``fyp/machine_annotation.py`` that exist only because the prompt asks for
free-text (non-structured) JSON:

    _compress_embedded_repeats, _decode_valid_unicode_escapes,
    fuzzy_load_of_json_from_string, flatten_one_machine_response,
    _remove_repetitions

When the pipeline moves to structured output, most of these get replaced by a
deterministic schema-driven flattener.  This module is the contract the
replacement must honour (or consciously change): it documents exactly how the
old path treated fences, repeats, unicode escapes, nested scenes/faces/audio,
and missing required keys.

Usage:
    python tests/unit/test_annotation_repair.py
Exit 0 iff all checks pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fyp.machine_annotation as ma

# ---------------------------------------------------------------------------
# _compress_embedded_repeats
# ---------------------------------------------------------------------------

def test_compress_repeats_multichar_unit() -> None:
    assert ma._compress_embedded_repeats("abcabcabc") == "[3]*[abc]"
    assert ma._compress_embedded_repeats("xabcabcabcy") == "x[3]*[abc]y"


def test_compress_repeats_below_threshold_unchanged() -> None:
    # Two repeats (< min_repeats=3) are left alone.
    assert ma._compress_embedded_repeats("abab") == "abab"
    assert ma._compress_embedded_repeats("hello") == "hello"


def test_compress_repeats_single_char_run_collapses_to_one() -> None:
    # Documented (lossy) behaviour: a run of >=3 identical chars collapses to 1.
    assert ma._compress_embedded_repeats("aaaa") == "a"
    assert ma._compress_embedded_repeats("aaa") == "a"


# ---------------------------------------------------------------------------
# _decode_valid_unicode_escapes
# ---------------------------------------------------------------------------

def test_decode_valid_escape() -> None:
    assert ma._decode_valid_unicode_escapes(r"&") == "&"
    assert ma._decode_valid_unicode_escapes(r"plain A text") == "plain A text"


def test_decode_passthrough_and_invalid() -> None:
    assert ma._decode_valid_unicode_escapes("no escapes here") == "no escapes here"
    # Invalid hex with drop_invalid=True drops the "\u" marker, keeps the rest.
    assert ma._decode_valid_unicode_escapes(r"\uZZZZ") == "ZZZZ"


# ---------------------------------------------------------------------------
# fuzzy_load_of_json_from_string
# ---------------------------------------------------------------------------

def test_fuzzy_load_valid_object() -> None:
    assert ma.fuzzy_load_of_json_from_string('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_fuzzy_load_strips_markdown_fence() -> None:
    assert ma.fuzzy_load_of_json_from_string('```json\n{"a": 1}\n```') == {"a": 1}


def test_fuzzy_load_rejects_non_object_and_empty() -> None:
    # A top-level array (not an object) is rejected.
    assert ma.fuzzy_load_of_json_from_string("[1,2,3]") is None
    assert ma.fuzzy_load_of_json_from_string("") is None
    assert ma.fuzzy_load_of_json_from_string(None) is None


# ---------------------------------------------------------------------------
# flatten_one_machine_response
# ---------------------------------------------------------------------------

def _full_response() -> dict:
    return {
        "transcript": [
            {"speaker": "A", "text": "hello"},
            {"speaker": "B", "text": "world"},
        ],
        "scenes": [
            {"scene_index": 0, "description": "a dog runs", "sentiment": "Positive"},
            {"scene_index": 1, "description": "a cat sleeps", "sentiment": "Positive"},
        ],
        "objects": ["dog", "cat"],
        "symbols_and_brands": ["nike"],
        "text_overlays": ["hi there"],
        "content_category": ["comedy", "animals"],
        "faces": [{"gender": "Female", "age_estimate": "20-30", "ethnicity": "Caucasian"}],
        "audio_summary": {
            "speech_vs_music": "50% speech, 50% music",
            "background_music": "upbeat",
            "notable_sounds": ["bark"],
        },
    }


def test_flatten_joins_lists_and_extracts_scene_sentiment() -> None:
    flat = ma.flatten_one_machine_response(_full_response())
    assert flat["transcript"] == "hello | world"
    assert flat["scenes"] == "a dog runs | a cat sleeps"
    assert flat["scene_sentiments"] == "Positive"
    assert flat["objects"] == "dog | cat"
    assert flat["content_category"] == "comedy | animals"


def test_flatten_unpacks_faces_and_audio() -> None:
    flat = ma.flatten_one_machine_response(_full_response())
    assert flat["faces_gender"] == "Female"
    assert flat["faces_age_estimate"] == "20-30"
    assert flat["faces_ethnicity"] == "Caucasian"
    assert flat["speech_vs_music"] == "50% speech, 50% music"
    assert flat["background_music"] == "upbeat"
    assert flat["notable_sounds"] == "bark"
    # Nested containers are removed after unpacking.
    assert "faces" not in flat
    assert "audio_summary" not in flat


def test_flatten_non_dict_returned_as_is() -> None:
    assert ma.flatten_one_machine_response("not a dict") == "not a dict"
    assert ma.flatten_one_machine_response(None) is None


# ---------------------------------------------------------------------------
# _remove_repetitions
# ---------------------------------------------------------------------------

def test_remove_repetitions_shrinks_repeated_phrase() -> None:
    # Documented (lossy) behaviour: the de-duplicator substantially shortens a
    # heavily-repeated transcript, but it MANGLES word order in the process
    # (the code's own comment: "Sometimes this screws things up but it works ok
    # most of the time"). We pin the reliable contract — large shrinkage and a
    # non-empty result — not a clean reconstruction.  A structured-output
    # pipeline with a thinking model should make this hack largely unnecessary.
    repeated = "the quick brown fox " * 13
    out = ma._remove_repetitions(repeated)
    assert isinstance(out, str) and out.strip()
    assert len(out) < len(repeated) * 0.5  # at least halved


def test_remove_repetitions_leaves_clean_text_unchanged() -> None:
    clean = "hello world this is a normal sentence"
    assert ma._remove_repetitions(clean) == clean


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception:
            failures += 1
            import traceback

            print(f"ERROR {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
