"""Unit tests for multi-platform annotation generalization.

Pins the platform-agnostic pieces added when annotation opened up beyond
TikTok: the permissive item-id sanity pattern, the composite
``(source_platform, item_id)`` keying of ``select_active_view`` /
``select_version_view``, and the batch request builder's explicit ``file_uri``
override. All checks are pure — no disk, no API, no real storage writes.

Usage:
    python tests/unit/test_multiplatform_annotation.py
Exit 0 iff all checks pass.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

import fyp.annotation_versioning as av

# The same pattern annotate_from_video_id_list uses to reject corrupt lists.
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{5,40}")


# ---------------------------------------------------------------------------
# item-id sanity pattern (platform-agnostic)
# ---------------------------------------------------------------------------

def test_id_pattern_accepts_tiktok() -> None:
    assert ID_PATTERN.fullmatch("7234567890123456789")


def test_id_pattern_accepts_youtube() -> None:
    assert ID_PATTERN.fullmatch("dQw4w9WgXcQ")
    assert ID_PATTERN.fullmatch("a-B_c1D2e3F")


def test_id_pattern_accepts_instagram_shortcode() -> None:
    assert ID_PATTERN.fullmatch("CxYzAb12Q3d")


def test_id_pattern_rejects_garbage() -> None:
    assert not ID_PATTERN.fullmatch("")
    assert not ID_PATTERN.fullmatch("abc")  # too short
    assert not ID_PATTERN.fullmatch("x" * 41)  # too long
    assert not ID_PATTERN.fullmatch("gs://bucket/media/123.mp4")  # path/URL
    assert not ID_PATTERN.fullmatch("123 456")  # whitespace
    assert not ID_PATTERN.fullmatch("nan")  # would be caught by length


# ---------------------------------------------------------------------------
# composite-key active view
# ---------------------------------------------------------------------------

def _multi_platform_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"source_platform": "tiktok", "item_id": "11111111111", "annotation_version": "vA", "val": "tt-old"},
            {"source_platform": "tiktok", "item_id": "11111111111", "annotation_version": "vB", "val": "tt-new"},
            {"source_platform": "youtube", "item_id": "11111111111", "annotation_version": "vB", "val": "yt"},
            {"source_platform": "instagram", "item_id": "CxYzAb12Q3d", "annotation_version": "vA", "val": "ig"},
        ]
    )


def test_active_view_keeps_same_id_on_two_platforms() -> None:
    out = av.select_active_view(_multi_platform_frame(), "vB")
    # tiktok+youtube share an item_id but are distinct items; instagram falls
    # back to its vA row. 3 rows total.
    assert len(out) == 3
    assert set(zip(out["source_platform"], out["val"])) == {
        ("tiktok", "tt-new"), ("youtube", "yt"), ("instagram", "ig"),
    }


def test_active_view_prefers_active_version_per_platform_item() -> None:
    out = av.select_active_view(_multi_platform_frame(), "vA")
    tt = out[out["source_platform"] == "tiktok"]
    assert len(tt) == 1 and tt.iloc[0]["val"] == "tt-old"
    # youtube has no vA row -> falls back to its latest (vB)
    yt = out[out["source_platform"] == "youtube"]
    assert len(yt) == 1 and yt.iloc[0]["val"] == "yt"


def test_active_view_without_platform_column_unchanged() -> None:
    df = pd.DataFrame(
        [
            {"item_id": "i1", "annotation_version": "vA", "val": "old"},
            {"item_id": "i1", "annotation_version": "vB", "val": "new"},
        ]
    )
    out = av.select_active_view(df, "vB")
    assert len(out) == 1 and out.iloc[0]["val"] == "new"


def test_version_view_is_composite_keyed() -> None:
    out = av.select_version_view(_multi_platform_frame(), "vB")
    assert len(out) == 2
    assert set(out["source_platform"]) == {"tiktok", "youtube"}


# ---------------------------------------------------------------------------
# batch request builder file_uri override
# ---------------------------------------------------------------------------

def test_build_request_dict_uses_explicit_file_uri() -> None:
    from fyp.machine_annotation_batch import build_request_dict

    req = build_request_dict(
        "dQw4w9WgXcQ", bucket="b", media_prefix="media",
        system_instruction="p", schema_json={"type": "object"},
        gen_params={}, file_uri="gs://b/media/youtube/dQw4w9WgXcQ.mp4",
    )
    uri = req["request"]["contents"][0]["parts"][1]["fileData"]["fileUri"]
    assert uri == "gs://b/media/youtube/dQw4w9WgXcQ.mp4"


def test_build_request_dict_falls_back_to_flat_uri() -> None:
    from fyp.machine_annotation_batch import build_request_dict

    req = build_request_dict(
        "7234567890123456789", bucket="b", media_prefix="media",
        system_instruction="p", schema_json={"type": "object"}, gen_params={},
    )
    uri = req["request"]["contents"][0]["parts"][1]["fileData"]["fileUri"]
    assert uri == "gs://b/media/7234567890123456789.mp4"


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
