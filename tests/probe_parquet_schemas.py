"""Probe on-disk schemas of the parquets that are candidates for selective loading.

Reports per file: size on disk, row count, column count, and a sample of
column names (showing the on-disk stringified-tuple form for MultiIndex
columns). Output is plain text, written to stdout AND to
tmp/parquet_schema_probe.txt for the follow-up report.
"""
import os
import sys
from os.path import abspath, dirname, join

sys.path.insert(0, abspath(join(dirname(__file__), '..')))

import pyarrow.parquet as pq
from fyp import fyp_config

fyp_config.initialize()
fyp_cf = fyp_config.fyp_cf

LOCAL_DATA = fyp_cf['paths']['local_data']
COLLECTIONS_LABEL = fyp_cf['labels']['COLLECTIONS_LABEL']

CANDIDATES = [
    ('recoded', f'{COLLECTIONS_LABEL}_metadata.parquet', 'Tier 1: metadata hot path'),
    ('recoded', f'{COLLECTIONS_LABEL}_recoded.parquet', 'Tier 2: events, organize_datasets'),
    ('recoded', 'scrapes_recoded.parquet', 'Tier 2: scrape data, run_timelines_refresh'),
    ('recoded', 'machine_annotations_recoded.parquet', 'Tier 2: annotations, study refresh'),
    ('recoded', 'enrichment_status.parquet', 'Tier 1: enrichment status'),
    ('cache', 'everything_recoded.parquet', 'Tier 2: largest study cache'),
    ('cache', 'paper_three_recoded.parquet', 'Tier 2: large study cache'),
    ('cache', 'chenglong_recoded.parquet', 'Tier 2: medium study cache'),
]


def _resolve_path(storage_location: str, filename: str) -> str:
    return join(LOCAL_DATA, storage_location, filename)


def _fmt_bytes(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def probe(storage_location: str, filename: str, note: str, out):
    path = _resolve_path(storage_location, filename)
    header = f"\n{'=' * 78}\n{storage_location}/{filename}  --  {note}\n{'=' * 78}"
    print(header)
    out.append(header)

    if not os.path.exists(path):
        msg = f"  [SKIP] Not found at {path}"
        print(msg)
        out.append(msg)
        return

    size = os.path.getsize(path)
    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow
    md = pf.metadata
    row_groups = md.num_row_groups
    rows = md.num_rows
    cols = len(schema.names)

    info = (
        f"  size:        {_fmt_bytes(size)}\n"
        f"  rows:        {rows:,}\n"
        f"  columns:     {cols}\n"
        f"  row groups:  {row_groups}\n"
    )
    print(info, end='')
    out.append(info.rstrip())

    print("  column names (first 40):")
    out.append("  column names (first 40):")
    for i, (name, field) in enumerate(zip(schema.names, schema)):
        if i >= 40:
            print(f"    ... and {cols - 40} more")
            out.append(f"    ... and {cols - 40} more")
            break
        line = f"    [{i:3d}] {name!r:60s}  dtype={field.type}"
        print(line)
        out.append(line)


def main():
    out = []
    for storage_location, filename, note in CANDIDATES:
        probe(storage_location, filename, note, out)

    out_path = abspath(join(dirname(__file__), '..', 'tmp', 'parquet_schema_probe.txt'))
    os.makedirs(dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        f.write('\n'.join(out) + '\n')
    print(f"\n[OK] Wrote {out_path}")


if __name__ == '__main__':
    main()
