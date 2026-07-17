"""Build the Qwen-vs-Gemini eval set from prod GCS (read-only).

Selects ~N recently-annotated items whose raw Gemini response used the current
model + active annotation version, stratified across platforms, downloads their
mp4s to a local work dir and saves the parsed Gemini structured responses as
the reference arm.

Run (from the repo root, prod GCS mode):

    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=fyp_bucket_01 \
        PYTHONPATH=. .venv/bin/python scripts/adhoc/qwen_eval/01_build_eval_set.py

Outputs (under --workdir, default ~/qwen_eval_work):
    eval_manifest.json     — [{item_id, source_platform, duration, video_file}]
    gemini_reference.json  — {item_id: {parsed structured response + meta}}
    videos/<platform>_<item_id>.mp4
"""

import argparse
import json
import os
import random
import shutil
import subprocess

import pandas as pd

import fyp.data_io as data_io
import fyp.media_paths as media_paths
from fyp.fyp_config import get_config


def load_recent_raw_pool(model_name: str, max_files: int = 40) -> dict[str, dict]:
    """Parse recent raw archives; return {item_id: row} for clean current-model rows."""
    fns = sorted(
        fn for fn in data_io.listdir(storage_location="machine_annotations_raw")
        if fn.startswith("machine_annotations_") and fn.endswith(".json")
    )
    pool: dict[str, dict] = {}
    for fn in reversed(fns[-max_files:]):
        rows = data_io.load_json(storage_location="machine_annotations_raw", filename=fn)
        kept = 0
        for row in rows.values():
            if row.get("model") != model_name or row.get("error") not in ("", "-"):
                continue
            if not row.get("structured"):
                continue
            try:
                parsed = json.loads(row["response"])
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
            if not isinstance(parsed, dict):
                continue
            item_id = str(row["item_id"])
            if item_id in pool:
                continue
            row["parsed"] = parsed
            pool[item_id] = row
            kept += 1
        print(f"{fn}: kept {kept} (pool now {len(pool)})")
    return pool






def stratified_sample(pool: dict[str, dict], status: pd.DataFrame, n: int,
                      max_duration: float, seed: int) -> pd.DataFrame:
    """Pick n eval items stratified by platform, preferring shorter recent items."""
    status = status.reset_index()
    status["item_id"] = status["item_id"].astype(str)
    cand = status[status["item_id"].isin(pool.keys())]
    if "video_downloaded" in cand.columns:
        cand = cand[cand["video_downloaded"].fillna(False).astype(bool)]
    print(f"candidates after status filters: {len(cand)}")

    rng = random.Random(seed)
    groups = list(cand.groupby("source_platform"))
    picked: list[pd.DataFrame] = []
    remaining = n
    for i, (platform, grp) in enumerate(groups):
        share = max(1, round(n * len(grp) / len(cand)))
        share = min(share, remaining - (len(groups) - 1 - i), len(grp))
        share = max(share, 0)
        idx = rng.sample(list(grp.index), min(share, len(grp)))
        picked.append(grp.loc[idx])
        remaining -= len(idx)
        print(f"  {platform}: {len(idx)} of {len(grp)}")
    out = pd.concat(picked)
    if len(out) < n:
        extra = cand.drop(out.index)
        idx = rng.sample(list(extra.index), min(n - len(out), len(extra)))
        out = pd.concat([out, extra.loc[idx]])
    return out.head(n)






def probe_duration(path: str) -> float | None:
    """Return the media duration in seconds via ffprobe, or None."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return None






def download_videos(items: pd.DataFrame, videos_dir: str, n: int,
                    max_duration: float) -> dict[str, dict]:
    """Download candidates until n pass the duration cap; return {item_id: meta}."""
    kept: dict[str, dict] = {}
    for _, row in items.iterrows():
        if len(kept) >= n:
            break
        item_id = str(row["item_id"])
        platform = str(row["source_platform"])
        dest = os.path.join(videos_dir, f"{platform}_{item_id}.mp4")
        if not os.path.exists(dest):
            resolved = media_paths.resolve_media(item_id, platform=platform)
            if resolved is None:
                print(f"  MISSING media: {platform}/{item_id}")
                continue
            if resolved["kind"] == "local":
                shutil.copyfile(resolved["path"], dest)
            else:
                bucket = get_config()["data_io"]["bucket"]
                bucket.blob(resolved["blob_name"]).download_to_filename(dest)
        duration = probe_duration(dest)
        if duration is None or duration > max_duration:
            print(f"  skipped {platform}/{item_id}: duration={duration}")
            os.remove(dest)
            continue
        kept[item_id] = {"video_file": dest, "duration": duration, "source_platform": platform}
        print(f"  kept {platform}/{item_id} ({duration:.0f}s, {os.path.getsize(dest) / 1e6:.1f} MB)")
    return kept






def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--max-duration", type=float, default=120.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--workdir", default=os.path.expanduser("~/qwen_eval_work"))
    args = ap.parse_args()

    cf = get_config()
    model_name = cf["machine"]["model"]
    print(f"reference model: {model_name}")

    videos_dir = os.path.join(args.workdir, "videos")
    os.makedirs(videos_dir, exist_ok=True)

    pool = load_recent_raw_pool(model_name)
    if not pool:
        raise SystemExit("no clean raw rows found for the current model")

    status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")
    items = stratified_sample(pool, status, args.n * 2, args.max_duration, args.seed)
    print(f"selected {len(items)} candidates (target {args.n})")

    kept = download_videos(items, videos_dir, args.n, args.max_duration)

    manifest = [{"item_id": iid, **meta} for iid, meta in kept.items()]
    with open(os.path.join(args.workdir, "eval_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)

    reference = {iid: pool[iid] for iid in (m["item_id"] for m in manifest)}
    with open(os.path.join(args.workdir, "gemini_reference.json"), "w") as f:
        json.dump(reference, f, indent=1)

    print(f"wrote {len(manifest)} items to {args.workdir}")






if __name__ == "__main__":
    main()
