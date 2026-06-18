"""Offline tests for the Gemini Batch API annotation path (pure functions).

Pins the correctness-critical contracts in ``fyp/machine_annotation_batch.py``
without any GCS / batch-API calls:
  * the JSONL request line (GCS fileData, structured generation config,
    media_resolution);
  * the output-record -> raw-shape mapping (success, error, empty, and DNF
    synthesis for ids missing from the output);
  * and — the key guarantee — that a batch-ingested raw record refines through
    the SAME marker-driven pipeline as the synchronous path
    (refine_one_raw_annotation_batch), producing an annotated row carrying its
    annotation_version.

The thin GCS / batch-API wrappers are SPIKE-GATED and not exercised here.

Usage:
    python tests/golden/test_batch_annotation.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))        # tests/golden
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))    # project root

from _harness import isolated_storage, pinned_var_schema
from test_structured_refinement_path import _structured_response

import fyp.machine_annotation as ma
import fyp.machine_annotation_batch as batch

_GEN = {"temperature": 1.0, "max_output_tokens": 65536, "thinking_budget": -1, "media_resolution": None}
_SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}


def _success_record(item_id: str, text: str) -> dict:
    return {
        "request": {
            "contents": [
                {"role": "user", "parts": [
                    {"text": "Analyze this video"},
                    {"fileData": {"fileUri": f"gs://b/media/{item_id}.mp4", "mimeType": "video/mp4"}},
                ]}
            ]
        },
        "response": {
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 800,
                              "thoughtsTokenCount": 300, "totalTokenCount": 2300},
        },
    }


# ---------------------------------------------------------------------------
# request builder
# ---------------------------------------------------------------------------

def test_build_request_dict_structure() -> None:
    req = batch.build_request_dict("12345", bucket="mybucket", media_prefix="media",
                                   system_instruction="PROMPT", schema_json=_SCHEMA, gen_params=_GEN)
    r = req["request"]
    parts = r["contents"][0]["parts"]
    assert parts[1]["fileData"]["fileUri"] == "gs://mybucket/media/12345.mp4"
    assert parts[1]["fileData"]["mimeType"] == "video/mp4"
    assert r["systemInstruction"]["parts"][0]["text"] == "PROMPT"
    gc = r["generationConfig"]
    assert gc["responseMimeType"] == "application/json"
    assert gc["responseSchema"] == _SCHEMA
    assert gc["thinkingConfig"]["thinkingBudget"] == -1
    assert "mediaResolution" not in gc       # None -> omitted


def test_build_request_media_resolution() -> None:
    gen = dict(_GEN, media_resolution="LOW")
    gc = batch.build_request_dict("1", bucket="b", media_prefix="media",
                                  system_instruction="p", schema_json=_SCHEMA, gen_params=gen)["request"]["generationConfig"]
    assert gc["mediaResolution"] == "MEDIA_RESOLUTION_LOW"
    gen2 = dict(_GEN, media_resolution="MEDIA_RESOLUTION_HIGH")
    gc2 = batch.build_request_dict("1", bucket="b", media_prefix="media",
                                   system_instruction="p", schema_json=_SCHEMA, gen_params=gen2)["request"]["generationConfig"]
    assert gc2["mediaResolution"] == "MEDIA_RESOLUTION_HIGH"


def test_item_id_from_uri() -> None:
    assert batch.item_id_from_uri("gs://b/media/999.mp4") == "999"
    assert batch.item_id_from_uri(None) is None


# ---------------------------------------------------------------------------
# output -> raw-shape mapping
# ---------------------------------------------------------------------------

def test_ingest_success_record_shape() -> None:
    rec = _success_record("111", '{"type_of_story": "Human-Interest"}')
    out = batch.ingest_output_record(rec, model="gemini-3-flash-preview",
                                     prompt_fn="new_prompt_002.txt", annotation_version="av_x")
    assert out["item_id"] == "111"
    assert out["structured"] is True
    assert out["annotation_version"] == "av_x"
    assert out["finish_reason"] == "STOP"
    assert out["response"] == '{"type_of_story": "Human-Interest"}'
    assert out["usage"]["prompt_tokens"] == 1200
    assert out["usage"]["total_tokens"] == 2300


def test_ingest_raw_shape_matches_call_machine_contract() -> None:
    # The raw dict MUST carry exactly the keys the synchronous call_machine
    # emits, so the marker-driven refinement consumes it unchanged.
    expected = {"item_id", "inference_ts", "inference_duration", "model", "prompt_fn",
                "annotation_version", "structured", "usage", "error", "finish_reason", "response"}
    out = batch.ingest_output_record(_success_record("1", "{}"), model="m",
                                     prompt_fn="p", annotation_version="av_x")
    assert set(out.keys()) == expected


def test_ingest_error_record_is_dnf() -> None:
    rec = {"request": {"contents": [{"parts": [{"fileData": {"fileUri": "gs://b/media/7.mp4"}}]}]},
           "status": {"code": 3, "message": "bad request"}}
    out = batch.ingest_output_record(rec, model="m", prompt_fn="p", annotation_version="av_x")
    assert out["item_id"] == "7"
    assert out["finish_reason"].startswith("DNF")
    assert out["response"] == ""


def test_ingest_records_synthesizes_dnf_for_missing() -> None:
    records = [_success_record("a", "{}")]
    raw = batch.ingest_records_to_raw(records, ["a", "b"], model="m", prompt_fn="p", annotation_version="av_x")
    by_item = {v["item_id"]: v for v in raw.values()}
    assert by_item["a"]["finish_reason"] == "STOP"
    assert by_item["b"]["finish_reason"].startswith("DNF")   # missing from output
    assert by_item["b"]["response"] == ""


def test_batch_ingested_raw_refines_through_structured_path() -> None:
    # The end-to-end guarantee: a batch-ingested raw record refines exactly like
    # a synchronous one (structured marker -> flatten_structured), yielding an
    # annotated row that carries its annotation_version.
    rec = _success_record("1000000000000000001", json.dumps(_structured_response("Human-Interest")))
    raw = batch.ingest_records_to_raw([rec], ["1000000000000000001"],
                                      model="gemini-3-flash-preview", prompt_fn="new_prompt_002.txt",
                                      annotation_version="av_batch_test")
    with pinned_var_schema(), isolated_storage():
        df = ma.refine_one_raw_annotation_batch(raw_outputs_from_machine=raw,
                                                raw_json_filename="machine_annotations_batch_x.json", verbose=False)
    assert df is not None and len(df) == 1
    assert bool(df["annotated_ok"].fillna(False).all())
    assert df["annotation_version"].iloc[0] == "av_batch_test"


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
