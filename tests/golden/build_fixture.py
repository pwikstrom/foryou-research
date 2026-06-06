"""Build a small, committable fixture of real raw Gemini responses.

Samples responses across many ``machine_annotations_raw`` files so the golden
corpus exercises a diverse range of model outputs (different content, lengths,
and a few empty / DNF responses to cover the bad path).  Run this once; the
resulting ``fixtures/raw_sample.json`` is committed and treated as frozen.

Usage:
    python tests/golden/build_fixture.py [--target 150] [--per-file 12]

Re-run only when you deliberately want to refresh the sample (then rebuild the
golden snapshot with build_golden.py and review the diff).
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from _harness import FIXTURE_PATH, fyp_cf


def _list_raw_files() -> list[Path]:
    raw_dir = Path(fyp_cf["paths"]["machine_annotations_raw"])
    return sorted(raw_dir.glob("machine_annotations_*.json"))


def build(target: int = 150, per_file: int = 12, seed: int = 7) -> dict:
    rng = random.Random(seed)
    files = _list_raw_files()
    if not files:
        raise SystemExit(
            f"No raw annotation files found in {fyp_cf['paths']['machine_annotations_raw']}"
        )
    rng.shuffle(files)

    collected: dict[str, dict] = {}
    n_empty = 0
    for fn in files:
        if len(collected) >= target:
            break
        try:
            with open(fn, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            print(f"  skip {fn.name}: {exc}")
            continue
        entries = list(data.values())
        rng.shuffle(entries)
        for entry in entries[:per_file]:
            item_id = str(entry.get("item_id"))
            if not item_id or item_id in collected:
                continue
            # Keep a few empty/DNF responses to exercise the bad path, but don't
            # let them dominate the sample.
            is_empty = not entry.get("response")
            if is_empty:
                if n_empty >= max(3, target // 20):
                    continue
                n_empty += 1
            collected[item_id] = entry
            if len(collected) >= target:
                break

    fixture = {str(i): entry for i, entry in enumerate(collected.values())}
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FIXTURE_PATH, "w", encoding="utf-8") as f:
        json.dump(fixture, f, ensure_ascii=False, indent=1)

    size_kb = FIXTURE_PATH.stat().st_size / 1024
    print(
        f"Wrote {len(fixture)} responses ({n_empty} empty/DNF) "
        f"to {FIXTURE_PATH} [{size_kb:.0f} KB] from {len(files)} candidate files."
    )
    return fixture


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the raw-response golden fixture")
    ap.add_argument("--target", type=int, default=150)
    ap.add_argument("--per-file", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    build(target=args.target, per_file=args.per_file, seed=args.seed)
