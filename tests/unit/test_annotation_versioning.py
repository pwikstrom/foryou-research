"""Unit tests for annotation version identity + registry transforms.

Pins ``fyp/annotation_versioning.py``: the deterministic ``annotation_version``
hash (sensitive to model / prompt text / schema / gen params) and the pure
registry transforms (``_register_into`` / ``_promote_into``) that implement the
stay-pinned-until-promote rule. All checks are pure — no disk, no API, no real
storage writes.

Usage:
    python tests/unit/test_annotation_versioning.py
Exit 0 iff all checks pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

import fyp.annotation.annotation_versioning as av

PROMPT_A = "Analyze the video. Step 1: extract a transcript."
PROMPT_B = "Analyze the video. Step 1: extract a transcript (revised wording)."
SCHEMA_A = {"type": "object", "properties": {"x": {"type": "string"}}}
SCHEMA_B = {"type": "object", "properties": {"y": {"type": "string"}}}
PARAMS = {
    "use_structured_output": True,
    "temperature": 1.0,
    "thinking_budget": -1,
    "media_resolution": None,
    "max_output_tokens": 65536,
}


def _desc(model="m", prompt=PROMPT_A, schema=SCHEMA_A, params=None, label=None) -> dict:
    return av.build_version_descriptor(model, prompt, schema, params or PARAMS, label)


# ---------------------------------------------------------------------------
# version identity
# ---------------------------------------------------------------------------


def test_version_deterministic() -> None:
    assert _desc()["annotation_version"] == _desc()["annotation_version"]


def test_version_has_prefix() -> None:
    assert _desc()["annotation_version"].startswith("av_")


def test_version_changes_on_prompt() -> None:
    assert (
        _desc(prompt=PROMPT_A)["annotation_version"] != _desc(prompt=PROMPT_B)["annotation_version"]
    )


def test_version_changes_on_model() -> None:
    assert _desc(model="m1")["annotation_version"] != _desc(model="m2")["annotation_version"]


def test_version_changes_on_schema() -> None:
    assert (
        _desc(schema=SCHEMA_A)["annotation_version"] != _desc(schema=SCHEMA_B)["annotation_version"]
    )


def test_version_changes_on_gen_params() -> None:
    altered = dict(PARAMS, temperature=0.0)
    assert _desc(params=PARAMS)["annotation_version"] != _desc(params=altered)["annotation_version"]


def test_media_resolution_affects_version() -> None:
    altered = dict(PARAMS, media_resolution="MEDIA_RESOLUTION_LOW")
    assert _desc(params=PARAMS)["annotation_version"] != _desc(params=altered)["annotation_version"]


def test_schema_hash_none_for_freetext() -> None:
    assert av.compute_schema_hash(None) == "none"
    assert av.compute_schema_hash({}) == "none"
    assert av.compute_schema_hash(SCHEMA_A) != "none"


def test_label_default_and_override() -> None:
    assert _desc(label="np002")["label"] == "np002"
    assert _desc()["label"].startswith("m:")


def test_active_version_from_real_config_is_safe() -> None:
    # Read-only: exercises the config/prompt-reading path without writing.
    version = av.active_annotation_version()
    assert version == "unknown" or version.startswith("av_")


# ---------------------------------------------------------------------------
# registry transforms (pure)
# ---------------------------------------------------------------------------


def test_register_does_not_auto_activate() -> None:
    d = _desc()
    reg = av._register_into(av.empty_registry(), d, PROMPT_A, SCHEMA_A, "t0")
    assert reg["preferred"] is None  # stay-pinned-until-promote
    assert d["annotation_version"] in reg["versions"]


def test_register_never_activates() -> None:
    d1, d2 = _desc(prompt=PROMPT_A), _desc(prompt=PROMPT_B)
    reg = av._register_into(av.empty_registry(), d1, PROMPT_A, SCHEMA_A, "t0")
    reg = av._register_into(reg, d2, PROMPT_B, SCHEMA_A, "t1")
    assert reg["preferred"] is None  # nothing auto-promotes
    assert d1["annotation_version"] in reg["versions"]
    assert d2["annotation_version"] in reg["versions"]


def test_register_idempotent() -> None:
    d = _desc()
    reg = av._register_into(av.empty_registry(), d, PROMPT_A, SCHEMA_A, "t0")
    reg2 = av._register_into(reg, d, PROMPT_A, SCHEMA_A, "t9")
    assert reg2 == reg
    assert len(reg2["versions"]) == 1
    assert reg2["versions"][d["annotation_version"]]["created_at"] == "t0"


def test_register_snapshots_prompt_and_schema() -> None:
    d = _desc()
    reg = av._register_into(av.empty_registry(), d, PROMPT_A, SCHEMA_A, "t0")
    entry = reg["versions"][d["annotation_version"]]
    assert entry["prompt_text"] == PROMPT_A
    assert entry["schema_json"] == SCHEMA_A


def test_promote_changes_active() -> None:
    d1, d2 = _desc(prompt=PROMPT_A), _desc(prompt=PROMPT_B)
    reg = av._register_into(av.empty_registry(), d1, PROMPT_A, SCHEMA_A, "t0")
    reg = av._register_into(reg, d2, PROMPT_B, SCHEMA_A, "t1")
    reg = av._promote_into(reg, d2["annotation_version"])
    assert reg["preferred"] == d2["annotation_version"]


def test_promote_unknown_raises() -> None:
    reg = av._register_into(av.empty_registry(), _desc(), PROMPT_A, SCHEMA_A, "t0")
    raised = None
    try:
        av._promote_into(reg, "av_doesnotexist")
    except KeyError as exc:
        raised = exc
    assert raised is not None


# ---------------------------------------------------------------------------
# active / pinned view selection (pure, synthetic frames)
# ---------------------------------------------------------------------------


def _view_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"item_id": "i1", "annotation_version": "vA", "val": "a1"},
            {"item_id": "i1", "annotation_version": "vB", "val": "b1"},
            {"item_id": "i2", "annotation_version": "vA", "val": "a2"},
            {"item_id": "i3", "annotation_version": "vB", "val": "b3"},
        ]
    )


def test_select_preferred_view_prefers_active_with_fallback() -> None:
    out = av.select_preferred_view(_view_df(), "vA")
    got = dict(zip(out["item_id"], out["val"]))
    assert got == {"i1": "a1", "i2": "a2", "i3": "b3"}  # i3 falls back to vB


def test_select_preferred_view_keep_last_within_version() -> None:
    df = pd.DataFrame(
        [
            {"item_id": "i1", "annotation_version": "vA", "val": "first"},
            {"item_id": "i1", "annotation_version": "vA", "val": "last"},
        ]
    )
    out = av.select_preferred_view(df, "vA")
    assert len(out) == 1
    assert out.iloc[0]["val"] == "last"


def test_select_version_view_strict() -> None:
    out = av.select_version_view(_view_df(), "vB")
    got = dict(zip(out["item_id"], out["val"]))
    assert got == {"i1": "b1", "i3": "b3"}  # only vB rows; i2 (vA-only) excluded


def test_select_view_without_version_column_falls_back_to_latest() -> None:
    df = pd.DataFrame([{"item_id": "i1", "val": "x"}, {"item_id": "i1", "val": "y"}])
    out = av.select_preferred_view(df, "vA")
    assert len(out) == 1
    assert out.iloc[0]["val"] == "y"


# ---------------------------------------------------------------------------
# "latest" means the most recent annotation (inference_ts), not the last row
# ---------------------------------------------------------------------------


def _timed_df() -> pd.DataFrame:
    """i1's newest annotation (vNew, ts 300) sits BEFORE its older rows in the
    frame — the order the October 2026 freeze found, where the legacy row came
    last and was the one studies read."""
    return pd.DataFrame(
        [
            {"item_id": "i1", "annotation_version": "vNew", "inference_ts": 300, "val": "new"},
            {"item_id": "i1", "annotation_version": "vMid", "inference_ts": 200, "val": "mid"},
            {
                "item_id": "i1",
                "annotation_version": "v0_legacy",
                "inference_ts": None,
                "val": "old",
            },
            {"item_id": "i2", "annotation_version": "vPref", "inference_ts": 100, "val": "pref"},
            {"item_id": "i2", "annotation_version": "vNew", "inference_ts": 300, "val": "new2"},
        ]
    )


def test_fallback_reads_the_newest_annotation_not_the_last_row() -> None:
    out = av.select_preferred_view(_timed_df(), "vPref")
    got = dict(zip(out["item_id"], out["val"]))
    # i1 has no vPref row: it falls back to its newest annotation, although the
    # legacy row is last in the frame. i2 keeps the preferred version even
    # though a newer annotation exists.
    assert got == {"i1": "new", "i2": "pref"}


def test_no_preferred_version_reads_the_newest_annotation() -> None:
    out = av.select_latest_view(_timed_df())
    assert dict(zip(out["item_id"], out["val"])) == {"i1": "new", "i2": "new2"}


def test_rows_without_a_timestamp_sort_oldest() -> None:
    df = pd.DataFrame(
        [
            {"item_id": "i1", "annotation_version": "vA", "inference_ts": 5, "val": "timed"},
            {
                "item_id": "i1",
                "annotation_version": "v0_legacy",
                "inference_ts": None,
                "val": "bare",
            },
        ]
    )
    assert av.select_latest_view(df).iloc[0]["val"] == "timed"
    assert av.select_preferred_view(df, "vOther").iloc[0]["val"] == "timed"


def test_timestamp_ties_keep_the_last_row() -> None:
    same_ts = pd.DataFrame(
        [
            {"item_id": "i1", "annotation_version": "vA", "inference_ts": 7, "val": "first"},
            {"item_id": "i1", "annotation_version": "vB", "inference_ts": 7, "val": "last"},
        ]
    )
    no_ts = same_ts.assign(inference_ts=None)
    for df in (same_ts, no_ts):
        assert av.select_latest_view(df).iloc[0]["val"] == "last"
        assert av.select_preferred_view(df, "vOther").iloc[0]["val"] == "last"


def test_newest_row_is_kept_within_a_version() -> None:
    df = pd.DataFrame(
        [
            {"item_id": "i1", "annotation_version": "vA", "inference_ts": 900, "val": "rerun"},
            {"item_id": "i1", "annotation_version": "vA", "inference_ts": 100, "val": "first-run"},
            {"item_id": "i1", "annotation_version": "vB", "inference_ts": 950, "val": "other"},
        ]
    )
    assert av.select_version_view(df, "vA").iloc[0]["val"] == "rerun"
    assert av.select_preferred_view(df, "vA").iloc[0]["val"] == "rerun"


def test_newest_row_mask_handles_stored_dtypes_and_keeps_frame_order() -> None:
    # The archive stores inference_ts as a nullable pyarrow integer.
    df = pd.DataFrame(
        {
            "source_platform": pd.array(["tiktok"] * 4, dtype="string[pyarrow]"),
            "item_id": pd.array(["b", "a", "b", "a"], dtype="string[pyarrow]"),
            "inference_ts": pd.array([20, None, 10, 5], dtype="int64[pyarrow]"),
            "val": ["b-new", "a-bare", "b-old", "a-timed"],
        }
    )
    mask = av.newest_row_mask(df, ["source_platform", "item_id"])
    assert mask.tolist() == [True, False, False, True]
    assert av.keep_newest(df, ["source_platform", "item_id"])["val"].tolist() == [
        "b-new",
        "a-timed",
    ]
    assert av.newest_row_mask(df.iloc[0:0], ["item_id"]).tolist() == []


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
