import json
import pyarrow.parquet as pq
import pyarrow as pa
from collections import defaultdict
from pathlib import Path


# ── Key/column extraction ──────────────────────────────────────────────────────

def extract_keys(obj, prefix="") -> set[str]:
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            full_key = f"{prefix}.{k}" if prefix else k
            keys.add(full_key)
            keys.update(extract_keys(v, full_key))
    elif isinstance(obj, list):
        for item in obj:
            keys.update(extract_keys(item, prefix))
    return keys


def get_parquet_columns(path: str | Path) -> list[dict]:
    root = Path(path)
    results = []
    for f in sorted(root.rglob("*.parquet")):
        schema = pq.read_schema(f)
        results.append({"file": str(f), "columns": schema.names})
    return results


def get_json_keys(path: str | Path) -> list[dict]:
    root = Path(path)
    results = []
    for f in sorted(root.rglob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            results.append({"file": str(f), "keys": sorted(extract_keys(data))})
        except (json.JSONDecodeError, OSError) as e:
            results.append({"file": str(f), "keys": [], "error": str(e)})
    return results


# ── Rename helpers ─────────────────────────────────────────────────────────────

def apply_rename(name: str, rename_map: dict[str, str], strip_prefixes: list[str]) -> str:
    """Apply dictionary rename, then strip any matching leading prefix."""
    name = rename_map.get(name, name)
    for prefix in strip_prefixes:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break  # apply at most one prefix strip
    return name


# ── Conflict detection ─────────────────────────────────────────────────────────

def detect_parquet_conflicts(
    old_names: list[str],
    rename_map: dict[str, str],
    strip_prefixes: list[str],
) -> list[dict]:
    """Return a list of conflicts where two columns would map to the same new name.

    Each entry is {"new_name": str, "originals": [str, ...]}.
    """
    new_to_originals: dict[str, list[str]] = defaultdict(list)
    for col in old_names:
        new_to_originals[apply_rename(col, rename_map, strip_prefixes)].append(col)
    return [
        {"new_name": new, "originals": originals}
        for new, originals in new_to_originals.items()
        if len(originals) > 1
    ]


def detect_json_conflicts(
    obj,
    rename_map: dict[str, str],
    strip_prefixes: list[str],
    path: str = "",
) -> list[dict]:
    """Recursively find dict levels where two keys would map to the same new name.

    Each entry is {"new_name": str, "originals": [str, ...], "path": str}
    where path is the dot-path to the conflicting dict (empty string for root).
    """
    conflicts = []
    if isinstance(obj, dict):
        new_to_originals: dict[str, list[str]] = defaultdict(list)
        for k in obj:
            new_to_originals[apply_rename(k, rename_map, strip_prefixes)].append(k)
        for new_k, originals in new_to_originals.items():
            if len(originals) > 1:
                conflicts.append({"new_name": new_k, "originals": originals, "path": path})
        for k, v in obj.items():
            child_path = f"{path}.{k}" if path else k
            conflicts.extend(detect_json_conflicts(v, rename_map, strip_prefixes, child_path))
    elif isinstance(obj, list):
        for item in obj:
            conflicts.extend(detect_json_conflicts(item, rename_map, strip_prefixes, path))
    return conflicts


# ── Parquet renaming ───────────────────────────────────────────────────────────

def rename_parquet_columns(
    file: str | Path,
    rename_map: dict[str, str],
    strip_prefixes: list[str],
    dry_run: bool = False,
) -> dict:
    file = Path(file)
    table = pq.read_table(file)
    old_names = table.schema.names
    new_names = [apply_rename(col, rename_map, strip_prefixes) for col in old_names]
    changes = {o: n for o, n in zip(old_names, new_names) if o != n}
    conflicts = detect_parquet_conflicts(old_names, rename_map, strip_prefixes)

    if changes and not dry_run:
        if conflicts:
            raise ValueError(
                f"{file}: cannot rename — conflicts would overwrite columns: "
                + ", ".join(f"{c['originals']} → {c['new_name']!r}" for c in conflicts)
            )
        renamed = table.rename_columns(new_names)
        pq.write_table(renamed, file)

    return {"file": str(file), "changes": changes, "conflicts": conflicts}


# ── JSON renaming ──────────────────────────────────────────────────────────────

def rename_json_keys_recursive(obj, rename_map: dict[str, str], strip_prefixes: list[str]):
    """Recursively rename all keys in a JSON structure (applies to individual key names, not dot paths)."""
    if isinstance(obj, dict):
        return {
            apply_rename(k, rename_map, strip_prefixes): rename_json_keys_recursive(v, rename_map, strip_prefixes)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [rename_json_keys_recursive(item, rename_map, strip_prefixes) for item in obj]
    return obj


def rename_json_file_keys(
    file: str | Path,
    rename_map: dict[str, str],
    strip_prefixes: list[str],
    dry_run: bool = False,
) -> dict:
    file = Path(file)
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        conflicts = detect_json_conflicts(data, rename_map, strip_prefixes)

        original_keys = extract_keys(data)
        changes = {
            k: ".".join(apply_rename(part, rename_map, strip_prefixes) for part in k.split("."))
            for k in sorted(original_keys)
            if ".".join(apply_rename(part, rename_map, strip_prefixes) for part in k.split(".")) != k
        }

        if not dry_run:
            if conflicts:
                raise ValueError(
                    f"{file}: cannot rename — conflicts would overwrite keys: "
                    + ", ".join(
                        f"{c['originals']} → {c['new_name']!r} at {c['path']!r}" for c in conflicts
                    )
                )
            renamed_data = rename_json_keys_recursive(data, rename_map, strip_prefixes)
            file.write_text(json.dumps(renamed_data, indent=2, ensure_ascii=False), encoding="utf-8")

        return {"file": str(file), "changes": changes, "conflicts": conflicts}
    except (json.JSONDecodeError, OSError) as e:
        return {"file": str(file), "changes": {}, "conflicts": [], "error": str(e)}


# ── Main ───────────────────────────────────────────────────────────────────────

def process_all_files(
    path: str | Path,
    rename_map: dict[str, str],
    strip_prefixes: list[str],
    dry_run: bool = True,
) -> None:
    path = Path(path)
    label = "[DRY RUN] " if dry_run else ""

    print(f"\n{'='*60}")
    print(f"{label}Scanning: {path}")
    print(f"  Rename map:      {rename_map}")
    print(f"  Strip prefixes:  {strip_prefixes}")
    print(f"{'='*60}\n")

    total_conflicts = 0

    # ── Parquet ──
    parquet_files = get_parquet_columns(path)
    print(f"── Parquet files ({len(parquet_files)}) ──────────────────────────")
    for entry in parquet_files:
        result = rename_parquet_columns(entry["file"], rename_map, strip_prefixes, dry_run=dry_run)
        has_output = result["changes"] or result["conflicts"]
        if has_output:
            print(f"  {result['file']}")
        for old, new in result["changes"].items():
            print(f"    {old!r:30s} → {new!r}")
        for c in result["conflicts"]:
            print(f"    CONFLICT: {c['originals']} would all map to {c['new_name']!r}")
            total_conflicts += 1
        if not has_output:
            print(f"  {result['file']}  (no changes)")

    # ── JSON ──
    json_files = get_json_keys(path)
    print(f"\n── JSON files ({len(json_files)}) ────────────────────────────────")
    for entry in json_files:
        if "error" in entry:
            print(f"  {entry['file']}  ERROR: {entry['error']}")
            continue
        result = rename_json_file_keys(entry["file"], rename_map, strip_prefixes, dry_run=dry_run)
        has_output = result["changes"] or result["conflicts"]
        if has_output:
            print(f"  {result['file']}")
        for old, new in result["changes"].items():
            print(f"    {old!r:30s} → {new!r}")
        for c in result["conflicts"]:
            loc = f" (at {c['path']!r})" if c["path"] else ""
            print(f"    CONFLICT{loc}: {c['originals']} would all map to {c['new_name']!r}")
            total_conflicts += 1
        if not has_output:
            print(f"  {result['file']}  (no changes)")

    print(f"\n{'='*60}")
    action = "Would rename" if dry_run else "Renamed"
    print(f"{label}{action} columns/keys in {len(parquet_files)} parquet + {len(json_files)} JSON files.")
    if total_conflicts:
        print(f"  *** {total_conflicts} CONFLICT(S) DETECTED — resolve before applying. ***")
    if dry_run:
        print("  Pass dry_run=False to apply changes.")
    print(f"{'='*60}\n")


# ── Config & entry point ───────────────────────────────────────────────────────

if __name__ == "__main__":
    SEARCH_PATH = "/Users/<user>/fyp_local/"

    RENAME_MAP = {
        "collection_id":       "collection_id",
        "D_watch_duration":    "play_duration",
        "D_primary_value":     "extra_data",
        "D_feature_name":      "activity_type"
    }

    STRIP_PREFIXES = [
        "T_",
        "G_",
        "S_",
        "D_",
        "B_",
    ]

    process_all_files(
        path=SEARCH_PATH,
        rename_map=RENAME_MAP,
        strip_prefixes=STRIP_PREFIXES,
        dry_run=False,   # ← flip to False to write changes
    )
