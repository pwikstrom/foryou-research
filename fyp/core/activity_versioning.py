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

from fyp import activity_contract as ac
from fyp.logging_setup import get_logger

logger = get_logger(__name__)

# NOTE: fyp.data_io is imported LAZILY inside functions — a module-level import
# creates an import cycle via fyp_config (importing this module first leaves it
# partially initialized while fyp_config's load_var_schema overlay calls into
# it, silently losing legacy metadata). See fyp.annotation_versioning.


def _data_io():
    """Lazy fyp.data_io accessor (breaks the fyp_config import cycle)."""
    import fyp.data_io as data_io

    return data_io

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




def active_version_descriptor(fresh: bool = False) -> dict:
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




def active_activity_version(fresh: bool = False) -> str:
    """Return just the current ``activity_contract_version`` id, never raising."""
    try:
        return active_version_descriptor(fresh=fresh)["activity_contract_version"]
    except Exception:
        return "unknown"




def empty_registry() -> dict:
    """Return a fresh, empty version registry."""
    return {"versions": {}, "preferred": None}




def _register_into(
    registry: dict,
    descriptor: dict,
    created_at: str | None = None,
    field_metadata: dict | None = None,
) -> dict:
    """Return a copy of ``registry`` with ``descriptor`` recorded if new.

    Recording a version never changes the ``active`` pointer (stay-pinned-until-promote).
    ``field_metadata`` snapshots the contract's var_schema column metadata at
    registration so a field a future contract stops emitting stays
    contract-owned via :func:`union_field_metadata`.
    """
    registry = _copy.deepcopy(registry)
    versions = registry.setdefault("versions", {})
    version = descriptor["activity_contract_version"]
    if version not in versions:
        versions[version] = {
            **descriptor,
            "field_metadata": field_metadata or {},
            "created_at": created_at,
        }
    return registry




def _promote_into(registry: dict, version: str) -> dict:
    """Return a copy of ``registry`` with ``preferred`` set to ``version``."""
    registry = _copy.deepcopy(registry)
    if version not in registry.get("versions", {}):
        raise KeyError(f"unknown activity_contract_version: {version}")
    registry.pop("active", None)  # pre-2026-07 key name
    registry["preferred"] = version
    return registry




def load_registry() -> dict:
    """Load the version registry from storage, or an empty one if absent."""
    # No exists() pre-flight: load_json_optional returns None for an absent
    # registry in one round-trip. It RAISES on a real read failure, so the
    # "registry unreadable" warning in union_field_metadata still fires instead
    # of an outage masquerading as an empty registry.
    registry = _data_io().load_json_optional(
        storage_location=REGISTRY_LOCATION, filename=REGISTRY_FILENAME
    )
    if isinstance(registry, dict) and "versions" in registry:
        # The promoted pointer was called "active" before the 2026-07
        # terminology change (active now means "used for new rows").
        if "preferred" not in registry and "active" in registry:
            registry["preferred"] = registry.pop("active")
        return registry
    return empty_registry()




def save_registry(registry: dict) -> None:
    """Persist the version registry to storage."""
    _data_io().save_json(
        data=registry, storage_location=REGISTRY_LOCATION, filename=REGISTRY_FILENAME
    )




def register_version(descriptor: dict | None = None, created_at: str | None = None) -> dict:
    """Record a version in the registry if it is not already present."""
    if descriptor is None:
        descriptor = active_version_descriptor()
    if created_at is None:
        created_at = _dt.datetime.now().isoformat(timespec="seconds")

    from fyp import registry_metadata as rm

    registry = load_registry()
    updated = _register_into(
        registry, descriptor, created_at, field_metadata=rm.snapshot_field_metadata(ac)
    )
    if updated != registry:
        save_registry(updated)
    return updated




def get_preferred_version() -> str | None:
    """Return the currently active (promoted) activity version, if any."""
    return load_registry().get("preferred")




def promote_version(version: str) -> dict:
    """Promote ``version`` to be the active version. Returns the registry."""
    registry = _promote_into(load_registry(), version)
    save_registry(registry)
    return registry




def list_versions() -> list[dict]:
    """Return version summaries (without the bulky per-field snapshots)."""
    registry = load_registry()
    preferred = registry.get("preferred")
    summaries = []
    for version, info in registry.get("versions", {}).items():
        summary = {k: v for k, v in info.items() if k not in ("field_digest", "field_metadata")}
        summary["preferred"] = version == preferred
        summaries.append(summary)
    return summaries




def union_field_metadata(versions_to_include: set | None = None) -> dict:
    """Merge ``field_metadata`` across registered activity versions, never raising.

    The var_schema overlay unions this with the current contract's metadata so
    an activity field retired from a future contract stays contract-owned and
    read-only (badged "legacy") instead of degrading into an editable orphan.
    """
    try:
        from fyp import registry_metadata as rm

        return rm.union_field_metadata(load_registry(), versions_to_include)
    except Exception as e:
        logger.warning(f"WARNING: activity version registry unreadable ({e}); legacy union empty.")
        return {}




def stamp_version(df: pd.DataFrame) -> pd.DataFrame:
    """Stamp the per-row ``activity_contract_version`` provenance column in place."""
    df[PROVENANCE_COLUMN] = pd.Series(
        active_activity_version(), index=df.index, dtype="string[pyarrow]"
    )
    return df




def ensure_active_version_registered() -> str:
    """Register the current contract's version if new; return its id.

    Safe to call repeatedly (idempotent) and never raises — intended to be
    invoked once per ingest run.
    """
    try:
        descriptor = active_version_descriptor()
        register_version(descriptor=descriptor)
        return descriptor["activity_contract_version"]
    except Exception:
        return "unknown"
