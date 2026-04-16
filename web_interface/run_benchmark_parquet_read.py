"""Benchmark parquet read paths on the task-runner.

Compares three ways of loading a GCS-hosted parquet into a pandas DataFrame:
  A) pd.read_parquet("gs://...")                -- current path (gcsfs)
  B) blob.download_as_bytes() + pq.read_table   -- bytes-in-memory
  C) blob.download_to_filename() + pd.read_parquet(tmp)  -- tempfile round-trip

Each method is run 3 times; the script logs individual runs and the median.

Trigger via Cloud Task with payload e.g.
  {"storage_location": "recoded", "filename": "machine_annotations_recoded.parquet"}

Defaults to the four recoded parquets that dominate the stats phase.
"""

import io
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def _default_targets():
    """Resolve the real recoded filenames (label values come from config)."""
    from fyp.organize_datasets import (
        COLLECTIONS_LABEL,
        MACHINE_ANNOTATIONS_LABEL,
        SCRAPES_LABEL,
    )
    return [
        ("recoded", f"{SCRAPES_LABEL}_recoded.parquet"),
        ("recoded", f"{MACHINE_ANNOTATIONS_LABEL}_recoded.parquet"),
        ("recoded", f"{COLLECTIONS_LABEL}_recoded.parquet"),
        ("recoded", "enrichment_status.parquet"),
    ]

REPEATS = 3


def _bench_one(storage_location: str, filename: str, reporter: TaskStatusReporter) -> None:
    import pandas as pd
    import pyarrow.parquet as pq

    from fyp.data_io import _get_bucket, _resolve_paths

    gcs_uri, _, mode, blob_name = _resolve_paths(storage_location, filename)
    if mode != 'gcs':
        reporter.log(f"[BENCH] SKIP {storage_location}/{filename} — not in GCS mode")
        return
    bucket = _get_bucket()

    reporter.log(f"[BENCH] ===== {storage_location}/{filename} =====")
    reporter.log(f"[BENCH] gcs_uri={gcs_uri}")

    # Size sanity
    blob_size_mb = None
    try:
        blob = bucket.get_blob(blob_name)
        if blob is not None:
            blob_size_mb = blob.size / (1024 * 1024)
            reporter.log(f"[BENCH] blob size={blob_size_mb:.1f} MB")
    except Exception as e:
        reporter.log(f"[BENCH] size lookup failed: {e}")

    timings = {"A_gcsfs": [], "B_bytes": [], "C_tempfile": []}

    for i in range(REPEATS):
        # --- Method A: gcsfs path (current production behavior) ---
        t0 = time.perf_counter()
        df_a = pd.read_parquet(
            gcs_uri, engine="pyarrow",
            dtype_backend="pyarrow", use_threads=True,
        )
        t_a = time.perf_counter() - t0
        timings["A_gcsfs"].append(t_a)
        rows_a = len(df_a)
        del df_a

        # --- Method B: download_as_bytes + pq.read_table ---
        t0 = time.perf_counter()
        blob = bucket.blob(blob_name)
        raw = blob.download_as_bytes()
        table = pq.read_table(io.BytesIO(raw))
        df_b = table.to_pandas(types_mapper=pd.ArrowDtype)
        t_b = time.perf_counter() - t0
        timings["B_bytes"].append(t_b)
        rows_b = len(df_b)
        del df_b, table, raw

        # --- Method C: download_to_filename + pd.read_parquet(tmp) ---
        t0 = time.perf_counter()
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
                tmp_path = tmp.name
            blob = bucket.blob(blob_name)
            blob.download_to_filename(tmp_path)
            df_c = pd.read_parquet(
                tmp_path, engine="pyarrow",
                dtype_backend="pyarrow", use_threads=True,
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        t_c = time.perf_counter() - t0
        timings["C_tempfile"].append(t_c)
        rows_c = len(df_c)
        del df_c

        reporter.log(
            f"[BENCH] run={i+1}/{REPEATS} "
            f"A_gcsfs={t_a:.2f}s  B_bytes={t_b:.2f}s  C_tempfile={t_c:.2f}s  "
            f"rows(a/b/c)={rows_a}/{rows_b}/{rows_c}"
        )

    # Median summary
    med_a = statistics.median(timings["A_gcsfs"])
    med_b = statistics.median(timings["B_bytes"])
    med_c = statistics.median(timings["C_tempfile"])
    reporter.log(
        f"[BENCH] MEDIAN {filename}: "
        f"A_gcsfs={med_a:.2f}s  B_bytes={med_b:.2f}s  C_tempfile={med_c:.2f}s  "
        f"(B vs A: {med_a/med_b:.2f}x, C vs A: {med_a/med_c:.2f}x)"
    )


def run_benchmark_parquet_read(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    task_args = task_args or {}
    reporter.log("Starting parquet read benchmark...")

    if task_args.get("filename"):
        targets = [(task_args.get("storage_location", "recoded"), task_args["filename"])]
    else:
        targets = _default_targets()

    reporter.log(f"[BENCH] targets={targets}  repeats={REPEATS}")

    for storage_location, filename in targets:
        try:
            _bench_one(storage_location, filename, reporter)
        except Exception as e:
            reporter.log(f"[BENCH] FAILED {storage_location}/{filename}: {e}")

    reporter.log("Benchmark complete.")


if __name__ == "__main__":
    from web_interface.task_status import LocalStatusReporter
    reporter = LocalStatusReporter("benchmark_parquet_read")
    try:
        run_benchmark_parquet_read(reporter=reporter, task_args={})
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
