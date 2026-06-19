"""Production-path test: structured raw responses refine via the structured path.

When ``use_structured_output`` is on, ``call_machine`` marks each raw output
``structured=True`` and stores schema-constrained JSON. This test verifies that
``refine_one_raw_annotation_batch`` (the production refinement) detects that
marker and routes through ``flatten_structured`` instead of the legacy fuzzy
flattener — producing a valid annotated dataframe. No API calls.

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
        "transcript": "hello world, this is the spoken text",
        "spoken_language": "English",
        "multilingual": "No",
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
        "primary_country": "Australia", "tiktok_native": "Yes",
        "trend_technical": "No", "trend_cultural": "No",
        "advertising": "No", "aigc": "No",
        "main_gender": "Female", "main_ethnicity": "Caucasian",
        "political_score": 0,
        "sensitivity_score": 10,
        "call_to_action": "follow for more",
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
