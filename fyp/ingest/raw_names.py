"""Generated identities for raw uploads.

The name a platform gives an export ("user_data_tiktok.json", "user_data_tiktok_2.json")
carries no information: every TikTok participant's browser produces the same
handful of names, and two people's donations must never share a storage key or
a collection id. So nothing user-chosen is ever used as either. Every raw
object written into a raw location gets a name allocated here, every new
collection gets an id allocated here, and the original filename survives only
as metadata (``original_filename`` in the ingestion manifest and ledger, and
the default ``display_collection_id``).

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




def display_label(original_filename: str | None, platform: str | None = None) -> str:
    """The default ``display_collection_id`` for an upload: the original
    filename's stem, whitespace-collapsed and length-capped, or a platform
    fallback when there is nothing usable."""
    stem = os.path.splitext(os.path.basename(str(original_filename or "")))[0]
    stem = re.sub(r"\s+", " ", stem).strip()
    if not stem:
        stem = f"{str(platform or 'donation').capitalize()} donation"
    return stem[:_MAX_DISPLAY_LEN]




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
                             known_ids: set[str] | None = None) -> tuple[str, str, str]:
    """Allocate the identity of one uploaded file.

    Returns ``(stored_filename, collection_id, display_collection_id)``:
    a fresh stored name that is free in ``raw_path`` and the archive, the
    collection id (the stored name's stem) checked against every known id,
    and the display label derived from the original filename.

    Args:
        platform: e.g. ``"tiktok"``.
        source: e.g. ``"ddp"``.
        original_filename: The name the browser sent; only its extension and
            its stem (for the display label) are used.
        raw_path: The raw storage location the file will be moved into.
        known_ids: Pre-loaded ``known_collection_ids()`` when allocating several
            files in one request.

    Raises:
        RuntimeError: When no free name could be found (practically impossible).
    """
    if known_ids is None:
        known_ids = known_collection_ids()
    ext = os.path.splitext(str(original_filename or ""))[1]
    for _ in range(_ALLOC_ATTEMPTS):
        name = stored_filename(platform, source, ext)
        cid = os.path.splitext(name)[0]
        if cid in known_ids or not raw_name_is_free(raw_path, name):
            continue
        known_ids.add(cid)
        return name, cid, display_label(original_filename, platform)
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
