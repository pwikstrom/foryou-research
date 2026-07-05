"""Verify fyp.media_paths: relpath construction, candidate ordering, and the
resolve_media preference chain (storage_link → platform subpath → legacy flat).

Local-mode only (tmpdir fixtures); no GCS access.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fyp.media_paths as media_paths
from fyp.fyp_config import fyp_cf


def main() -> int:
    assert media_paths.media_relpath("tiktok", "123") == "tiktok/123.mp4"
    assert media_paths.media_relpath("instagram", "abc", ext="webm") == "instagram/abc.webm"

    # Candidate order: platform subpath first, then legacy flat, then others
    cands = media_paths.candidate_relpaths("123", "tiktok")
    assert cands[0] == "tiktok/123.mp4" and cands[1] == "123.mp4", f"got {cands}"
    # Unknown platform: flat first, then registered platforms
    cands = media_paths.candidate_relpaths("123", None)
    assert cands[0] == "123.mp4" and "tiktok/123.mp4" in cands, f"got {cands}"

    with tempfile.TemporaryDirectory() as tmp:
        original_media = fyp_cf['paths']['media']
        original_use_gcs = fyp_cf['data_io']['use_gcs_for_media']
        fyp_cf['paths']['media'] = tmp
        fyp_cf['data_io']['use_gcs_for_media'] = False

        try:
            VID_FLAT = "1111111111111111111"
            VID_PLAT = "2222222222222222222"
            VID_BOTH = "3333333333333333333"
            VID_NONE = "4444444444444444444"

            platform_dir = media_paths.ensure_local_platform_dir("tiktok")
            assert os.path.isdir(platform_dir)

            (Path(tmp) / f"{VID_FLAT}.mp4").write_bytes(b"x")
            (Path(platform_dir) / f"{VID_PLAT}.mp4").write_bytes(b"x")
            (Path(tmp) / f"{VID_BOTH}.mp4").write_bytes(b"x")
            (Path(platform_dir) / f"{VID_BOTH}.mp4").write_bytes(b"x")

            # Legacy flat file found via fallback (verified → size included)
            r = media_paths.resolve_media(VID_FLAT, platform="tiktok")
            assert r == {"kind": "local", "path": os.path.join(tmp, f"{VID_FLAT}.mp4"), "size": 1}, r

            # Platform subpath found first
            r = media_paths.resolve_media(VID_PLAT, platform="tiktok")
            assert r == {"kind": "local", "path": os.path.join(platform_dir, f"{VID_PLAT}.mp4"), "size": 1}, r

            # Both present → platform subpath wins
            r = media_paths.resolve_media(VID_BOTH, platform="tiktok")
            assert r["path"] == os.path.join(platform_dir, f"{VID_BOTH}.mp4"), r

            # A valid storage_link short-circuits the probe entirely
            link = os.path.join(tmp, f"{VID_FLAT}.mp4")
            r = media_paths.resolve_media(VID_BOTH, platform="tiktok", storage_link=link)
            assert r == {"kind": "local", "path": link, "size": 1}, r

            # An invalid storage_link falls back to probing
            r = media_paths.resolve_media(VID_PLAT, platform="tiktok", storage_link="/nope/missing.mp4")
            assert r is not None and r["path"].endswith(f"tiktok/{VID_PLAT}.mp4"), r

            # Nothing anywhere → None
            assert media_paths.resolve_media(VID_NONE, platform="tiktok") is None

            # gs:// storage_link parses without verification when check_exists=False
            r = media_paths.resolve_media(
                VID_NONE, storage_link="gs://bkt/media/tiktok/x.mp4", check_exists=False
            )
            assert r == {"kind": "gcs", "bucket_name": "bkt", "blob_name": "media/tiktok/x.mp4"}, r
            assert media_paths.media_gs_uri(r) == "gs://bkt/media/tiktok/x.mp4"

            print("OK — media path resolution verified")
            return 0
        finally:
            fyp_cf['paths']['media'] = original_media
            fyp_cf['data_io']['use_gcs_for_media'] = original_use_gcs


if __name__ == "__main__":
    sys.exit(main())
