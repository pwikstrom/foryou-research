"""Loader, validator, and accessors for the declarative scrape contract
(``config/scrape_contract.toml``).

The contract is the single source for the canonical, cross-platform scrape
schema. It is to the scraper what ``config/annotation_contract.toml`` is to the
Gemini annotator. From it:

  * the scraper base class (:mod:`fyp.platform_scraper`) reads its base
    (``scope="base"``) and per-platform (``scope="platform"``) field sets and
    PyArrow dtypes — the ``REQUIRED_COLUMNS`` / ``additional_columns`` analogue
    from :mod:`fyp.ingest`;
  * :func:`fyp.fyp_config._apply_contract_scrape_metadata` overlays
    role / scale / display_name / description / section onto the matching
    ``var_schema`` rows (and injects any missing scrape columns);
  * :func:`fyp.recode_variables.compute_var_schema_hash` folds a digest of the
    field set in, so a contract edit invalidates cached study parquets.

The per-field surface mirrors the annotation contract's where it overlaps
(``role`` / ``scale`` / ``display_name`` / ``description`` / ``section``) and
adds the two keys the scraper needs that the annotation contract does not: the
stored ``dtype`` and the per-platform ``per_k_of`` engagement denominator.
"""

import tomllib
from pathlib import Path

_DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "scrape_contract.toml"
)

# The two field scopes: a base field every platform emits, vs a field owned by a
# single platform (the subclass's additional_columns).
VALID_SCOPES = frozenset({"base", "platform"})

# Legacy → canonical scrape column renames (the "canonicalize everywhere"
# migration). Applied in-memory to var_schema rows at load
# (fyp_config._apply_contract_scrape_metadata) and to legacy on-disk scrape
# parquets at consolidation (fyp.scrape), so un-migrated data self-heals.
LEGACY_COLUMN_ALIASES: dict[str, str] = {
    "createTime": "create_time",
    "author_nickname": "author_name",
    "video_duration": "duration",
    "stats_playCount": "play_count",
    "saves_per_play": "saves_per_K_play",
    "comments_per_play": "comments_per_K_play",
    "faves_per_play": "faves_per_K_play",
    "shares_per_play": "shares_per_K_play",
}

# The subset of LEGACY_COLUMN_ALIASES whose stored VALUES are also scaled ×1000
# (per-play → per-thousand-plays) when migrating legacy parquet values. The
# column rename alone is in LEGACY_COLUMN_ALIASES; the value conversion lives at
# consolidation.
PER_PLAY_TO_PER_K: dict[str, str] = {
    "saves_per_play": "saves_per_K_play",
    "comments_per_play": "comments_per_K_play",
    "faves_per_play": "faves_per_K_play",
    "shares_per_play": "shares_per_K_play",
}




def default_contract_path() -> Path:
    """Return the repo-relative default path to the scrape contract."""
    return _DEFAULT_CONTRACT_PATH




def load_contract(path: str | Path | None = None) -> dict:
    """Load and validate the scrape contract from a TOML file.

    Args:
        path: Path to the contract TOML. Defaults to
            ``config/scrape_contract.toml`` next to the project root.

    Returns:
        The parsed contract dict.

    Raises:
        FileNotFoundError: if the contract file does not exist.
        ValueError: if the contract fails validation.
    """
    contract_path = Path(path) if path is not None else _DEFAULT_CONTRACT_PATH
    if not contract_path.exists():
        raise FileNotFoundError(f"Scrape contract not found: {contract_path}")
    with open(contract_path, "rb") as handle:
        contract = tomllib.load(handle)
    errors = validate_contract(contract)
    if errors:
        joined = "\n  - ".join(errors)
        raise ValueError(f"Invalid scrape contract ({contract_path}):\n  - {joined}")
    return contract




def default_platform(contract: dict) -> str | None:
    """Return the platform a no-argument ``get_scraper()`` selects by default."""
    return contract.get("meta", {}).get("default_platform")




def platforms(contract: dict) -> list[str]:
    """Return the distinct platforms that own at least one ``scope="platform"`` field."""
    seen: list[str] = []
    for field in contract.get("fields", []):
        plat = field.get("platform")
        if field.get("scope") == "platform" and plat and plat not in seen:
            seen.append(plat)
    return seen




def base_fields(contract: dict) -> list[dict]:
    """Return the base (cross-platform) fields in document order."""
    return [f for f in contract.get("fields", []) if f.get("scope") == "base"]




def platform_fields(contract: dict, platform: str) -> list[dict]:
    """Return the fields a single platform owns, in document order."""
    return [
        f
        for f in contract.get("fields", [])
        if f.get("scope") == "platform" and f.get("platform") == platform
    ]




def base_field_names(contract: dict) -> list[str]:
    """Return the ordered canonical column names of the base fields."""
    return [f["name"] for f in base_fields(contract)]




def field_dtypes(contract: dict, platform: str | None = None) -> dict[str, str]:
    """Return ``{column: pyarrow_dtype_str}`` for the base set, plus a platform's fields.

    With ``platform=None`` only the base fields are returned (the
    ``REQUIRED_COLUMNS`` analogue). With a platform name, that platform's fields
    are added (the ``additional_columns`` analogue). Document order is preserved.

    Args:
        contract: the parsed contract dict.
        platform: optional platform whose fields to include alongside the base set.

    Returns:
        Mapping of canonical column name → PyArrow dtype string.
    """
    out: dict[str, str] = {}
    for field in contract.get("fields", []):
        scope = field.get("scope")
        if scope == "base":
            out[field["name"]] = field.get("dtype")
        elif scope == "platform" and platform is not None and field.get("platform") == platform:
            out[field["name"]] = field.get("dtype")
    return out




def derived_fields(contract: dict, platform: str | None = None) -> set[str]:
    """Return the names of fields computed at scrape time (``derived = true``).

    These are not raw-mapped from the platform payload — the base class fills
    them (``scrape_status`` / ``scrape_ts`` / ``storage_link`` / the per-K rates /
    ``plays_per_day``). Scoped like :func:`field_dtypes`.
    """
    out: set[str] = set()
    for field in contract.get("fields", []):
        if not field.get("derived"):
            continue
        scope = field.get("scope")
        if scope == "base" or (
            scope == "platform" and platform is not None and field.get("platform") == platform
        ):
            out.add(field["name"])
    return out




def per_k_sources(contract: dict, platform: str) -> dict[str, str]:
    """Return ``{rate_field: raw_count_column}`` for a platform's per-K ratios.

    Read from ``[perk.<platform>]``. The base class derives each rate as
    ``raw_count / play_count * 1000``.
    """
    return dict(contract.get("perk", {}).get(platform, {}))




def contract_column_metadata(contract: dict) -> dict[str, dict]:
    """Return ``{column: {role, scale, display_name, description, section}}``.

    The var_schema overlay payload — one entry per field that declares any
    var_schema metadata (``role`` / ``scale`` / ``display_name``); a field with
    none is not contract-owned and is skipped. A plain carried column declares
    ``scale`` / ``display_name`` but no ``role`` (blank role is the default). The
    scrape contract has no object/array flattening, so the column name is the
    field ``name`` directly.

    Args:
        contract: the parsed contract dict.

    Returns:
        Mapping of var_schema column name → its contract-owned metadata.
    """
    out: dict[str, dict] = {}
    for field in contract.get("fields", []):
        name = field.get("name")
        if not name or not (field.get("role") or field.get("scale") or field.get("display_name")):
            continue
        out[name] = {
            "role": field.get("role"),
            "scale": field.get("scale"),
            "display_name": field.get("display_name"),
            "description": field.get("description"),
            "section": field.get("section"),
            # source is semantic (a "derived:" prefix short-circuits the recode
            # plan), so the contract owns it: explicit per-field override, else
            # the derived-membership default.
            "source": field.get("source")
            or ("derived: scrape" if field.get("derived") else "scrape"),
        }
    return out




def contract_field_digest(contract: dict) -> dict:
    """Return a compact, order-independent view of the field set for hashing.

    Folded into :func:`fyp.recode_variables.compute_var_schema_hash` so a change
    to a stored dtype, a scope/platform reassignment, or a per-K mapping
    invalidates cached study parquets (mirroring the annotation contract's
    ``gm_digest`` fold).
    """
    return {
        "fields": {
            f["name"]: {
                "scope": f.get("scope"),
                "platform": f.get("platform"),
                "dtype": f.get("dtype"),
                "derived": bool(f.get("derived")),
                "per_k_of": f.get("per_k_of"),
            }
            for f in contract.get("fields", [])
            if f.get("name")
        },
        "perk": contract.get("perk", {}),
    }




def validate_contract(contract: dict) -> list[str]:
    """Validate the scrape contract; return a list of error strings (empty = valid).

    Lets a frequent editor catch mistakes before they reach the scraper or the
    var_schema overlay. Mirrors the protective role of
    :func:`fyp.annotation_contract.validate_contract`.

    Args:
        contract: the parsed contract dict.

    Returns:
        A list of human-readable validation errors (empty when valid).
    """
    errors: list[str] = []
    fields = contract.get("fields", [])

    if not contract.get("meta", {}).get("default_platform"):
        errors.append("missing [meta].default_platform")
    if not fields:
        errors.append("contract has no [[fields]]")

    # var_schema role/scale vocabularies live in recode_variables; import lazily so
    # this module never pulls in fyp_config (which recode_variables imports) at load.
    try:
        from fyp.recode_variables import VAR_SCHEMA_ROLES, VAR_SCHEMA_SCALES
        valid_roles, valid_scales = set(VAR_SCHEMA_ROLES), set(VAR_SCHEMA_SCALES)
    except Exception:
        valid_roles, valid_scales = None, None

    seen_names: set[str] = set()
    base_names: set[str] = set()
    field_names: set[str] = set()
    for field in fields:
        name = field.get("name")
        where = f"field '{name}'"
        if not name:
            errors.append("a field is missing 'name'")
            continue
        if name in seen_names:
            errors.append(f"duplicate field name '{name}'")
        seen_names.add(name)
        field_names.add(name)

        scope = field.get("scope")
        if scope not in VALID_SCOPES:
            errors.append(f"{where}: scope must be one of {sorted(VALID_SCOPES)}")
        if scope == "base":
            base_names.add(name)
        if scope == "platform" and not field.get("platform"):
            errors.append(f"{where}: scope='platform' requires a 'platform'")

        if not field.get("dtype"):
            errors.append(f"{where}: missing 'dtype'")
        if not field.get("section"):
            errors.append(f"{where}: missing 'section'")

        role = field.get("role")
        scale = field.get("scale")
        if valid_roles is not None and role is not None and role not in valid_roles:
            errors.append(f"{where}: invalid role '{role}'")
        if valid_scales is not None and scale is not None and scale not in valid_scales:
            errors.append(f"{where}: invalid scale '{scale}'")

    # per_k_of must name a base field (the denominator, e.g. play_count).
    for field in fields:
        denom = field.get("per_k_of")
        if denom is not None and denom not in base_names:
            errors.append(f"field '{field.get('name')}': per_k_of '{denom}' is not a base field")

    # Every [perk.<platform>] entry maps a base rate field to an existing source column.
    for plat, mapping in contract.get("perk", {}).items():
        if not isinstance(mapping, dict):
            errors.append(f"[perk.{plat}] must be a table of rate_field → source_column")
            continue
        for rate_field, source_col in mapping.items():
            if rate_field not in base_names:
                errors.append(f"[perk.{plat}]: '{rate_field}' is not a base field")
            if source_col not in field_names:
                errors.append(f"[perk.{plat}].{rate_field}: source '{source_col}' is not a contract field")

    return errors
