#!/usr/bin/env python3
"""Platform-aware media object paths.

Multi-platform media layout: new downloads are written under a per-platform
subdirectory — ``{media_prefix}/{platform}/{item_id}.{ext}`` — while media
downloaded before the layout change stays at the legacy flat
``{media_prefix}/{item_id}.mp4`` path (no bulk migration). This module is the
single owner of that layout and of the reader-side resolution order:

1. the row's ``storage_link`` (stamped at scrape time — always preferred),
2. the platform subpath,
3. the legacy flat path,
4. other registered platforms' subpaths (when the platform is unknown).

Resolutions are cached (bounded) so repeated probes for the same item — e.g.
the viewer's HTTP Range requests — do not re-hit GCS.
"""

import os
import threading

_RESOLVE_CACHE: dict[tuple, dict | None] = {}
_RESOLVE_CACHE_LOCK = threading.Lock()
_RESOLVE_CACHE_MAX = 512


def _cf():
    """Lazy fyp_config accessor (avoids the fyp_config import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf






def _registered_platforms() -> list[str]:
    """Lazy list of platforms registered in the scrape contract."""
    import fyp.scrape_queues as scrape_queues

    return scrape_queues.registered_platforms()






def media_relpath(platform: str, item_id: str, ext: str = "mp4") -> str:
    """Return the platform-subdirectory relative path for one media object."""
    return f"{platform}/{item_id}.{ext}"






def candidate_relpaths(item_id: str, platform: str | None = None) -> list[str]:
    """Return relative media paths to probe, in resolution order.

    Args:
        item_id: The item whose media is being located.
        platform: The item's platform when known; ``None`` probes broadly.

    Returns:
        Relative paths: the platform subpath first (when known), then the
        legacy flat path, then the other registered platforms' subpaths.
    """
    candidates: list[str] = []
    if platform:
        candidates.append(media_relpath(platform, item_id))
    candidates.append(f"{item_id}.mp4")
    for other in _registered_platforms():
        rel = media_relpath(other, item_id)
        if rel not in candidates:
            candidates.append(rel)
    return candidates






def ensure_local_platform_dir(platform: str) -> str:
    """Create (if needed) and return the local media dir for one platform."""
    platform_dir = os.path.join(_cf()['paths']['media'], platform)
    os.makedirs(platform_dir, exist_ok=True)
    return platform_dir






def _parse_storage_link(storage_link: str) -> dict | None:
    """Split a ``storage_link`` value into a gcs/local descriptor (unverified)."""
    if not storage_link:
        return None
    if storage_link.startswith("gs://"):
        rest = storage_link[len("gs://"):]
        bucket_name, _, blob_name = rest.partition("/")
        if not bucket_name or not blob_name:
            return None
        return {"kind": "gcs", "bucket_name": bucket_name, "blob_name": blob_name}
    return {"kind": "local", "path": storage_link}






def _gcs_stat(blob_name: str) -> int | None:
    """Probe one blob in the configured media bucket; return its size or ``None``.

    A single metadata GET both verifies existence and fetches the size, so
    callers (the viewer's Range handler) need no follow-up ``exists()`` /
    ``reload()`` round-trips.
    """
    bucket = _cf()['data_io'].get('bucket')
    if bucket is None:
        return None
    try:
        blob = bucket.get_blob(blob_name)
        return None if blob is None else blob.size
    except Exception:
        return None






def resolve_media(
    item_id: str,
    platform: str | None = None,
    storage_link: str | None = None,
    check_exists: bool = True,
) -> dict | None:
    """Locate one item's media object across the platform/legacy layouts.

    Args:
        item_id: The item whose media is being located.
        platform: The item's platform when known (narrows the probe order).
        storage_link: The row's stored link, preferred over probing when valid.
        check_exists: Verify existence before returning (set ``False`` only
            when the caller handles missing files itself).

    Returns:
        ``{"kind": "gcs", "bucket_name": ..., "blob_name": ...}`` or
        ``{"kind": "local", "path": ...}``, or ``None`` when nothing resolves.
        When ``check_exists`` verified the object, a ``"size"`` key carries its
        byte size so callers can serve Range requests without re-stat'ing.
    """
    fyp_cf = _cf()
    use_gcs = fyp_cf['data_io']['use_gcs_for_media']

    cache_key = (item_id, platform, storage_link or "", use_gcs)
    with _RESOLVE_CACHE_LOCK:
        if check_exists and cache_key in _RESOLVE_CACHE:
            return _RESOLVE_CACHE[cache_key]

    resolved: dict | None = None

    linked = _parse_storage_link(storage_link or "")
    if linked is not None:
        if not check_exists:
            resolved = linked
        elif linked["kind"] == "gcs" and use_gcs:
            size = _gcs_stat(linked["blob_name"])
            if size is not None:
                resolved = {**linked, "size": size}
        elif linked["kind"] == "local" and os.path.exists(linked["path"]):
            resolved = {**linked, "size": os.path.getsize(linked["path"])}

    if resolved is None:
        if use_gcs:
            prefix = fyp_cf['data_io']['gcs_media_prefix']
            bucket = fyp_cf['data_io'].get('bucket')
            bucket_name = getattr(bucket, "name", "") if bucket is not None else ""
            for rel in candidate_relpaths(item_id, platform):
                blob_name = f"{prefix}/{rel}"
                if not check_exists:
                    resolved = {"kind": "gcs", "bucket_name": bucket_name, "blob_name": blob_name}
                    break
                size = _gcs_stat(blob_name)
                if size is not None:
                    resolved = {"kind": "gcs", "bucket_name": bucket_name, "blob_name": blob_name, "size": size}
                    break
        else:
            media_dir = fyp_cf['paths']['media']
            for rel in candidate_relpaths(item_id, platform):
                path = os.path.join(media_dir, rel)
                if not check_exists:
                    resolved = {"kind": "local", "path": path}
                    break
                if os.path.exists(path):
                    resolved = {"kind": "local", "path": path, "size": os.path.getsize(path)}
                    break

    if check_exists:
        with _RESOLVE_CACHE_LOCK:
            if len(_RESOLVE_CACHE) >= _RESOLVE_CACHE_MAX:
                _RESOLVE_CACHE.clear()
            _RESOLVE_CACHE[cache_key] = resolved
    return resolved






def media_gs_uri(resolved: dict) -> str:
    """Return the ``gs://`` URI for a GCS resolution (e.g. for Gemini)."""
    return f"gs://{resolved['bucket_name']}/{resolved['blob_name']}"
