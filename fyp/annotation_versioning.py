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

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf


REGISTRY_FILENAME = "annotation_versions.json"
REGISTRY_LOCATION = "recoded"
LEGACY_VERSION = "v0_legacy"

# Generation parameters that materially change model output and therefore
# belong in the version identity.
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
) -> dict:
    """Build a self-describing version descriptor and its deterministic id.

    Args:
        model: The model id (e.g. ``"gemini-3-flash-preview"``).
        prompt_text: The exact prompt / system-instruction text.
        schema_json: The response-schema dict, or ``None`` for free text.
        gen_params: The output-affecting generation parameters.
        label: Optional human-readable label; defaults to the model plus a short
            prompt fingerprint.

    Returns:
        A descriptor dict including the computed ``annotation_version``.
    """
    prompt_hash = _sha256_hex(prompt_text or "", 16)
    schema_hash = compute_schema_hash(schema_json)
    normalized_params = {key: gen_params.get(key) for key in _VERSION_GEN_PARAM_KEYS}

    identity = {
        "model": model,
        "prompt_hash": prompt_hash,
        "schema_hash": schema_hash,
        "gen_params": normalized_params,
    }
    canonical = json.dumps(identity, sort_keys=True, ensure_ascii=False)
    version = "av_" + _sha256_hex(canonical, 12)

    return {
        "annotation_version": version,
        "label": label or f"{model}:{prompt_hash[:6]}",
        "model": model,
        "prompt_hash": prompt_hash,
        "schema_hash": schema_hash,
        "gen_params": normalized_params,
    }




def _read_prompt_text() -> str:
    """Read the current prompt file's text."""
    with open(fyp_cf["machine"]["prompt"]) as handle:
        return handle.read()




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
    machine = fyp_cf["machine"]
    use_structured = bool(machine.get("use_structured_output", False))
    signature = (
        machine.get("model"),
        machine.get("prompt"),
        use_structured,
        machine.get("temperature"),
        machine.get("thinking_budget"),
        machine.get("media_resolution"),
        machine.get("max_output_tokens"),
    )
    if not fresh and _DESCRIPTOR_CACHE.get("signature") == signature:
        return _DESCRIPTOR_CACHE["descriptor"]

    prompt_text = _read_prompt_text()
    schema_json = None
    if use_structured:
        from fyp.annotation_schema import get_annotation_json_schema

        schema_json = get_annotation_json_schema()
    gen_params = {key: machine.get(key) for key in _VERSION_GEN_PARAM_KEYS}
    descriptor = build_version_descriptor(
        model=machine.get("model"),
        prompt_text=prompt_text,
        schema_json=schema_json,
        gen_params=gen_params,
        label=machine.get("version_label"),
    )
    descriptor["prompt_fn"] = os.path.basename(fyp_cf["machine"]["prompt"])

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




def _register_into(
    registry: dict,
    descriptor: dict,
    prompt_text: str | None,
    schema_json: dict | None,
    created_at: str | None = None,
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




def register_version(
    descriptor: dict | None = None,
    prompt_text: str | None = None,
    schema_json: dict | None = None,
    created_at: str | None = None,
) -> dict:
    """Record a version in the registry if it is not already present.

    With no arguments the current configuration's descriptor (and its prompt /
    schema snapshot) is used. Returns the (possibly updated) registry.
    """
    if descriptor is None:
        current_version_descriptor()
        descriptor = _DESCRIPTOR_CACHE["descriptor"]
        prompt_text = _DESCRIPTOR_CACHE.get("prompt_text")
        schema_json = _DESCRIPTOR_CACHE.get("schema_json")
    if created_at is None:
        created_at = _dt.datetime.now().isoformat(timespec="seconds")

    registry = load_registry()
    updated = _register_into(registry, descriptor, prompt_text, schema_json, created_at)
    if updated != registry:
        save_registry(updated)
    return updated




def get_active_version() -> str | None:
    """Return the currently active (promoted) annotation version, if any."""
    return load_registry().get("active")




def promote_version(version: str) -> dict:
    """Promote ``version`` to be the active version. Returns the registry."""
    registry = _promote_into(load_registry(), version)
    save_registry(registry)
    return registry




def list_versions() -> list[dict]:
    """Return version summaries (without the bulky prompt/schema snapshots)."""
    registry = load_registry()
    active = registry.get("active")
    summaries = []
    for version, info in registry.get("versions", {}).items():
        summary = {k: v for k, v in info.items() if k not in ("prompt_text", "schema_json")}
        summary["active"] = version == active
        summaries.append(summary)
    return summaries




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
    row per item is kept.

    Args:
        df: A frame containing ``item_col`` and ``version_col``.
        active_version: The promoted version to prefer.
        item_col: The per-item key column.
        version_col: The version column.

    Returns:
        One row per item: the active version where available, else the latest
        other version.
    """
    if version_col not in df.columns:
        return df.drop_duplicates(subset=[item_col], keep="last").reset_index(drop=True)
    active_rows = df[df[version_col] == active_version].drop_duplicates(
        subset=[item_col], keep="last"
    )
    covered = set(active_rows[item_col])
    fallback = df[~df[item_col].isin(covered)].drop_duplicates(
        subset=[item_col], keep="last"
    )
    combined = pd.concat([active_rows, fallback], ignore_index=True)
    return combined.reset_index(drop=True)




def select_version_view(
    df: pd.DataFrame,
    version: str,
    item_col: str = "item_id",
    version_col: str = "annotation_version",
) -> pd.DataFrame:
    """Build a strict single-version view (for a version-pinned study).

    Only rows of ``version`` are kept (latest per item). Used when a study is
    pinned to a specific annotation version for reproducibility; coverage is
    intentionally limited to items annotated under that version.
    """
    if version_col not in df.columns:
        return df.drop_duplicates(subset=[item_col], keep="last").reset_index(drop=True)
    rows = df[df[version_col] == version].drop_duplicates(subset=[item_col], keep="last")
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
