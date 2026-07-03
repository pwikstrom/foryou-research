"""Verify check_existing_media correctly identifies which video IDs already
have a valid media file on disk, and reports each file's actual location.

Exercises the local-mode branch with a controlled temporary media directory
so we don't depend on the user's actual fyp_local layout. Covers both the
legacy flat layout ({id}.mp4) and the per-platform layout ({platform}/{id}.mp4).
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fyp.scrape as scrape
from fyp.fyp_config import fyp_cf


def main() -> int:
    min_size = fyp_cf['misc']['min_media_object_size']
    print(f"min_media_object_size = {min_size}")

    with tempfile.TemporaryDirectory() as tmp:
        # Save and override the configured media path
        original_media = fyp_cf['paths']['media']
        original_use_gcs = fyp_cf['data_io']['use_gcs_for_media']
        fyp_cf['paths']['media'] = tmp
        fyp_cf['data_io']['use_gcs_for_media'] = False

        try:
            VID_OK = "1111111111111111111"
            VID_TOOSMALL = "2222222222222222222"
            VID_MISSING = "3333333333333333333"
            VID_PLATFORM = "4444444444444444444"

            # Valid-size file at the legacy flat path
            (Path(tmp) / f"{VID_OK}.mp4").write_bytes(b"\0" * (min_size + 100))
            # Under-size file (should NOT count)
            (Path(tmp) / f"{VID_TOOSMALL}.mp4").write_bytes(b"\0" * max(0, min_size - 1))
            # Valid-size file at the per-platform subpath
            (Path(tmp) / "tiktok").mkdir()
            (Path(tmp) / "tiktok" / f"{VID_PLATFORM}.mp4").write_bytes(b"\0" * (min_size + 100))

            present = scrape.check_existing_media(
                [VID_OK, VID_TOOSMALL, VID_MISSING, VID_PLATFORM], platform="tiktok"
            )

            print(f"present = {sorted(present)}")
            assert isinstance(present, dict), "check_existing_media now returns {vid: storage_link}"
            assert VID_OK in present, f"expected {VID_OK} in present"
            assert VID_TOOSMALL not in present, "under-size file should be excluded"
            assert VID_MISSING not in present, "missing file should be excluded"
            assert VID_PLATFORM in present, "platform-subpath file should be found"

            # The reported link is the file's ACTUAL location (legacy flat vs platform)
            assert present[VID_OK] == os.path.join(tmp, f"{VID_OK}.mp4")
            assert present[VID_PLATFORM] == os.path.join(tmp, "tiktok", f"{VID_PLATFORM}.mp4")

            # Without a platform, only the legacy flat path is probed
            flat_only = scrape.check_existing_media([VID_OK, VID_PLATFORM])
            assert VID_OK in flat_only and VID_PLATFORM not in flat_only

            # Empty input → empty dict, no errors
            assert scrape.check_existing_media([]) == {}

            print("OK — check_existing_media local-mode behaviour verified")
            return 0
        finally:
            fyp_cf['paths']['media'] = original_media
            fyp_cf['data_io']['use_gcs_for_media'] = original_use_gcs


if __name__ == "__main__":
    sys.exit(main())
