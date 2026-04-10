"""Search all files in ~/fyp_local for the string 'other things-+-'."""

import json
import os
import sys
from pathlib import Path

import pandas as pd


SEARCH_STRING = "other things-+-"
SEARCH_LOWER = SEARCH_STRING.lower()
ROOT = Path.home() / "fyp_local"


def search_parquet(filepath: str) -> list[str]:
    """Search a parquet file for the target string (case-insensitive)."""
    hits = []
    try:
        import pyarrow.parquet as pq
        table = pq.read_table(filepath)
        for col_name in table.column_names:
            if SEARCH_LOWER in col_name.lower():
                hits.append(f"column name: '{col_name}'")
            col = table.column(col_name)
            # Convert entire column to string representation and search
            for chunk in col.chunks:
                for val in chunk.to_pylist():
                    if val is None:
                        continue
                    if SEARCH_LOWER in str(val).lower():
                        hits.append(f"column '{col_name}' values")
                        break
                else:
                    continue
                break
    except Exception as e:
        hits.append(f"ERROR reading: {e}")
    return hits


def search_json(filepath: str) -> list[str]:
    """Search a JSON file for the target string (case-insensitive)."""
    hits = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        if SEARCH_LOWER in text.lower():
            hits.append("found in file content")
    except Exception as e:
        hits.append(f"ERROR reading: {e}")
    return hits


def search_ndjson(filepath: str) -> list[str]:
    """Search an NDJSON file for the target string (case-insensitive)."""
    hits = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if SEARCH_LOWER in line.lower():
                    hits.append(f"found on line {i}")
                    if len(hits) >= 5:
                        hits.append("... (truncated)")
                        break
    except Exception as e:
        hits.append(f"ERROR reading: {e}")
    return hits


def search_csv(filepath: str) -> list[str]:
    """Search a CSV file for the target string (case-insensitive)."""
    hits = []
    try:
        df = pd.read_csv(filepath)
        for col in df.columns:
            if SEARCH_LOWER in col.lower():
                hits.append(f"column name: '{col}'")
            if df[col].astype(str).str.lower().str.contains(SEARCH_LOWER, na=False, regex=False).any():
                hits.append(f"column '{col}' values")
    except Exception as e:
        hits.append(f"ERROR reading: {e}")
    return hits


def search_raw(filepath: str) -> list[str]:
    """Search any other file as raw text (case-insensitive)."""
    hits = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        if SEARCH_LOWER in text.lower():
            hits.append("found in file content")
    except Exception as e:
        hits.append(f"ERROR reading: {e}")
    return hits


def main() -> None:
    results: dict[str, list[str]] = {}
    file_count = 0
    extensions_seen: dict[str, int] = {}

    for dirpath, _, filenames in os.walk(ROOT):
        for fname in filenames:
            if fname == ".DS_Store":
                continue
            filepath = os.path.join(dirpath, fname)
            file_count += 1
            ext = Path(fname).suffix.lower()
            extensions_seen[ext] = extensions_seen.get(ext, 0) + 1

            if ext == ".parquet":
                hits = search_parquet(filepath)
            elif ext == ".json":
                hits = search_json(filepath)
            elif ext == ".ndjson":
                hits = search_ndjson(filepath)
            elif ext == ".csv":
                hits = search_csv(filepath)
            else:
                hits = search_raw(filepath)

            if hits:
                rel = os.path.relpath(filepath, ROOT)
                results[rel] = hits

            if file_count % 100 == 0:
                print(f"  scanned {file_count} files...", file=sys.stderr)

    print(f"\nScanned {file_count} files in {ROOT}")
    print(f"File types: {dict(sorted(extensions_seen.items(), key=lambda x: -x[1]))}")
    print(f"\nSearching for: '{SEARCH_STRING}'")
    print(f"{'=' * 60}")

    if not results:
        print("No matches found.")
    else:
        print(f"Found matches in {len(results)} file(s):\n")
        for fpath, hits in sorted(results.items()):
            print(f"  {fpath}")
            for h in hits:
                print(f"    → {h}")


if __name__ == "__main__":
    main()
