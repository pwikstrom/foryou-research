"""Production-path test: structured raw responses refine via the structured path.

When ``use_structured_output`` is on, ``call_machine`` marks each raw output
``structured=True`` and stores schema-constrained JSON. This test verifies that
``refine_one_raw_annotation_batch`` (the production refinement) detects that
marker and routes through ``flatten_structured`` + ``apply_conditional_rules``
instead of the legacy fuzzy flattener — producing a valid annotated dataframe
with the same column shape. No API calls.

Usage:
    python tests/golden/test_structured_refinement_path.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import isolated_storage, pinned_var_schema

import fyp.machine_annotation as ma


def _structured_response(type_of_story: str) -> dict:
    return {
        "transcript": [{"speaker": "A", "text": "hello world"}],
        "scenes": [{"scene_index": 0, "description": "a person dances",
                    "sentiment": "Positive High-Energy"}],
        "objects": ["phone", "table"],
        "symbols_and_brands": ["nike"],
        "text_overlays": ["hi there"],
        "faces": [{"gender": "Female", "age_estimate": "20-30", "ethnicity": "Caucasian"}],
        "audio_summary": {"speech_vs_music": "60% speech, 40% music",
                          "background_music": "upbeat", "notable_sounds": ["clap"]},
        "main_activity": "dancing in a studio",
        "video_story": "A person dances in a studio.",
        "type_of_story": type_of_story,
        "content_category": ["Performance", "Daily Life"],
        "australian_relevance": "No", "tiktok_native": "Yes", "trend": "No",
        "advertising": "No", "aigc": "No",
        "main_gender": "Female", "main_ethnicity": "Caucasian",
        "political_score": {"score": 0, "rationale": "no political content"},
        "sensitivity_score": {"score": 10, "rationale": "low sensitivity"},
        "call_to_action": "follow for more",
        "aussie_political_message": "-", "aussie_political_positioning": "-",
        "framing_analysis_problem_definition": "model-written framing",
        "framing_analysis_attribution_of_responsibility": "model-written framing",
        "framing_analysis_moral_evaluation": "model-written framing",
        "framing_analysis_treatment_recommendation": "model-written framing",
        "cultural_representation_analysis_key_groups": "two-sentence justification here.",
        "cultural_representation_analysis_complexity_vs_stereotypes": "two-sentence justification.",
        "cultural_representation_analysis_symbolism_and_imagery": "two-sentence justification.",
        "cultural_representation_analysis_inclusion_and_exclusion": "two-sentence justification.",
        "ideological_analysis_dominant_ideologies": "two-sentence justification here.",
        "ideological_analysis_power_dynamics": "two-sentence justification here.",
        "ideological_analysis_critique_or_reinforcement": "two-sentence justification.",
        "ideological_analysis_cultural_or_historical_context": "two-sentence justification.",
    }


def _raw_batch() -> dict:
    items = {
        "0": ("1000000000000000001", "Human-Interest"),
        "1": ("1000000000000000002", "Issue-Based"),
    }
    out = {}
    for idx, (item_id, tos) in items.items():
        out[idx] = {
            "item_id": item_id,
            "model": "gemini-2.5-flash",
            "prompt_fn": "new_prompt_002.txt",
            "structured": True,
            "finish_reason": "FinishReason.STOP",
            "response": json.dumps(_structured_response(tos)),
        }
    return out


def test_structured_refinement_produces_valid_rows() -> None:
    with pinned_var_schema(), isolated_storage():
        df = ma.refine_one_raw_annotation_batch(
            raw_outputs_from_machine=_raw_batch(),
            raw_json_filename="structured_test.json",
            verbose=False,
        )
    assert df is not None and not df.empty, "structured refinement returned nothing"
    assert df.shape[0] == 2, f"expected 2 rows, got {df.shape[0]}"
    assert "annotated_ok" in df.columns
    assert bool(df["annotated_ok"].fillna(False).all()), "structured rows should be annotated_ok"
    # The structured path produced the recoded analytic columns.
    for col in ("type_of_story", "content_category", "main_gender"):
        assert col in df.columns, f"missing recoded column {col}"


def test_routing_is_marker_driven() -> None:
    """The ``structured`` marker — not the payload — decides which flattener runs.

    The structured path parses the ``{score, rationale}`` score-object correctly
    into a numeric column; the legacy path (marker off) stringifies the dict and
    ``recode_scores`` coerces it to NaN. Comparing the recoded ``sensitivity_
    score`` on the same payload proves routing is governed by the marker.
    """
    struct_batch = _raw_batch()
    legacy_batch = _raw_batch()
    for entry in legacy_batch.values():
        entry["structured"] = False

    with pinned_var_schema(), isolated_storage():
        df_structured = ma.refine_one_raw_annotation_batch(
            raw_outputs_from_machine=struct_batch,
            raw_json_filename="structured_route.json", verbose=False,
        )
    with pinned_var_schema(), isolated_storage():
        df_legacy = ma.refine_one_raw_annotation_batch(
            raw_outputs_from_machine=legacy_batch,
            raw_json_filename="legacy_route.json", verbose=False,
        )

    assert df_structured["sensitivity_score"].notna().all(), (
        "structured path should parse the score-object into a numeric value"
    )
    assert df_legacy["sensitivity_score"].isna().all(), (
        "legacy path should NOT parse the structured score-object (proves routing "
        "is marker-driven, and that legacy raw files are unaffected)"
    )


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
            traceback.print_exc()
            print(f"ERROR {t.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
