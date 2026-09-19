#!/usr/bin/env python3
"""List TikTok DDP raw files in ddp_raw that carry no watch history.

The trap this script exists to avoid: a TikTok export holds a ``VideoList``
key under *two* unrelated sections, and only one of them is watch history.

    Your Activity -> Watch History -> VideoList    <- videos the donor WATCHED
    Post          -> Posts         -> VideoList    <- videos the donor POSTED

Matching on the ``VideoList`` key alone (which is all the immediate-parent
DFS in TikTokDDPCollection.load_single_raw looks at) counts a donor's own
uploads as watch history. This script keys off the *section* that holds the
list instead, so a file whose only VideoList sits under Posts is correctly
reported as having no watch history.

Three ways watch history can be absent, reported separately because they
mean different things:
  - section present, list is null   -> donor withheld it, or TikTok sent none
  - section absent entirely         -> not requested in the export
  - VideoList under a section this script does not know -> flagged UNKNOWN,
    never silently counted as missing (guards against an export vintage
    renaming the section)

Only reads (listdir + load_json) from wherever "ddp_raw" resolves - writes
nothing.

Usage:
    source .venv/bin/activate
    python scripts/check_watch_history_missing.py

Storage resolves the same way the app's does. To read a bucket rather than
the local data directory, point FYP_CONFIG_PATH at a config whose
``[data_io] use_gcs_for_data`` is true (a worktree has no
``config/config.local.toml`` of its own, so it defaults to local storage).
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import fyp.data_io as data_io  # noqa: E402

# Bookkeeping file that lives beside the donor files in every raw location
# (fyp/ingest/base.py) - not a donation.
MANIFEST_FILENAME = "ingestion_manifest.json"

# Sections whose VideoList IS watch history, across export vintages:
# "Watch History" is current (under "Your Activity"), "Video Browsing
# History" is the older name (under "Activity").
WATCH_SECTIONS = {"watchhistory", "videobrowsinghistory"}
# Sections whose VideoList is explicitly NOT watch history.
POSTED_SECTIONS = {"posts", "videos"}


def _norm(key: str) -> str:
    """Fold a section name to a comparable form ("Watch History" -> watchhistory)."""
    return "".join(str(key).split()).lower()


def find_video_lists(root: dict) -> list[tuple[str, str, object]]:
    """Return (path, section_key, value) for every key named VideoList.

    The section key is the key of the dict that holds the VideoList - the
    discriminator between watched and posted videos.
    """
    found = []
    stack = deque([("", "", root)])
    while stack:
        path, section, obj = stack.pop()
        if isinstance(obj, dict):
            for k, v in obj.items():
                sub = f"{path}/{k}"
                if _norm(k) == "videolist":
                    found.append((sub, section, v))
                else:
                    stack.append((sub, k, v))
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                if isinstance(item, (dict, list)):
                    stack.append((f"{path}[{i}]", section, item))
    return found


def _rows(value: object) -> int:
    """Count non-empty dict records, matching what the ingester treats as a row."""
    if not isinstance(value, list):
        return 0
    return sum(1 for item in value if isinstance(item, dict) and item)


def classify(donation: dict) -> dict:
    """Summarise one raw file's watch-history state."""
    watched = posted = 0
    section_seen = False
    null_section = False
    unknown: list[str] = []

    for path, section, value in find_video_lists(donation):
        norm = _norm(section)
        if norm in WATCH_SECTIONS:
            section_seen = True
            watched += _rows(value)
            if value is None or (isinstance(value, list) and not value):
                null_section = True
        elif norm in POSTED_SECTIONS:
            posted += _rows(value)
        else:
            unknown.append(f"{path} (section '{section}', {_rows(value)} rows)")

    if watched > 0:
        state = "ok"
    elif unknown:
        state = "unknown"
    elif section_seen and null_section:
        state = "missing_null"
    else:
        state = "missing_absent"

    return {"state": state, "watched": watched, "posted": posted, "unknown": unknown}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--storage-location", default="ddp_raw")
    parser.add_argument(
        "--min-rows", type=int, default=11,
        help="files with fewer watch-history rows than this are flagged LOW "
             "(the ingester silently discards a file with 10 or fewer)",
    )
    parser.add_argument("--limit", type=int, default=None, help="only check the first N files")
    args = parser.parse_args()

    files = sorted(
        f for f in data_io.listdir(storage_location=args.storage_location)
        if f.endswith(".json") and f != MANIFEST_FILENAME
    )
    if args.limit:
        files = files[: args.limit]

    if not files:
        print(f"No .json files found in '{args.storage_location}'.")
        return

    print(f"Checking {len(files)} file(s) in '{args.storage_location}'...")

    missing, low, unknown, unreadable, ok = [], [], [], [], []
    for i, fname in enumerate(files, 1):
        donation = data_io.load_json(storage_location=args.storage_location, filename=fname)
        if not isinstance(donation, dict):
            unreadable.append(fname)
            continue
        res = classify(donation)
        if res["state"] == "unknown":
            unknown.append((fname, res))
        elif res["state"].startswith("missing"):
            missing.append((fname, res))
        elif res["watched"] < args.min_rows:
            low.append((fname, res))
        else:
            ok.append((fname, res))
        if i % 200 == 0:
            print(f"  ...{i}/{len(files)}")

    def _note(res: dict) -> str:
        why = "section present but empty/null" if res["state"] == "missing_null" else "section absent"
        extra = f", but {res['posted']:,} POSTED videos" if res["posted"] else ""
        return f"{why}{extra}"

    if missing:
        print(f"\nNO WATCH HISTORY ({len(missing)}):")
        for fname, res in missing:
            print(f"  {fname}\n      {_note(res)}")

    if low:
        print(f"\nLOW - under the ingester's {args.min_rows}-row floor ({len(low)}):")
        for fname, res in low:
            print(f"  {fname}  ({res['watched']} watched rows)")

    if unknown:
        print(f"\nUNKNOWN section holding a VideoList - check by hand ({len(unknown)}):")
        for fname, res in unknown:
            for u in res["unknown"]:
                print(f"  {fname}\n      {u}")

    if unreadable:
        print(f"\nUNREADABLE - not a JSON object, e.g. a zip uploaded as .json ({len(unreadable)}):")
        for fname in unreadable:
            print(f"  {fname}")

    posted_as_plays = sum(res["posted"] for _, res in ok + low + missing + unknown)
    print(
        f"\nOK: {len(ok)}  Low: {len(low)}  No watch history: {len(missing)}  "
        f"Unknown: {len(unknown)}  Unreadable: {len(unreadable)}  Total: {len(files)}"
    )
    if posted_as_plays:
        print(
            f"Note: {posted_as_plays:,} posted-video rows sit under Post/Posts/VideoList. "
            f"The ingester now books these as excluded-by-design and keeps them out of the "
            f"viability floor, so a donation of nothing but posted videos is discarded at "
            f"load rather than admitted with no viewing at all."
        )


if __name__ == "__main__":
    main()
