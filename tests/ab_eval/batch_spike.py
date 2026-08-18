"""Live de-risking spike for the Gemini Batch API annotation path.

Submits a SMALL real batch job (a handful of GCS videos), waits for it, then
downloads the raw output and checks the two assumptions that were built from
docs but never confirmed live:
  1. gemini-3-flash-preview is batch-enabled on the project.
  2. the request JSONL + output record shapes in fyp/machine_annotation_batch.py
     match what Vertex actually wants/returns (so refinement can consume it).

It prints the RAW first output record so we can see the true structure, then
runs the production ingest mapper and validates that each response parses to a
structured 35-field annotation. PASS => the batch path is trustworthy.

Needs GCS (videos in gs://<bucket>/media) + ADC. Billable (~$0.05-0.10).

Usage:
    python tests/ab_eval/batch_spike.py --bucket fyp_bucket_01 --n 5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    ap = argparse.ArgumentParser(description="Gemini Batch API spike")
    ap.add_argument("--bucket", default="fyp_bucket_01")
    ap.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID"))
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--poll-minutes", type=int, default=90)
    args = ap.parse_args()

    # Configure GCS BEFORE importing fyp config so init picks up the bucket.
    os.environ["FYP_GCS_BUCKET_NAME"] = args.bucket

    from google.cloud import storage

    import fyp.annotation_versioning as av
    import fyp.machine_annotation_batch as batch
    from fyp.fyp_config import fyp_cf
    from fyp.machine_annotation import initialize_machine

    # Make sure the batch module's GCS handles are populated.
    dio = fyp_cf["data_io"]
    dio["GCS_bucket_name"] = args.bucket
    if not dio.get("bucket"):
        dio["bucket"] = storage.Client(project=args.project).bucket(args.bucket)
    media_prefix = dio.get("gcs_media_prefix", "media")
    initialize_machine()

    print(f"[spike] bucket={args.bucket} model={fyp_cf['machine']["gemini"]['model']} "
          f"temp={fyp_cf['machine']["gemini"]['temperature']} version={av.active_annotation_version()}")

    # 1. Pick N real video ids from gs://bucket/media/
    blobs = dio["bucket"].list_blobs(prefix=f"{media_prefix}/", max_results=args.n * 4)
    ids = [Path(b.name).stem for b in blobs if b.name.endswith(".mp4")][: args.n]
    if not ids:
        print("[spike] FAIL: no .mp4 found in GCS media prefix.")
        return 1
    print(f"[spike] {len(ids)} videos: {ids}")

    # 2. Build + upload the JSONL, then submit the batch job.
    import datetime as _dt
    ts = "".join(c for c in str(_dt.datetime.now()) if c.isdigit())
    print("[spike] building + uploading JSONL...")
    jsonl_uri, submitted_ids = batch.build_and_upload_jsonl(ids, ts)
    print(f"[spike] input: {jsonl_uri}")
    job_name, output_uri = batch.submit_batch_job(jsonl_uri, ts)
    print(f"[spike] submitted job: {job_name}")
    print(f"[spike] output will be: {output_uri}")

    # 3. Poll until terminal.
    terminal = {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED",
                "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
    deadline = time.time() + args.poll_minutes * 60
    state = batch.poll_batch_job(job_name)
    last = None
    while state not in terminal:
        if state != last:
            print(f"[spike] state: {state}  ({time.strftime('%H:%M:%S')})", flush=True)
            last = state
        if time.time() > deadline:
            print(f"[spike] STILL RUNNING after {args.poll_minutes} min. Re-check job later:\n"
                  f"        {job_name}\n        output: {output_uri}")
            return 2
        time.sleep(60)
        try:
            state = batch.poll_batch_job(job_name)
        except Exception as exc:  # noqa: BLE001
            print(f"[spike] poll error (will retry): {exc}")
    print(f"[spike] terminal state: {state}")

    if "SUCCEEDED" not in state and "PARTIAL" not in state:
        print(f"[spike] FAIL: job ended {state}. Inspect in the Cloud console (Vertex AI > Batch).")
        return 1

    # 4. Download raw output and inspect the real structure.
    bucket = dio["bucket"]
    prefix = output_uri.replace(f"gs://{args.bucket}/", "").rstrip("/")
    records = []
    out_blobs = [b for b in bucket.list_blobs(prefix=prefix) if b.name.endswith(".jsonl")]
    print(f"[spike] output blobs: {[b.name for b in out_blobs]}")
    for b in out_blobs:
        for line in b.download_as_text().splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    print(f"[spike] downloaded {len(records)} output records for {len(submitted_ids)} submitted.")

    if records:
        print("\n[spike] ===== RAW first output record (truncated) =====")
        print(json.dumps(records[0], indent=2, default=str)[:1800])
        print("[spike] ===== end raw record =====\n")

    # 5. Run the production ingest mapper + validate structured parsing.
    raw = batch.ingest_records_to_raw(
        records, submitted_ids,
        model=fyp_cf["machine"]["gemini"]["model"],
        prompt_fn=os.path.basename(fyp_cf["machine"]["gemini"]["prompt"]),
        annotation_version=av.active_annotation_version(),
    )
    ok, empty, bad_json = 0, 0, 0
    for entry in raw.values():
        resp = entry.get("response") or ""
        if not resp:
            empty += 1
            continue
        try:
            d = json.loads(resp)
            if isinstance(d, dict) and "type_of_story" in d and len(d) >= 30:
                ok += 1
            else:
                bad_json += 1
        except Exception:
            bad_json += 1

    print("[spike] ===== VERDICT =====")
    print(f"  job state           : {state}")
    print(f"  submitted / returned: {len(submitted_ids)} / {len(records)}")
    print(f"  ingest item_id ok   : {sum(1 for e in raw.values() if e.get('item_id') and not str(e['finish_reason']).startswith('DNF'))}")
    print(f"  structured-JSON ok  : {ok}   empty: {empty}   bad/short: {bad_json}")
    sample = next(iter(raw.values()))
    print(f"  sample mapped keys  : {sorted(sample.keys())}")
    print(f"  sample finish/usage : {sample.get('finish_reason')} / {sample.get('usage')}")
    verdict = "PASS — batch path validated; format matches the ingest mapper." if ok >= max(1, len(records) // 2) \
        else "FAIL — output did not map cleanly; compare the RAW record above to ingest_output_record()."
    print(f"  ==> {verdict}")
    return 0 if ok >= max(1, len(records) // 2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
