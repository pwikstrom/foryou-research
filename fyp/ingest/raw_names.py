"""Generated identities for raw uploads.

The name a platform gives an export ("user_data_tiktok.json", "user_data_tiktok_2.json")
carries no information: every TikTok participant's browser produces the same
handful of names, and two people's donations must never share a storage key or
a collection id. So nothing user-chosen is ever used as either. Every raw
object written into a raw location gets a name allocated here, every new
collection gets an id allocated here, and the original filename survives only
as metadata (``original_filename`` in the ingestion manifest and ledger, and
the seed of the default ``display_collection_id``).

Display ids are the operator-facing half of the same contract. They are free
text and mean nothing to the pipeline, but two collections sharing one name
make a picker, a legend or a study selection ambiguous, so a name is unique
across the Hub: allocation suffixes a colliding default ("user_data_tiktok",
"user_data_tiktok (2)") and every rename is checked against
:func:`display_id_owner` before it is written.

A stored name looks like ``tiktok_ddp_20260906T112918Z_3f9a1c7b.json``: the
platform and source so a bucket listing stays readable, an upload timestamp so
it sorts, eight random hex digits so it is unique, and the validated extension
because the parsers route on it. The collection id of a single-file upload is
that name's stem.

The storage layer enforces the other half of the contract: raw locations are
append-only (``fyp.core.data_io.APPEND_ONLY_LOCATIONS``), so even a bug here
cannot overwrite an existing donation.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone

import fyp.data_io as data_io
from fyp.logging_setup import get_logger

logger = get_logger(__name__)

# Keys of a manifest entry written by the upload routes and read by the
# ingester, the ledger, the UI, and withdraw/restore. Keyed by STORED name.
MANIFEST_PROVENANCE_KEYS: tuple[str, ...] = (
    "original_filename", "display_collection_id", "user_id", "tz",
    "client_reviewed", "uploaded_by", "uploaded_at",
)

_MAX_DISPLAY_LEN = 80
_ALLOC_ATTEMPTS = 20
_DISPLAY_SUFFIX_ATTEMPTS = 500




def _slug(value: str | None, fallback: str) -> str:
    v = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    return v or fallback




def _utc_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")




def stored_filename(platform: str | None, source: str | None, extension: str,
                    now: datetime | None = None) -> str:
    """One candidate stored name. ``extension`` includes the dot and is
    lower-cased; an empty extension is kept empty (AIO objects have none)."""
    ext = (extension or "").lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return (f"{_slug(platform, 'platform')}_{_slug(source, 'upload')}_"
            f"{_utc_stamp(now)}_{secrets.token_hex(4)}{ext}")




def normalize_display_id(value) -> str:
    """The stored form of a display label: whitespace collapsed, trimmed, and
    length-capped. An empty result means "no label" — the collection then
    shows its own id."""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:_MAX_DISPLAY_LEN]




def display_key(value) -> str:
    """The key two display labels collide on.

    Case and stray whitespace are not enough to tell two collections apart in
    a picker or a chart legend, so "Donor A" and "donor  a" are one name here.
    """
    return normalize_display_id(value).casefold()




def display_label(original_filename: str | None, platform: str | None = None) -> str:
    """The default ``display_collection_id`` for an upload: the original
    filename's stem, whitespace-collapsed and length-capped, or a platform
    fallback when there is nothing usable."""
    base = os.path.basename(str(original_filename or ""))
    stem = normalize_display_id(os.path.splitext(base)[0])
    if not stem:
        stem = f"{str(platform or 'donation').capitalize()} donation"
    return stem




def known_collection_ids(raw_paths: list[str] | None = None) -> set[str]:
    """Every collection id the Hub knows about, from every store that can hold
    one: the metadata table (the dataset), the tags sidecar (links, display
    names, pending uploads' owners), every raw location's ingestion manifest
    (uploads not yet ingested), the ingestion ledger (files ingested before),
    and the withdrawal ledger (collections inside their restore window).

    Args:
        raw_paths: The raw storage locations whose manifests to read. Defaults
            to the registered ingestion classes' raw paths.
    """
    from fyp.organize_datasets import COLLECTIONS_LABEL

    ids: set[str] = set()

    try:
        meta_fn = f"{COLLECTIONS_LABEL}_metadata.parquet"
        if data_io.exists(storage_location="recoded", filename=meta_fn):
            meta = data_io.load_parquet(storage_location="recoded", filename=meta_fn)
            if meta is not None:
                if "collection_id" in getattr(meta, "columns", []):
                    ids.update(str(c) for c in meta["collection_id"].dropna().unique())
                else:
                    ids.update(str(c) for c in meta.index.dropna().unique())
    except Exception as exc:  # never let a bookkeeping read block an upload
        logger.warning(f"[raw_names] could not read collection metadata: {exc}")

    for fn in (f"{COLLECTIONS_LABEL}_tags.json", "withdrawals.json"):
        try:
            if data_io.exists(storage_location="recoded", filename=fn):
                doc = data_io.load_json(storage_location="recoded", filename=fn, verbose=False) or {}
                ids.update(str(k) for k in doc.keys())
        except Exception as exc:
            logger.warning(f"[raw_names] could not read {fn}: {exc}")

    try:
        if data_io.exists(storage_location="recoded", filename="ingestion_ledger.json"):
            ledger = data_io.load_json(storage_location="recoded",
                                       filename="ingestion_ledger.json", verbose=False) or {}
            for entry in (ledger.get("files") or {}).values():
                cid = (entry or {}).get("collection_id")
                if cid:
                    ids.add(str(cid))
    except Exception as exc:
        logger.warning(f"[raw_names] could not read the ingestion ledger: {exc}")

    if raw_paths is None:
        raw_paths = registered_raw_paths()
    for raw_path in raw_paths:
        try:
            if data_io.exists(storage_location=raw_path, filename="ingestion_manifest.json"):
                manifest = data_io.load_json(storage_location=raw_path,
                                             filename="ingestion_manifest.json", verbose=False) or {}
                for entry in manifest.values():
                    cid = (entry or {}).get("collection_id")
                    if cid:
                        ids.add(str(cid))
        except Exception as exc:
            logger.warning(f"[raw_names] could not read the manifest in {raw_path}: {exc}")
    return ids




def registered_raw_paths() -> list[str]:
    """Raw storage locations of the registered ingestion classes."""
    try:
        from fyp.ingest import get_main_collection
        return [c.raw_path for c in get_main_collection(verbose=False).collections
                if getattr(c, "raw_path", None)]
    except Exception as exc:
        logger.warning(f"[raw_names] could not list registered raw paths: {exc}")
        return []




def known_display_keys(known_ids: set[str] | None = None,
                       raw_paths: list[str] | None = None) -> set[str]:
    """Every display key already spoken for.

    Three things answer to a name: a collection's explicit
    ``display_collection_id`` in the tags sidecar, the same field on an upload
    still waiting in a raw location's manifest, and — for a collection nobody
    ever labelled — its own collection id, which is what every listing falls
    back to. All three go in, so a generated label never lands on a name the
    operator already sees somewhere.

    Args:
        known_ids: A pre-loaded :func:`known_collection_ids` result.
        raw_paths: The raw locations whose manifests to read.
    """
    from fyp.organize_datasets import COLLECTIONS_LABEL

    ids = known_ids if known_ids is not None else known_collection_ids(raw_paths)
    keys: set[str] = {display_key(cid) for cid in ids}

    try:
        fn = f"{COLLECTIONS_LABEL}_tags.json"
        if data_io.exists(storage_location="recoded", filename=fn):
            doc = data_io.load_json(storage_location="recoded", filename=fn, verbose=False) or {}
            for entry in doc.values():
                label = entry.get("display_collection_id") if isinstance(entry, dict) else None
                if label:
                    keys.add(display_key(label))
    except Exception as exc:  # never let a bookkeeping read block an upload
        logger.warning(f"[raw_names] could not read display ids from the tags sidecar: {exc}")

    if raw_paths is None:
        raw_paths = registered_raw_paths()
    for raw_path in raw_paths:
        try:
            if data_io.exists(storage_location=raw_path, filename="ingestion_manifest.json"):
                manifest = data_io.load_json(storage_location=raw_path,
                                             filename="ingestion_manifest.json", verbose=False) or {}
                for entry in manifest.values():
                    label = (entry or {}).get("display_collection_id")
                    if label:
                        keys.add(display_key(label))
        except Exception as exc:
            logger.warning(f"[raw_names] could not read display ids in {raw_path}: {exc}")

    keys.discard("")
    return keys




def unique_display_label(base: str, taken: set[str]) -> str:
    """``base`` if that name is free, else ``base (2)``, ``base (3)``, …

    The result is reserved in ``taken``, so three copies of
    user_data_tiktok.json uploaded in one request come out as three distinct
    names instead of one name three times.
    """
    label = normalize_display_id(base)
    key = display_key(label)
    if not key:
        return label
    if key not in taken:
        taken.add(key)
        return label
    for n in range(2, _DISPLAY_SUFFIX_ATTEMPTS + 2):
        candidate = _suffixed(label, f" ({n})")
        if display_key(candidate) not in taken:
            taken.add(display_key(candidate))
            return candidate
    # Practically unreachable: 500 same-named collections already exist.
    candidate = _suffixed(label, f" ({secrets.token_hex(3)})")
    taken.add(display_key(candidate))
    return candidate




def _suffixed(label: str, suffix: str) -> str:
    """``label`` with ``suffix`` appended, trimming the label — not the
    suffix — to stay inside the length cap."""
    return normalize_display_id(label[:_MAX_DISPLAY_LEN - len(suffix)]) + suffix




def entry_display_id(collection_id, entry) -> str:
    """The name a collection answers to: its label, or its own id when it has
    none (what every listing falls back to)."""
    label = entry.get("display_collection_id") if isinstance(entry, dict) else None
    return normalize_display_id(label) or str(collection_id)




def display_id_owner(display_id, tags: dict, *, exclude=None) -> str | None:
    """The collection already answering to ``display_id``, or None if free.

    A collection answers to its label AND to its own collection id — the id is
    what listings fall back to, and it is what the modal header, the tooltips
    and the process logs show whatever the label says. So naming one
    collection after another's id is a conflict too, the same way
    :func:`known_display_keys` treats it at upload time.

    ``exclude`` is the collection being renamed — a name never conflicts with
    itself, so re-saving an existing record is not a rename.
    """
    key = display_key(display_id)
    if not key:
        return None
    for cid, entry in (tags or {}).items():
        if exclude is not None and str(cid) == str(exclude):
            continue
        if key in (display_key(cid), display_key(entry_display_id(cid, entry))):
            return str(cid)
    return None




def duplicate_display_ids(tags: dict) -> dict[str, list[str]]:
    """``{label: [collection ids]}`` for every name more than one collection
    answers to. Empty while the invariant holds — writes have enforced it
    since 2026-09-09, so anything here predates the guard.
    """
    by_key: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for cid, entry in (tags or {}).items():
        label = entry_display_id(cid, entry)
        key = display_key(label)
        if not key:
            continue
        by_key.setdefault(key, []).append(str(cid))
        labels.setdefault(key, label)
    return {labels[k]: sorted(v) for k, v in sorted(by_key.items()) if len(v) > 1}




def raw_name_is_free(raw_path: str, filename: str) -> bool:
    """A stored name is free when nothing sits at it in the raw location or
    the archive (a withdrawn donation keeps its name there)."""
    if data_io.exists(storage_location=raw_path, filename=filename):
        return False
    try:
        if data_io.exists(storage_location="archive", filename=filename):
            return False
    except ValueError:
        pass  # no archive location registered (minimal local installs)
    return True




def allocate_upload_identity(platform: str | None, source: str | None,
                             original_filename: str, raw_path: str,
                             known_ids: set[str] | None = None,
                             known_displays: set[str] | None = None) -> tuple[str, str, str]:
    """Allocate the identity of one uploaded file.

    Returns ``(stored_filename, collection_id, display_collection_id)``:
    a fresh stored name that is free in ``raw_path`` and the archive, the
    collection id (the stored name's stem) checked against every known id,
    and a display label derived from the original filename and made unique
    against every name already in use — the filename is the same for every
    donor on a platform, so the raw stem would collide on the very next
    upload.

    Args:
        platform: e.g. ``"tiktok"``.
        source: e.g. ``"ddp"``.
        original_filename: The name the browser sent; only its extension and
            its stem (for the display label) are used.
        raw_path: The raw storage location the file will be moved into.
        known_ids: Pre-loaded ``known_collection_ids()`` when allocating several
            files in one request.
        known_displays: Pre-loaded ``known_display_keys()``, likewise. Both
            sets are extended in place, so every file in a batch is allocated
            against what the files before it took.

    Raises:
        RuntimeError: When no free name could be found (practically impossible).
    """
    if known_ids is None:
        known_ids = known_collection_ids()
    if known_displays is None:
        known_displays = known_display_keys(known_ids)
    ext = os.path.splitext(str(original_filename or ""))[1]
    for _ in range(_ALLOC_ATTEMPTS):
        name = stored_filename(platform, source, ext)
        cid = os.path.splitext(name)[0]
        if cid in known_ids or not raw_name_is_free(raw_path, name):
            continue
        known_ids.add(cid)
        return name, cid, unique_display_label(
            display_label(original_filename, platform), known_displays)
    raise RuntimeError("could not allocate a free name for the upload")




def manifest_entry(collection_id: str, original_filename: str, *,
                   display_collection_id: str | None = None,
                   user_id: str | None = None, tags: list[str] | None = None,
                   tz: str | None = None, client_reviewed: bool = False,
                   uploaded_by: str | None = None,
                   uploaded_at: str | None = None) -> dict:
    """The ingestion-manifest entry for one stored file (keyed by the caller
    under the STORED name). Only truthy optional fields are written so older
    readers keep seeing the shape they expect."""
    entry: dict = {
        "collection_id": str(collection_id),
        "original_filename": str(original_filename),
        "display_collection_id": display_collection_id,
        "tags": list(tags or []),
        "uploaded_at": uploaded_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if user_id:
        entry["user_id"] = user_id
    if uploaded_by:
        entry["uploaded_by"] = uploaded_by
    if tz:
        entry["tz"] = tz
    if client_reviewed:
        entry["client_reviewed"] = True
    return entry




def provenance_from_manifest(entry: dict | None) -> dict:
    """The provenance fields of a manifest entry, for copying into the ledger
    before the entry is pruned."""
    entry = entry or {}
    return {k: entry.get(k) for k in MANIFEST_PROVENANCE_KEYS if entry.get(k) is not None}
