#!/usr/bin/env python3
"""Annotation versioning: identity, registry, and promotion.

Every annotation is stamped with a deterministic ``annotation_version`` derived
from the inputs that change model output — the model id, the exact prompt text,
the response-schema shape, and the key generation parameters. A registry
(``annotation_versions.json``) snapshots the prompt text and schema behind each
version so the precise inputs that produced any annotation are preserved even if
the prompt file is later edited in place.

The active version (the one studies read by default) only changes on an explicit
:func:`promote_version` call — newly seen versions are recorded but never
auto-activated, so in-flight analyses do not shift underfoot.
"""

import copy as _copy
import datetime as _dt
import hashlib
import json
import os

import pandas as pd

from fyp.logging_setup import get_logger

logger = get_logger(__name__)

# NOTE: fyp.data_io / fyp.fyp_config are imported LAZILY inside functions.
# A module-level import here creates an import cycle: importing this module
# first (as the web app's import graph does) triggers fyp_config's module-level
# load_var_schema while THIS module is still partially initialized, so the
# legacy-metadata overlay's `av.union_field_metadata()` raised AttributeError
# and was silently swallowed — boot frames lost all legacy field metadata (and
# the schema hash drifted per-instance). Keep these imports function-level.


def _data_io():
    """Lazy fyp.data_io accessor (breaks the fyp_config import cycle)."""
    import fyp.data_io as data_io

    return data_io




def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf

REGISTRY_FILENAME = "annotation_versions.json"
REGISTRY_LOCATION = "recoded"
LEGACY_VERSION = "v0_legacy"

# The file-based prompt that predates the declarative-contract / generated-prompt
# system. Go-forward annotations use the generated contract prompt, so this file
# is only the historical ("legacy") prompt — it is what the ``v0_legacy``
# annotation version was produced with, and is shown for that version in the
# admin viewer. Kept under ``config/``.
LEGACY_PROMPT_FILENAME = "legacy_annotation_prompt.txt"

# Generation parameters that materially change model output and therefore
# belong in the version identity. ``use_structured_output`` is pinned True
# (structured output is the only annotation path) but stays in the identity
# so pre-existing ``av_`` hashes remain stable.
_VERSION_GEN_PARAM_KEYS = (
    "use_structured_output",
    "temperature",
    "thinking_budget",
    "media_resolution",
    "max_output_tokens",
)

_DESCRIPTOR_CACHE: dict = {}




def _sha256_hex(text: str, length: int = 64) -> str:
    """Return the hex SHA-256 of ``text`` truncated to ``length`` chars."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]




def compute_schema_hash(schema_json: dict | None) -> str:
    """Hash the response-schema dict so schema changes change the version.

    Args:
        schema_json: The portable JSON-schema dict for the structured response,
            or ``None`` for the free-text path.

    Returns:
        A short hash, or ``"none"`` when there is no response schema.
    """
    if not schema_json:
        return "none"
    canonical = json.dumps(schema_json, sort_keys=True, ensure_ascii=False)
    return _sha256_hex(canonical, 16)




def build_version_descriptor(
    model: str,
    prompt_text: str,
    schema_json: dict | None,
    gen_params: dict,
    label: str | None = None,
    extra_params: dict | None = None,
    backend: str | None = None,
) -> dict:
    """Build a self-describing version descriptor and its deterministic id.

    Args:
        model: The model id (e.g. ``"gemini-3-flash-preview"``).
        prompt_text: The exact prompt / system-instruction text.
        schema_json: The response-schema dict, or ``None`` for free text.
        gen_params: The output-affecting generation parameters.
        label: Optional human-readable label; defaults to the model plus a short
            prompt fingerprint.
        extra_params: Backend-specific output-affecting parameters merged into
            the identity ONLY when non-empty — Gemini passes nothing here, so
            every pre-existing ``av_`` hash is unchanged.
        backend: Backend id recorded on the descriptor as non-identity
            metadata (display/provenance only; the model id + params already
            uniquely determine the identity).

    Returns:
        A descriptor dict including the computed ``annotation_version``.
    """
    prompt_hash = _sha256_hex(prompt_text or "", 16)
    schema_hash = compute_schema_hash(schema_json)
    normalized_params = {key: gen_params.get(key) for key in _VERSION_GEN_PARAM_KEYS}
    # Constant since the free-text path was removed; kept in the identity so
    # existing av_ hashes do not shift.
    normalized_params["use_structured_output"] = True
    if extra_params:
        normalized_params.update(extra_params)

    identity = {
        "model": model,
        "prompt_hash": prompt_hash,
        "schema_hash": schema_hash,
        "gen_params": normalized_params,
    }
    canonical = json.dumps(identity, sort_keys=True, ensure_ascii=False)
    version = "av_" + _sha256_hex(canonical, 12)

    descriptor = {
        "annotation_version": version,
        "label": label or f"{model}:{prompt_hash[:6]}",
        "model": model,
        "prompt_hash": prompt_hash,
        "schema_hash": schema_hash,
        "gen_params": normalized_params,
    }
    if backend and backend != "gemini":
        descriptor["backend"] = backend
    return descriptor




def active_prompt_text() -> str:
    """Return the active system-instruction prompt text.

    The prompt is generated from the declarative contract
    (``annotation_schema.build_prompt``). The synchronous, batch and versioning
    paths all route through this so the prompt can never diverge across them.

    Returns:
        The prompt text the model is (or would be) sent.
    """
    from fyp.annotation_schema import build_prompt

    return build_prompt()


def active_prompt_label() -> str:
    """Return a stable ``prompt_fn`` label for the active prompt source."""
    return "annotation_contract.toml"


def _read_prompt_text() -> str:
    """Read the active prompt text (file or generated, per config flag)."""
    return active_prompt_text()


def legacy_prompt_text() -> str:
    """Return the pre-versioning file-based prompt text (the legacy prompt).

    This is the prompt used before the generated-contract system existed, i.e.
    what the ``v0_legacy`` annotation version was produced with. It is read from
    ``config/{LEGACY_PROMPT_FILENAME}`` and is never used for go-forward
    annotations. The admin annotation-versions viewer shows it for the legacy
    version, which otherwise has no stored prompt snapshot.

    Returns:
        The legacy prompt text, or ``""`` if the file cannot be read.
    """
    path = os.path.join(_cf()["paths"]["project_root"], "config", LEGACY_PROMPT_FILENAME)
    try:
        with open(path) as handle:
            return handle.read()
    except OSError as e:
        logger.warning(f"WARNING: legacy prompt file unreadable ({e}).")
        return ""




def current_version_descriptor(fresh: bool = False) -> dict:
    """Return the version descriptor for the current configuration.

    The descriptor (and the prompt text / schema snapshot used to build it) is
    cached keyed by a cheap config signature, so the prompt file is not re-read
    on every annotation call but a config change is still picked up.

    Args:
        fresh: When True, bypass the cache and recompute.

    Returns:
        The version descriptor for the active configuration.
    """
    machine = _cf()["machine"]
    # The contract etag is part of the signature so a runtime contract edit (which
    # leaves every [machine] config key unchanged) still busts the descriptor
    # cache — otherwise a long-lived process would keep stamping the old av_.
    try:
        from fyp import annotation_contract as _ac

        contract_etag = _ac.contract_etag()
    except Exception:
        contract_etag = None
    # A non-Gemini backend changes the effective model / prompt / params — its
    # identity feeds the signature and the descriptor below. Gemini keeps the
    # exact historical path (byte-identical av_ ids).
    try:
        from fyp.annotation.backends import active_backend_name, get_backend

        backend_name = active_backend_name()
        backend = get_backend(backend_name) if backend_name != "gemini" else None
    except Exception:
        backend_name, backend = "gemini", None
    signature = (
        machine.get("model"),
        machine.get("temperature"),
        machine.get("thinking_budget"),
        machine.get("media_resolution"),
        machine.get("max_output_tokens"),
        contract_etag,
        backend_name,
        tuple(sorted(backend.version_extra_params().items())) if backend else None,
    )
    if not fresh and _DESCRIPTOR_CACHE.get("signature") == signature:
        return _DESCRIPTOR_CACHE["descriptor"]

    prompt_text = _read_prompt_text()
    from fyp.annotation_schema import get_annotation_json_schema

    schema_json = get_annotation_json_schema()
    if backend is None:
        gen_params = {key: machine.get(key) for key in _VERSION_GEN_PARAM_KEYS}
        descriptor = build_version_descriptor(
            model=machine.get("model"),
            prompt_text=prompt_text,
            schema_json=schema_json,
            gen_params=gen_params,
            label=machine.get("version_label"),
        )
    else:
        prompt_text = prompt_text + backend.prompt_suffix()
        descriptor = build_version_descriptor(
            model=backend.effective_model_id(),
            prompt_text=prompt_text,
            schema_json=schema_json,
            gen_params=backend.version_gen_params(),
            extra_params=backend.version_extra_params(),
            backend=backend_name,
        )
    descriptor["prompt_fn"] = active_prompt_label()

    _DESCRIPTOR_CACHE["signature"] = signature
    _DESCRIPTOR_CACHE["descriptor"] = descriptor
    _DESCRIPTOR_CACHE["prompt_text"] = prompt_text
    _DESCRIPTOR_CACHE["schema_json"] = schema_json
    return descriptor




def current_annotation_version(fresh: bool = False) -> str:
    """Return just the current ``annotation_version`` id, never raising."""
    try:
        return current_version_descriptor(fresh=fresh)["annotation_version"]
    except Exception:
        return "unknown"




def empty_registry() -> dict:
    """Return a fresh, empty version registry."""
    return {"versions": {}, "active": None}




def _snapshot_field_metadata() -> dict:
    """Snapshot the current annotation contract's var_schema column metadata.

    ``{column: {role, scale, display_name, description, section}}`` for the
    contract's flattened output columns — recorded per version so a field a future
    contract stops emitting keeps its metadata (and stays contract-owned) via the
    version that defined it. Never raises.
    """
    from fyp import annotation_contract as ac
    from fyp import registry_metadata as rm

    return rm.snapshot_field_metadata(ac)




def _register_into(
    registry: dict,
    descriptor: dict,
    prompt_text: str | None,
    schema_json: dict | None,
    created_at: str | None = None,
    field_metadata: dict | None = None,
) -> dict:
    """Return a copy of ``registry`` with ``descriptor`` recorded if new.

    Recording a version never changes the ``active`` pointer — the active
    version only ever changes via :func:`promote_version`
    (stay-pinned-until-promote). ``active`` therefore stays ``None`` until the
    first explicit promotion, and consumers treat ``active is None`` as "latest
    annotation per item" (the historical, version-agnostic behaviour).
    """
    registry = _copy.deepcopy(registry)
    versions = registry.setdefault("versions", {})
    version = descriptor["annotation_version"]
    if version not in versions:
        versions[version] = {
            **descriptor,
            "prompt_text": prompt_text,
            "schema_json": schema_json,
            "field_metadata": field_metadata or {},
            "created_at": created_at,
        }
    return registry




def _promote_into(registry: dict, version: str) -> dict:
    """Return a copy of ``registry`` with ``active`` set to ``version``."""
    registry = _copy.deepcopy(registry)
    if version not in registry.get("versions", {}):
        raise KeyError(f"unknown annotation_version: {version}")
    registry["active"] = version
    return registry




def load_registry() -> dict:
    """Load the version registry from storage, or an empty one if absent."""
    if _data_io().exists(storage_location=REGISTRY_LOCATION, filename=REGISTRY_FILENAME):
        registry = _data_io().load_json(
            storage_location=REGISTRY_LOCATION, filename=REGISTRY_FILENAME
        )
        if isinstance(registry, dict) and "versions" in registry:
            return registry
    return empty_registry()




def save_registry(registry: dict) -> None:
    """Persist the version registry to storage."""
    _data_io().save_json(
        data=registry, storage_location=REGISTRY_LOCATION, filename=REGISTRY_FILENAME
    )




def register_version(
    descriptor: dict | None = None,
    prompt_text: str | None = None,
    schema_json: dict | None = None,
    created_at: str | None = None,
    field_metadata: dict | None = None,
) -> dict:
    """Record a version in the registry if it is not already present.

    With no arguments the current configuration's descriptor (and its prompt /
    schema snapshot + var_schema field metadata) is used. Returns the (possibly
    updated) registry.
    """
    if descriptor is None:
        current_version_descriptor()
        descriptor = _DESCRIPTOR_CACHE["descriptor"]
        prompt_text = _DESCRIPTOR_CACHE.get("prompt_text")
        schema_json = _DESCRIPTOR_CACHE.get("schema_json")
    if field_metadata is None:
        field_metadata = _snapshot_field_metadata()
    if created_at is None:
        created_at = _dt.datetime.now().isoformat(timespec="seconds")

    registry = load_registry()
    updated = _register_into(
        registry, descriptor, prompt_text, schema_json, created_at, field_metadata
    )
    if updated != registry:
        save_registry(updated)
    return updated




def get_active_version() -> str | None:
    """Return the currently active (promoted) annotation version, if any."""
    return load_registry().get("active")




def promote_version(version: str) -> dict:
    """Promote ``version`` to be the active version. Returns the registry.

    The synthetic ``v0_legacy`` version only exists to keep pre-versioning legacy
    fields contract-owned — it has no prompt/schema snapshot and can never be the
    active version, so promoting it is rejected.

    Raises:
        ValueError: If ``version`` is the legacy version.
        KeyError: If ``version`` is not in the registry.
    """
    if version == LEGACY_VERSION:
        raise ValueError("the legacy version cannot be promoted")
    registry = _promote_into(load_registry(), version)
    save_registry(registry)
    return registry




def list_versions() -> list[dict]:
    """Return version summaries (without the bulky prompt/schema/metadata snapshots)."""
    registry = load_registry()
    active = registry.get("active")
    summaries = []
    for version, info in registry.get("versions", {}).items():
        summary = {
            k: v for k, v in info.items()
            if k not in ("prompt_text", "schema_json", "field_metadata")
        }
        summary["active"] = version == active
        summaries.append(summary)
    return summaries




VERSIONS_IN_DATA_FILENAME = "annotation_versions_in_data.json"




def versions_in_data() -> set | None:
    """Distinct ``annotation_version`` values present in the consolidated archive.

    Read from the snapshot consolidation writes (see
    :func:`record_versions_in_data`). ``None`` means no snapshot exists yet —
    callers fall back to the unpruned all-versions behaviour. Never raises.
    """
    try:
        if _data_io().exists(storage_location=REGISTRY_LOCATION, filename=VERSIONS_IN_DATA_FILENAME):
            payload = _data_io().load_json(
                storage_location=REGISTRY_LOCATION, filename=VERSIONS_IN_DATA_FILENAME
            )
            values = payload.get("versions")
            if isinstance(values, list):
                return {str(v) for v in values}
    except Exception:
        pass
    return None




def record_versions_in_data(versions) -> None:
    """Persist the distinct ``annotation_version`` values present in the archive.

    Called at consolidation, right after the all-versions archive is
    materialized. Prunes :func:`union_field_metadata`'s legacy union to versions
    that can actually occur in the data — NOTE this feeds the var_schema hash,
    so a consolidation that shrinks the set correctly marks studies for rebuild.
    Never raises.
    """
    try:
        payload = {
            "versions": sorted({str(v) for v in versions if v and str(v) != "unknown"}),
            "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        _data_io().save_json(
            data=payload,
            storage_location=REGISTRY_LOCATION,
            filename=VERSIONS_IN_DATA_FILENAME,
        )
    except Exception:
        pass




def union_field_metadata(versions_to_include: set | None = None) -> dict:
    """Merge ``field_metadata`` across registered versions.

    Returns ``{column: metadata}``. Newer versions (by ``created_at``) win on a
    column present in several snapshots. The var_schema overlay uses this to keep
    fields from PAST contract versions (e.g. ``trend`` / ``australian_relevance``)
    contract-owned and read-only after the current contract stops emitting them.

    By default the union is pruned to the versions actually present in the
    consolidated archive (plus the current contract's version) when consolidation
    has recorded that set — see :func:`record_versions_in_data`; with no snapshot
    every registered version participates (backward-compatible). Never raises.
    """
    try:
        registry = load_registry()
    except Exception as e:
        # Loud on purpose: silently returning {} here once hid an import-cycle
        # failure that cost per-instance schema-hash drift.
        logger.warning(f"WARNING: annotation version registry unreadable ({e}); legacy union empty.")
        return {}
    if versions_to_include is None:
        in_data = versions_in_data()
        if in_data is not None:
            versions_to_include = in_data | {current_annotation_version()}
    from fyp import registry_metadata as rm

    return rm.union_field_metadata(registry, versions_to_include)




def _item_key_cols(df: pd.DataFrame, item_col: str) -> list[str]:
    """Return the per-item key columns for an annotation frame.

    Item ids are only guaranteed unique within a platform, so the key is
    composite ``(source_platform, item_col)`` when the frame carries the
    platform column, and plain ``item_col`` otherwise (legacy frames).
    """
    if "source_platform" in df.columns:
        return ["source_platform", item_col]
    return [item_col]




def select_active_view(
    df: pd.DataFrame,
    active_version: str,
    item_col: str = "item_id",
    version_col: str = "annotation_version",
) -> pd.DataFrame:
    """Build the active annotation view from a multi-version frame.

    Rows of ``active_version`` take precedence per item; items not covered by
    the active version fall back to their latest row from any other version, so
    coverage never drops when a version is promoted. Within a version the last
    row per item is kept. Items are keyed composite ``(source_platform,
    item_col)`` when the platform column is present (see :func:`_item_key_cols`).

    Args:
        df: A frame containing ``item_col`` and ``version_col``.
        active_version: The promoted version to prefer.
        item_col: The per-item key column.
        version_col: The version column.

    Returns:
        One row per item: the active version where available, else the latest
        other version.
    """
    key_cols = _item_key_cols(df, item_col)
    if version_col not in df.columns:
        return df.drop_duplicates(subset=key_cols, keep="last").reset_index(drop=True)
    active_rows = df[df[version_col] == active_version].drop_duplicates(
        subset=key_cols, keep="last"
    )
    if len(key_cols) == 1:
        covered_mask = df[item_col].isin(set(active_rows[item_col]))
    else:
        covered = set(map(tuple, active_rows[key_cols].itertuples(index=False)))
        covered_mask = pd.Series(
            list(map(tuple, df[key_cols].itertuples(index=False))), index=df.index
        ).isin(covered)
    fallback = df[~covered_mask].drop_duplicates(subset=key_cols, keep="last")
    combined = pd.concat([active_rows, fallback], ignore_index=True)
    return combined.reset_index(drop=True)




def select_version_view(
    df: pd.DataFrame,
    version: str,
    item_col: str = "item_id",
    version_col: str = "annotation_version",
) -> pd.DataFrame:
    """Build a strict single-version view (for a version-pinned study).

    Only rows of ``version`` are kept (latest per item, composite-keyed when
    ``source_platform`` is present). Used when a study is pinned to a specific
    annotation version for reproducibility; coverage is intentionally limited
    to items annotated under that version.
    """
    key_cols = _item_key_cols(df, item_col)
    if version_col not in df.columns:
        return df.drop_duplicates(subset=key_cols, keep="last").reset_index(drop=True)
    rows = df[df[version_col] == version].drop_duplicates(subset=key_cols, keep="last")
    return rows.reset_index(drop=True)




def ensure_current_version_registered() -> str:
    """Register the current config's version if new; return its id.

    Safe to call repeatedly (idempotent) and never raises — intended to be
    invoked once per annotation batch before workers start.
    """
    try:
        descriptor = current_version_descriptor()
        register_version(
            descriptor=descriptor,
            prompt_text=_DESCRIPTOR_CACHE.get("prompt_text"),
            schema_json=_DESCRIPTOR_CACHE.get("schema_json"),
        )
        return descriptor["annotation_version"]
    except Exception:
        return "unknown"




def _harvest_orphan_metadata() -> dict:
    """Return metadata of Gemini-source var_schema rows the current contract lacks.

    These are the legacy annotation fields (e.g. ``trend`` / ``australian_relevance``)
    whose metadata still lives in ``var_schema.csv`` because the current contract no
    longer owns them. Harvested so the version registry can take ownership. Never
    raises.
    """
    try:
        from fyp import annotation_contract as ac
        from fyp.fyp_config import fyp_cf

        owned = set(ac.contract_column_metadata(ac.load_contract()))
        vs = fyp_cf.get("var_schema")
        if vs is None or "variable_name" not in getattr(vs, "columns", []):
            return {}

        def _cell(row, col):
            val = row.get(col)
            return None if val is None or (isinstance(val, float) and pd.isna(val)) or pd.isna(val) else str(val)

        out: dict = {}
        for _, row in vs.iterrows():
            name = str(row.get("variable_name"))
            source = str(row.get("source") or "")
            if name in owned:
                continue
            if source == "Gemini" or source.startswith("derived: Gemini"):
                out[name] = {
                    "role": _cell(row, "role"),
                    "scale": _cell(row, "scale"),
                    "display_name": _cell(row, "display_name"),
                    "description": _cell(row, "description"),
                    "section": _cell(row, "section"),
                }
        return out
    except Exception:
        return {}




def backfill_legacy_metadata(orphan_metadata: dict | None = None) -> dict:
    """Seed the registry with metadata for existing legacy annotation fields.

    One-time, idempotent migration for the versions that predate per-version
    metadata snapshotting. Attaches the current orphan Gemini fields' metadata
    (:func:`_harvest_orphan_metadata` — ``trend`` / ``australian_relevance``) to a
    ``v0_legacy`` registry entry (created if absent), so the registry — not
    ``var_schema.csv`` — owns them. ``created_at`` is epoch so it sorts oldest in
    :func:`union_field_metadata` (newer versions win). Returns the registry.
    """
    if orphan_metadata is None:
        orphan_metadata = _harvest_orphan_metadata()

    registry = load_registry()
    versions = registry.setdefault("versions", {})
    entry = versions.get(LEGACY_VERSION)
    if entry is None:
        entry = {
            "annotation_version": LEGACY_VERSION,
            "label": "pre-versioning (legacy)",
            "field_metadata": {},
            "created_at": "1970-01-01T00:00:00",
        }
        versions[LEGACY_VERSION] = entry

    fm = entry.setdefault("field_metadata", {})
    changed = False
    for col, meta in orphan_metadata.items():
        if col not in fm:
            fm[col] = meta
            changed = True
    if changed:
        save_registry(registry)
    return registry




if __name__ == "__main__":
    import json as _json

    import fyp.fyp_config as _fc

    _fc.initialize()
    _reg = backfill_legacy_metadata()
    _summary = {
        v: sorted((e.get("field_metadata") or {}).keys())
        for v, e in _reg.get("versions", {}).items()
    }
    logger.info("Backfilled legacy annotation metadata. Per-version field_metadata keys:")
    logger.info(_json.dumps(_summary, indent=2))
    logger.info(f"union_field_metadata(): {sorted(union_field_metadata().keys())}")
