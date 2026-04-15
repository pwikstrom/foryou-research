"""Verify check_existing_media correctly identifies which video IDs already
have a valid media file on disk.

Exercises the local-mode branch with a controlled temporary media directory
so we don't depend on the user's actual fyp_local layout.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from fyp.fyp_config import fyp_cf
import fyp.scrape as scrape


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

            # Valid-size file
            (Path(tmp) / f"{VID_OK}.mp4").write_bytes(b"\0" * (min_size + 100))
            # Under-size file (should NOT count)
            (Path(tmp) / f"{VID_TOOSMALL}.mp4").write_bytes(b"\0" * max(0, min_size - 1))

            present = scrape.check_existing_media([VID_OK, VID_TOOSMALL, VID_MISSING])

            print(f"present = {sorted(present)}")
            assert VID_OK in present, f"expected {VID_OK} in present"
            assert VID_TOOSMALL not in present, f"under-size file should be excluded"
            assert VID_MISSING not in present, f"missing file should be excluded"

            # Empty input → empty set, no errors
            assert scrape.check_existing_media([]) == set()

            print("OK — check_existing_media local-mode behaviour verified")
            return 0
        finally:
            fyp_cf['paths']['media'] = original_media
            fyp_cf['data_io']['use_gcs_for_media'] = original_use_gcs


if __name__ == "__main__":
    sys.exit(main())
