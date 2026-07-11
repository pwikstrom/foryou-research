#!/usr/bin/env python3
"""Shared helpers for contract version registries' field-metadata snapshots.

All three version registries (annotation ``av_``, scrape ``sv_``, activity
``acv_``) snapshot the var_schema column metadata their contract defined at
registration time. The union of those snapshots keeps fields that a FUTURE
contract stops emitting contract-owned and read-only (badged "legacy" in the
admin editor) instead of degrading into editable orphans — the general
mechanism for "contracts change over time but data spans versions".

This module holds the registry-agnostic pieces: taking the snapshot from a
contract module and merging snapshots across a registry's versions.
"""


def snapshot_field_metadata(contract_module) -> dict:
    """Snapshot a contract's var_schema column metadata, never raising.

    Args:
        contract_module: a contract module exposing ``load_contract()`` and
            ``contract_column_metadata(contract)`` (annotation_contract,
            scrape_contract or activity_contract).

    Returns:
        ``{column: {role, scale, display_name, description, section}}`` for the
        contract's output columns, or ``{}`` when the contract cannot be loaded.
    """
    try:
        return contract_module.contract_column_metadata(contract_module.load_contract())
    except Exception:
        return {}






def union_field_metadata(registry: dict, versions_to_include: set | None = None) -> dict:
    """Merge ``field_metadata`` across a registry's versions.

    Newer versions (by ``created_at``) win on a column present in several
    snapshots. Entries without ``field_metadata`` (pre-snapshot registrations)
    contribute nothing.

    Args:
        registry: a version registry dict (``{"versions": {id: entry}}``).
        versions_to_include: when given, only these version ids participate —
            used to prune the union to versions actually present in the data.
            The caller is responsible for including the current version.

    Returns:
        ``{column: metadata}`` merged across the participating versions.
    """
    versions = registry.get("versions", {}) if isinstance(registry, dict) else {}
    if versions_to_include is not None:
        versions = {k: v for k, v in versions.items() if k in versions_to_include}
    entries = [e for e in versions.values() if isinstance(e, dict)]
    ordered = sorted(entries, key=lambda e: e.get("created_at") or "")
    merged: dict = {}
    for entry in ordered:
        fm = entry.get("field_metadata")
        if not isinstance(fm, dict):
            continue
        for col, meta in fm.items():
            if isinstance(meta, dict):
                merged[col] = meta
    return merged
