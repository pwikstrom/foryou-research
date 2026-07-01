#!/usr/bin/env python3
"""Activity-contract versioning: identity, registry, and per-row provenance.

The ingestion analogue of :mod:`fyp.scrape_versioning`. Every ingested activity
row is stamped with a deterministic ``activity_contract_version`` derived from the
activity-contract field set. A registry (``activity_versions.json``) snapshots the
full field digest behind each version so the precise schema that validated any
activity is preserved even if the contract is later edited.

Like the scrape version there is no all-versions archive — the collections
parquet accretes over time and the per-row ``activity_contract_version`` already
gives a queryable history. The active version only changes on an explicit
:func:`promote_version` call.
"""

import copy as _copy
import datetime as _dt
import hashlib
import json

import pandas as pd

import fyp.data_io as data_io
from fyp import activity_contract as ac

REGISTRY_FILENAME = "activity_versions.json"
REGISTRY_LOCATION = "recoded"
LEGACY_VERSION = "acv0_legacy"

# The canonical per-row provenance column stamped onto every ingested row.
# Declared as a derived field in config/activity_contract.toml so it is
# var_schema-known and carried through the pipeline.
PROVENANCE_COLUMN = "activity_contract_version"

_DESCRIPTOR_CACHE: dict = {}




def _sha256_hex(text: str, length: int = 64) -> str:
    """Return the hex SHA-256 of ``text`` truncated to ``length`` chars."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]




def build_activity_version_descriptor(contract: dict, label: str | None = None) -> dict:
    """Build a self-describing version descriptor and its deterministic id.

    The identity is the same ``contract_field_digest`` that feeds
    :func:`fyp.recode_variables.compute_var_schema_hash`, plus the platform set,
    so the activity version and the study-cache key move together on a contract
    change (no skew).

    Args:
        contract: the parsed activity contract dict.
        label: optional human-readable label; defaults to a short fingerprint.

    Returns:
        A descriptor dict including the computed ``activity_contract_version`` and
        a full ``field_digest`` snapshot.
    """
    field_digest = ac.contract_field_digest(contract)
    identity = {
        "field_digest": field_digest,
        "platforms": sorted(ac.platforms(contract)),
    }
    canonical = json.dumps(identity, sort_keys=True, ensure_ascii=False)
    version = "acv_" + _sha256_hex(canonical, 12)

    return {
        "activity_contract_version": version,
        "label": label or f"activity:{version[4:10]}",
        "meta_version": str(contract.get("meta", {}).get("version", "")),
        "platforms": identity["platforms"],
        "field_digest": field_digest,
    }




def current_version_descriptor(fresh: bool = False) -> dict:
    """Return the version descriptor for the current activity contract.

    Cached for the process lifetime — the contract is a file on disk that does
    not change within a run.

    Args:
        fresh: When True, reload the contract and recompute.

    Returns:
        The version descriptor for the active activity contract.
    """
    if not fresh and _DESCRIPTOR_CACHE.get("descriptor") is not None:
        return _DESCRIPTOR_CACHE["descriptor"]
    contract = ac.load_contract()
    descriptor = build_activity_version_descriptor(contract)
    _DESCRIPTOR_CACHE["descriptor"] = descriptor
    return descriptor




def current_activity_version(fresh: bool = False) -> str:
    """Return just the current ``activity_contract_version`` id, never raising."""
    try:
        return current_version_descriptor(fresh=fresh)["activity_contract_version"]
    except Exception:
        return "unknown"




def empty_registry() -> dict:
    """Return a fresh, empty version registry."""
    return {"versions": {}, "active": None}




def _register_into(registry: dict, descriptor: dict, created_at: str | None = None) -> dict:
    """Return a copy of ``registry`` with ``descriptor`` recorded if new.

    Recording a version never changes the ``active`` pointer (stay-pinned-until-promote).
    """
    registry = _copy.deepcopy(registry)
    versions = registry.setdefault("versions", {})
    version = descriptor["activity_contract_version"]
    if version not in versions:
        versions[version] = {**descriptor, "created_at": created_at}
    return registry




def _promote_into(registry: dict, version: str) -> dict:
    """Return a copy of ``registry`` with ``active`` set to ``version``."""
    registry = _copy.deepcopy(registry)
    if version not in registry.get("versions", {}):
        raise KeyError(f"unknown activity_contract_version: {version}")
    registry["active"] = version
    return registry




def load_registry() -> dict:
    """Load the version registry from storage, or an empty one if absent."""
    if data_io.exists(storage_location=REGISTRY_LOCATION, filename=REGISTRY_FILENAME):
        registry = data_io.load_json(
            storage_location=REGISTRY_LOCATION, filename=REGISTRY_FILENAME
        )
        if isinstance(registry, dict) and "versions" in registry:
            return registry
    return empty_registry()




def save_registry(registry: dict) -> None:
    """Persist the version registry to storage."""
    data_io.save_json(
        data=registry, storage_location=REGISTRY_LOCATION, filename=REGISTRY_FILENAME
    )




def register_version(descriptor: dict | None = None, created_at: str | None = None) -> dict:
    """Record a version in the registry if it is not already present."""
    if descriptor is None:
        descriptor = current_version_descriptor()
    if created_at is None:
        created_at = _dt.datetime.now().isoformat(timespec="seconds")

    registry = load_registry()
    updated = _register_into(registry, descriptor, created_at)
    if updated != registry:
        save_registry(updated)
    return updated




def get_active_version() -> str | None:
    """Return the currently active (promoted) activity version, if any."""
    return load_registry().get("active")




def promote_version(version: str) -> dict:
    """Promote ``version`` to be the active version. Returns the registry."""
    registry = _promote_into(load_registry(), version)
    save_registry(registry)
    return registry




def list_versions() -> list[dict]:
    """Return version summaries (without the bulky field-digest snapshot)."""
    registry = load_registry()
    active = registry.get("active")
    summaries = []
    for version, info in registry.get("versions", {}).items():
        summary = {k: v for k, v in info.items() if k != "field_digest"}
        summary["active"] = version == active
        summaries.append(summary)
    return summaries




def stamp_version(df: pd.DataFrame) -> pd.DataFrame:
    """Stamp the per-row ``activity_contract_version`` provenance column in place."""
    df[PROVENANCE_COLUMN] = pd.Series(
        current_activity_version(), index=df.index, dtype="string[pyarrow]"
    )
    return df




def ensure_current_version_registered() -> str:
    """Register the current contract's version if new; return its id.

    Safe to call repeatedly (idempotent) and never raises — intended to be
    invoked once per ingest run.
    """
    try:
        descriptor = current_version_descriptor()
        register_version(descriptor=descriptor)
        return descriptor["activity_contract_version"]
    except Exception:
        return "unknown"
