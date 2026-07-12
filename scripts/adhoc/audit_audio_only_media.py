#!/usr/bin/env python3
"""Audit for audio-only "mp4" media objects from the pre-2026-07-04 carousel bug.

Background: before the carousel hardening (commit d335fe3, 2026-07-04),
``fyp/tiktok_dl.py::save_tiktok`` let a TikTok photo post whose page-JSON
image extraction failed fall through to the video-download branch; yt-dlp then
downloaded the audio-only "slideshow audio" format, which was uploaded as
``{item_id}.mp4`` with ``video_downloaded=True`` and ``scrape_status="ok"``.
Those objects play no video and silently poison the viewer and Gemini uploads.

Two phases:

1. **Scan** (default, or ``--sweep``). Two candidate sources:

   - Default: rows in ``recoded/scrapes_recoded.parquet`` with
     ``video_downloaded=True`` and ``scrape_ts`` in the yt-dlp era
     (``[--since, --until)``; the ~1M pre-migration rows carry a backfilled
     2026-04-10 stamp, so the default 2026-04-13 floor excludes them). Each
     candidate's media object is located the same way ``fyp/media_paths.py``
     does — the row's ``storage_link`` first, then the legacy flat and
     platform GCS paths.
   - ``--sweep``: every ``.mp4`` under the bucket's media prefix whose GCS
     creation time falls in the window. This is the authoritative set — it
     covers media uploaded by prod scrapes that a stale local parquet mirror
     has not caught up with (in 2026-07 the local mirror held 908 of the
     ~29.8k in-window objects).

   Each object is ffprobed over an authenticated ranged-HTTP URL, so only the
   container headers are transferred, not the full file. An object is
   confirmed audio-only when it has no real video stream (attached-picture /
   cover-art streams are ignored). The report lands in
   ``tmp/audio_only_media_audit.json``.

2. **Delete + re-queue** (``--delete``, run after reviewing the report).
   Deletes the confirmed objects (GCS blobs and any local media copies) and
   appends the item ids to the TikTok scrape queue (``to_scrape_tiktok.json``)
   so the fixed pipeline regenerates proper slideshows, now with audio. By
   default the ids go to the **prod** queue — a direct read-modify-write of
   ``gs://<bucket>/<gcs_data_prefix>/cache/to_scrape_tiktok.json`` — because
   the affected media comes from prod scrapes; ``--requeue-target config``
   writes through ``fyp.scrape_queues`` (the active config's cache location)
   instead. Asks for interactive confirmation unless ``--yes`` is passed.
   Avoid running it while a prod scraper worker is draining the queue (the
   read-modify-write could race the worker's own queue save).

Usage:
    source .venv/bin/activate
    python tests/audit_audio_only_media.py --sweep        # full bucket audit
    python tests/audit_audio_only_media.py                # parquet-driven scan
    python tests/audit_audio_only_media.py --delete       # act on the report

Notes:
    - Needs ``ffprobe`` on PATH and GCS application-default credentials.
    - The GCS bucket is taken from the rows' ``storage_link`` values (or
      ``--bucket`` / ``FYP_GCS_BUCKET_NAME``), so the scan works even when the
      local config could not initialise its GCS handle.
    - The re-queue writes through ``fyp.data_io``, so it lands wherever the
      active config keeps the ``cache`` location (local dir in local mode, the
      GCS bucket when ``use_gcs_for_data`` is on). Run it under the same
      config as the scraper worker that should pick the ids up.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fyp.fyp_config as fyp_config  # noqa: E402  (auto-initializes on import)
from fyp import data_io  # noqa: E402
from fyp import scrape_queues  # noqa: E402
from fyp.fyp_config import fyp_cf  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_PATH = os.path.join(PROJECT_ROOT, "tmp", "audio_only_media_audit.json")
SCRAPES_FILE = "scrapes_recoded.parquet"
PLATFORM = "tiktok"

# Cover-art codecs: a "video" stream in one of these is an embedded thumbnail,
# not a playable video track.
ATTACHED_PIC_CODECS = {"mjpeg", "png", "bmp", "gif"}





class TokenCache:
    """Thread-safe access-token provider that refreshes before expiry.

    Long sweeps (~30+ min) outlive a single OAuth2 token, so workers fetch
    through this cache instead of holding one token for the whole run.
    """

    REFRESH_AFTER_S = 45 * 60

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token = ""
        self._fetched_at = 0.0





    def get(self) -> str:
        """Return a valid bearer token, refreshing when stale."""
        with self._lock:
            if not self._token or time.time() - self._fetched_at > self.REFRESH_AFTER_S:
                self._token = get_access_token()
                self._fetched_at = time.time()
            return self._token





def get_access_token() -> str:
    """Return an OAuth2 access token for GCS ranged-HTTP reads.

    Tries application-default credentials first, then the gcloud CLI.

    Returns:
        A bearer token string.

    Raises:
        RuntimeError: If no credential source yields a token.
    """
    try:
        import google.auth
        from google.auth.transport.requests import Request

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/devstorage.read_only"]
        )
        creds.refresh(Request())
        if creds.token:
            return creds.token
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        token = out.stdout.strip()
        if token:
            return token
    except Exception:
        pass
    raise RuntimeError("Could not obtain a GCS access token (ADC or gcloud).")





def parse_gs_link(storage_link: str) -> tuple[str, str] | None:
    """Split a ``gs://bucket/blob`` link into its bucket and blob parts.

    Args:
        storage_link: The row's stored link (may be empty or non-GCS).

    Returns:
        ``(bucket_name, blob_name)``, or ``None`` when the link is not a
        well-formed ``gs://`` URI.
    """
    if not isinstance(storage_link, str) or not storage_link.startswith("gs://"):
        return None
    bucket_name, _, blob_name = storage_link[len("gs://"):].partition("/")
    if not bucket_name or not blob_name:
        return None
    return bucket_name, blob_name





def candidate_blob_names(item_id: str, media_prefix: str) -> list[str]:
    """Return GCS blob names to probe for one item, in resolution order.

    Mirrors ``fyp.media_paths.candidate_relpaths``: the legacy flat path
    (where all pre-fix objects live) and the platform subpath.

    Args:
        item_id: The item whose media is being located.
        media_prefix: The bucket's media prefix (config ``gcs_media_prefix``).

    Returns:
        Blob names relative to the bucket root.
    """
    return [
        f"{media_prefix}/{item_id}.mp4",
        f"{media_prefix}/{PLATFORM}/{item_id}.mp4",
    ]





def classify_streams(probe: dict) -> tuple[str, dict]:
    """Classify one ffprobe result as real video vs audio-only.

    Args:
        probe: Parsed ffprobe JSON (``-show_streams -show_format``).

    Returns:
        A ``(status, details)`` pair. ``status`` is ``"ok_video"``,
        ``"audio_only"`` or ``"no_streams"``; ``details`` carries the codec
        names and probed duration for the report.
    """
    streams = probe.get("streams", [])
    video_codecs: list[str] = []
    audio_codecs: list[str] = []
    for s in streams:
        codec_type = s.get("codec_type")
        codec_name = s.get("codec_name", "?")
        if codec_type == "video":
            attached = s.get("disposition", {}).get("attached_pic") == 1
            if not attached and codec_name not in ATTACHED_PIC_CODECS:
                video_codecs.append(codec_name)
        elif codec_type == "audio":
            audio_codecs.append(codec_name)
    duration = probe.get("format", {}).get("duration")
    details = {
        "video_codecs": video_codecs,
        "audio_codecs": audio_codecs,
        "probed_duration_s": float(duration) if duration else None,
    }
    if video_codecs:
        return "ok_video", details
    if audio_codecs:
        return "audio_only", details
    return "no_streams", details





def run_ffprobe(target: str, headers: str | None = None) -> dict | None:
    """Run ffprobe against a URL or local path and return the parsed JSON.

    Args:
        target: HTTP(S) URL or local file path.
        headers: Optional raw HTTP header block for URL probes.

    Returns:
        The parsed ffprobe output, or ``None`` when the probe fails.
    """
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format"]
    if headers:
        cmd += ["-headers", headers]
    cmd.append(target)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None





def probe_blob(blob, tokens: TokenCache, record: dict) -> dict:
    """ffprobe one located blob and fill in the audit record.

    Args:
        blob: A ``google.cloud.storage`` blob handle (existence verified).
        tokens: Access-token cache for the ranged-HTTP probe.
        record: Partially-filled audit record to complete.

    Returns:
        The completed audit record (``status`` plus size/codec details).
    """
    record["blob_name"] = blob.name
    record["size_bytes"] = blob.size

    # Ranged-HTTP probe: ffprobe seeks within the object (headers + moov atom
    # only), so nothing close to the full file is transferred.
    url = (f"https://storage.googleapis.com/{blob.bucket.name}/"
           f"{urllib.parse.quote(blob.name, safe='/')}")
    probe = run_ffprobe(url, headers=f"Authorization: Bearer {tokens.get()}\r\n")
    probe_source = "http"
    if probe is None:
        # Fallback: download the object and probe it locally.
        probe_source = "download"
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=True) as tf:
                blob.download_to_filename(tf.name)
                probe = run_ffprobe(tf.name)
        except Exception:
            probe = None
    if probe is None:
        record["status"] = "probe_failed"
        return record

    status, details = classify_streams(probe)
    record["status"] = status
    record["probe_source"] = probe_source
    record.update(details)
    return record





def probe_item(row: dict, bucket, media_prefix: str, tokens: TokenCache) -> dict:
    """Locate and ffprobe one parquet candidate's media object.

    Args:
        row: Candidate record (``item_id``, ``storage_link``, ``scrape_ts``,
            ``has_images``).
        bucket: A ``google.cloud.storage`` bucket handle.
        media_prefix: The bucket's media prefix.
        tokens: Access-token cache for the ranged-HTTP probe.

    Returns:
        The audit record for this item (``status`` plus location/size/codec
        details).
    """
    item_id = row["item_id"]
    record = {
        "item_id": item_id,
        "scrape_ts": row["scrape_ts"],
        "has_images": row["has_images"],
        "storage_link": row["storage_link"],
        "blob_name": None,
        "size_bytes": None,
        "status": "missing",
    }

    # Locate the blob: storage_link first, then the flat/platform paths.
    names: list[str] = []
    linked = parse_gs_link(row["storage_link"])
    if linked is not None:
        names.append(linked[1])
    for name in candidate_blob_names(item_id, media_prefix):
        if name not in names:
            names.append(name)
    blob = None
    for name in names:
        found = bucket.get_blob(name)
        if found is not None:
            blob = found
            break
    if blob is None:
        return record
    return probe_blob(blob, tokens, record)





def load_candidates(since: str, until: str) -> pd.DataFrame:
    """Load the yt-dlp-era downloaded rows from the scrapes parquet.

    Args:
        since: Inclusive ``scrape_ts`` floor (ISO date).
        until: Exclusive ``scrape_ts`` ceiling (ISO date).

    Returns:
        One row per candidate with ``item_id``, ``storage_link``,
        ``scrape_ts`` and ``has_images``.
    """
    columns = ["item_id", "video_downloaded", "scrape_ts", "storage_link",
               "image_list"]
    available = data_io.get_parquet_columns("recoded", SCRAPES_FILE) or []
    if "source_platform" in available:
        columns.append("source_platform")
    df = data_io.load_parquet_selective("recoded", SCRAPES_FILE, columns=columns)

    mask = df["video_downloaded"].fillna(False).astype(bool)
    ts = pd.to_datetime(df["scrape_ts"], errors="coerce").astype("datetime64[ns]")
    mask &= (ts >= pd.Timestamp(since)) & (ts < pd.Timestamp(until))
    if "source_platform" in df.columns:
        plat = df["source_platform"].fillna("").astype(str)
        mask &= plat.isin(["", PLATFORM])
    out = df.loc[mask, ["item_id", "storage_link", "image_list"]].copy()
    out["item_id"] = out["item_id"].astype(str)
    out["scrape_ts"] = ts[mask].dt.strftime("%Y-%m-%d %H:%M:%S")
    out["storage_link"] = out["storage_link"].fillna("").astype(str)
    # image_list is a JSON-stringified list; anything beyond "[]" marks a
    # photo post (the population the bug hit).
    out["has_images"] = (
        out["image_list"].fillna("").astype(str).str.strip().str.len() > 2
    )
    out = out.drop(columns=["image_list"]).drop_duplicates(subset="item_id")
    return out.reset_index(drop=True)





def resolve_bucket_name(args: argparse.Namespace,
                        candidates: pd.DataFrame | None = None) -> str:
    """Resolve the media bucket name from CLI, env or the rows' links.

    Args:
        args: Parsed CLI arguments.
        candidates: Optional candidate frame whose ``storage_link`` values
            can name the bucket.

    Returns:
        The bucket name.

    Raises:
        SystemExit: When no source names a bucket.
    """
    bucket_name = args.bucket or os.environ.get("FYP_GCS_BUCKET_NAME", "")
    if not bucket_name and candidates is not None:
        for link in candidates["storage_link"]:
            parsed = parse_gs_link(link)
            if parsed is not None:
                bucket_name = parsed[0]
                break
    if not bucket_name:
        raise SystemExit("No GCS bucket name (use --bucket or "
                         "FYP_GCS_BUCKET_NAME).")
    return bucket_name





def report_and_save(results: list[dict], args: argparse.Namespace,
                    bucket_name: str, media_prefix: str, mode: str,
                    n_candidates: int) -> None:
    """Aggregate probe results, print the summary and write the report.

    Args:
        results: Per-object audit records.
        args: Parsed CLI arguments (window bounds).
        bucket_name: The audited bucket.
        media_prefix: The bucket's media prefix.
        mode: ``"parquet"`` or ``"sweep"`` (recorded in the report).
        n_candidates: Size of the candidate set.
    """
    by_status: dict[str, list[dict]] = {}
    for rec in results:
        by_status.setdefault(rec["status"], []).append(rec)
    confirmed = sorted(by_status.get("audio_only", []),
                       key=lambda r: r["size_bytes"] or 0)

    print("\n=== Audit summary ===")
    for status in ("ok_video", "audio_only", "no_streams", "probe_failed",
                   "missing"):
        print(f"  {status:>13}: {len(by_status.get(status, []))}")

    if confirmed:
        print("\nConfirmed audio-only objects (masquerading as mp4):")
        print(f"  {'item_id':<22} {'size':>10} {'dur_s':>7} "
              f"{'photo?':>6}  blob")
        for rec in confirmed:
            dur = rec.get("probed_duration_s")
            print(f"  {rec['item_id']:<22} {rec['size_bytes'] or 0:>10,} "
                  f"{dur if dur is not None else '?':>7} "
                  f"{'yes' if rec['has_images'] else 'no':>6}  "
                  f"{rec['blob_name']}")

    report = {
        "generated_by": "tests/audit_audio_only_media.py",
        "mode": mode,
        "window": {"since": args.since, "until": args.until},
        "bucket": bucket_name,
        "media_prefix": media_prefix,
        "candidates": n_candidates,
        "summary": {k: len(v) for k, v in by_status.items()},
        "confirmed_audio_only": confirmed,
        "needs_attention": (by_status.get("no_streams", [])
                            + by_status.get("probe_failed", [])
                            + by_status.get("missing", [])),
    }
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to {REPORT_PATH}")
    if confirmed:
        print("Review it, then run with --delete to remove the bad objects "
              "and re-queue the ids.")





def scan(args: argparse.Namespace) -> None:
    """Parquet-driven scan: probe the local mirror's candidate rows.

    Args:
        args: Parsed CLI arguments (window, bucket override, worker count).
    """
    from google.cloud import storage

    candidates = load_candidates(args.since, args.until)
    print(f"Candidates (downloaded, scraped {args.since}..{args.until}): "
          f"{len(candidates):,}")
    if candidates.empty:
        print("Nothing to audit.")
        return

    bucket_name = resolve_bucket_name(args, candidates)
    media_prefix = fyp_cf["data_io"].get("gcs_media_prefix", "media")
    print(f"Bucket: gs://{bucket_name}, media prefix: {media_prefix}/")

    bucket = storage.Client().bucket(bucket_name)
    tokens = TokenCache()

    rows = candidates.to_dict("records")
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(probe_item, row, bucket, media_prefix, tokens)
                   for row in rows]
        for i, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if i % 50 == 0 or i == len(futures):
                print(f"  probed {i}/{len(futures)}")
    report_and_save(results, args, bucket_name, media_prefix, "parquet",
                    len(candidates))





def sweep(args: argparse.Namespace) -> None:
    """Bucket-driven audit: probe every in-window mp4 under the media prefix.

    Uses GCS object creation time (media is uploaded at scrape time) to bound
    the candidate set, so it covers prod scrapes the local parquet mirror has
    not caught up with.

    Args:
        args: Parsed CLI arguments (window, bucket override, worker count).
    """
    from google.cloud import storage

    # Loaded up front both to enrich records and to name the bucket when
    # neither --bucket nor FYP_GCS_BUCKET_NAME is set.
    candidates = load_candidates(args.since, args.until)
    bucket_name = resolve_bucket_name(args, candidates)
    media_prefix = fyp_cf["data_io"].get("gcs_media_prefix", "media")
    print(f"Sweeping gs://{bucket_name}/{media_prefix}/ for mp4s created "
          f"{args.since}..{args.until} ...")

    since = datetime.datetime.fromisoformat(args.since).replace(
        tzinfo=datetime.timezone.utc)
    until = datetime.datetime.fromisoformat(args.until).replace(
        tzinfo=datetime.timezone.utc)
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blobs = [
        blob for blob in client.list_blobs(bucket_name,
                                           prefix=f"{media_prefix}/")
        if blob.name.endswith(".mp4") and since <= blob.time_created < until
    ]
    print(f"In-window mp4 objects: {len(blobs):,}")
    if not blobs:
        print("Nothing to audit.")
        return

    # Enrich records with row metadata where the local mirror has the item.
    parquet_info = {row["item_id"]: row
                    for row in candidates.to_dict("records")}

    def make_record(blob) -> dict:
        item_id = os.path.splitext(os.path.basename(blob.name))[0]
        row = parquet_info.get(item_id, {})
        return {
            "item_id": item_id,
            "scrape_ts": row.get("scrape_ts"),
            "has_images": row.get("has_images"),
            "storage_link": row.get("storage_link"),
            "in_local_parquet": bool(row),
            "status": "missing",
        }

    tokens = TokenCache()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(probe_blob, blob, tokens, make_record(blob))
                   for blob in blobs]
        for i, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if i % 500 == 0 or i == len(futures):
                print(f"  probed {i}/{len(futures)}")
    report_and_save(results, args, bucket_name, media_prefix, "sweep",
                    len(blobs))





def requeue_prod_gcs(ids: list[str], bucket) -> tuple[str, int]:
    """Append item ids to the prod scrape queue blob on GCS.

    Locally the config's cache location is a local directory the prod worker
    never reads, so the re-queue writes the prod queue blob directly
    (read-modify-write with dedup; the file may be absent when the queue is
    drained).

    Args:
        ids: Item ids to queue.
        bucket: The prod data bucket handle.

    Returns:
        ``(blob_name, queue_length_after_append)``.
    """
    prefix = fyp_cf["data_io"].get("gcs_data_prefix", "data")
    blob_name = f"{prefix}/cache/{scrape_queues.queue_filename(PLATFORM)}"
    blob = bucket.blob(blob_name)
    current: list[str] = []
    if blob.exists():
        try:
            current = json.loads(blob.download_as_bytes())
        except Exception:
            current = []
        if not isinstance(current, list):
            current = []
    merged = list(dict.fromkeys([str(i) for i in current] + list(ids)))
    blob.upload_from_string(json.dumps(merged),
                            content_type="application/json")
    return blob_name, len(merged)





def delete_and_requeue(args: argparse.Namespace) -> None:
    """Delete confirmed audio-only objects and re-queue their item ids.

    Args:
        args: Parsed CLI arguments (``--yes`` skips the confirmation prompt).
    """
    from google.cloud import storage

    if not os.path.exists(REPORT_PATH):
        raise SystemExit(f"No report at {REPORT_PATH} — run the scan first.")
    with open(REPORT_PATH) as f:
        report = json.load(f)
    confirmed = report.get("confirmed_audio_only", [])
    if not confirmed:
        print("Report contains no confirmed audio-only objects — nothing to do.")
        return

    total_bytes = sum(rec.get("size_bytes") or 0 for rec in confirmed)
    print(f"About to DELETE {len(confirmed)} objects "
          f"({total_bytes / 1e6:.1f} MB) from gs://{report['bucket']} and "
          f"re-queue the ids on {scrape_queues.queue_filename(PLATFORM)}.")
    for rec in confirmed:
        print(f"  gs://{report['bucket']}/{rec['blob_name']}")
    if not args.yes:
        try:
            answer = input("Type 'DELETE' to proceed: ").strip()
        except EOFError:
            answer = ""
        if answer != "DELETE":
            print("Aborted.")
            return

    bucket = storage.Client().bucket(report["bucket"])
    local_media_dir = fyp_cf.get("paths", {}).get("media", "")
    deleted, missing = 0, 0
    for rec in confirmed:
        blob = bucket.blob(rec["blob_name"])
        try:
            blob.delete()
            deleted += 1
        except Exception as exc:
            missing += 1
            print(f"  ! could not delete {rec['blob_name']}: {exc}")
        # Remove any local copies too (flat and platform layouts).
        if local_media_dir:
            for rel in (f"{rec['item_id']}.mp4",
                        f"{PLATFORM}/{rec['item_id']}.mp4"):
                path = os.path.join(local_media_dir, rel)
                if os.path.exists(path):
                    os.remove(path)
                    print(f"  removed local copy {path}")
    print(f"Deleted {deleted} objects ({missing} failed/missing).")

    ids = [rec["item_id"] for rec in confirmed]
    if args.requeue_target == "prod":
        blob_name, queue_len = requeue_prod_gcs(ids, bucket)
        print(f"Re-queued {len(ids)} ids on gs://{report['bucket']}/"
              f"{blob_name} — queue length now {queue_len}.")
    else:
        queue_len = scrape_queues.append_to_scrape_queue(PLATFORM, ids)
        mode = "GCS" if fyp_cf["data_io"].get("use_gcs_for_cache") else "local"
        print(f"Re-queued {len(ids)} ids on "
              f"{scrape_queues.queue_filename(PLATFORM)} "
              f"({mode} cache location) — queue length now {queue_len}.")
    print("Next: start the TikTok scraper worker, then Consolidate & Refresh "
          "so the rows' storage_link/video_downloaded are regenerated.")





def main() -> None:
    """Parse arguments and dispatch to the scan or delete phase."""
    parser = argparse.ArgumentParser(
        description="Audit (and optionally purge + re-queue) audio-only "
                    "media objects from the pre-2026-07-04 carousel bug.")
    parser.add_argument("--since", default="2026-04-13",
                        help="Inclusive scrape_ts floor (yt-dlp migration).")
    parser.add_argument("--until", default="2026-07-05",
                        help="Exclusive scrape_ts ceiling (day after the fix).")
    parser.add_argument("--bucket", default="",
                        help="GCS bucket override (default: from storage_link "
                             "rows or FYP_GCS_BUCKET_NAME).")
    parser.add_argument("--workers", type=int, default=8,
                        help="Concurrent ffprobe workers.")
    parser.add_argument("--sweep", action="store_true",
                        help="Audit every in-window mp4 in the bucket "
                             "instead of the local parquet's candidate rows.")
    parser.add_argument("--delete", action="store_true",
                        help="Act on the existing report: delete confirmed "
                             "objects and re-queue the ids.")
    parser.add_argument("--requeue-target", choices=["prod", "config"],
                        default="prod",
                        help="Where --delete re-queues the ids: the prod GCS "
                             "queue blob (default) or the active config's "
                             "cache location via fyp.scrape_queues.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the interactive confirmation in --delete.")
    args = parser.parse_args()

    if args.delete:
        delete_and_requeue(args)
    elif args.sweep:
        sweep(args)
    else:
        scan(args)





if __name__ == "__main__":
    main()
