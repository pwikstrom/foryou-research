"""Loader, validator, and accessors for the declarative activity contract
(``config/activity_contract.toml``).

The activity contract is the single source for the canonical, platform-agnostic
ACTIVITY schema — the donation/engagement stream ingested by :mod:`fyp.ingest`.
It is to ingestion what ``config/scrape_contract.toml`` is to the scraper and
``config/annotation_contract.toml`` is to the Gemini annotator. From it:

  * :mod:`fyp.ingest` reads its required columns + PyArrow dtypes (the hardcoded
    ``REQUIRED_COLUMNS`` / ``additional_columns`` analogue) and the required-core
    field set that drives the per-row hard-drop integrity gate;
  * :func:`fyp.fyp_config._apply_contract_activity_metadata` overlays
    role / scale / display_name / description / section onto the matching
    ``var_schema`` rows (and injects any missing activity columns);
  * :func:`fyp.recode_variables.compute_var_schema_hash` folds a digest of the
    field set in, so a contract edit invalidates cached study parquets.

Field scopes: ``base`` (every platform emits it) vs ``platform`` (a single
platform's extra; currently empty — play_duration went base). A ``derived`` field is
computed after ingestion (``session_id`` / the ``local_*`` features /
``activity_contract_version``) and so is owned for metadata but is not part of
the required-column set. A ``required`` field is one whose null value makes a row
invalid (the hard-drop gate).
"""

import tomllib
from pathlib import Path

import fyp

# Anchored on the fyp package (not this file) so the path survives module
# moves within the package tree.
_DEFAULT_CONTRACT_PATH = (
    Path(fyp.__file__).resolve().parent.parent / "config" / "activity_contract.toml"
)

# A base field every platform emits, vs a field owned by a single platform.
VALID_SCOPES = frozenset({"base", "platform"})




def default_contract_path() -> Path:
    """Return the repo-relative default path to the activity contract."""
    return _DEFAULT_CONTRACT_PATH




def load_contract(path: str | Path | None = None) -> dict:
    """Load and validate the activity contract from a TOML file.

    Args:
        path: Path to the contract TOML. Defaults to
            ``config/activity_contract.toml`` next to the project root.

    Returns:
        The parsed contract dict.

    Raises:
        FileNotFoundError: if the contract file does not exist.
        ValueError: if the contract fails validation.
    """
    contract_path = Path(path) if path is not None else _DEFAULT_CONTRACT_PATH
    if not contract_path.exists():
        raise FileNotFoundError(f"Activity contract not found: {contract_path}")
    with open(contract_path, "rb") as handle:
        contract = tomllib.load(handle)
    errors = validate_contract(contract)
    if errors:
        joined = "\n  - ".join(errors)
        raise ValueError(f"Invalid activity contract ({contract_path}):\n  - {joined}")
    return contract




def required_columns(contract: dict) -> dict[str, str]:
    """Return ``{column: pyarrow_dtype}`` for the ingested base columns.

    The ``REQUIRED_COLUMNS`` analogue from :mod:`fyp.ingest`: every ``scope="base"``
    field that is not ``derived`` (derived fields — ``session_id`` / ``local_*`` /
    the provenance stamp — are added after ingestion, not read from raw data).
    Document order is preserved.
    """
    out: dict[str, str] = {}
    for field in contract.get("fields", []):
        if field.get("scope") == "base" and not field.get("derived"):
            out[field["name"]] = field.get("dtype")
    return out




def platform_columns(contract: dict, platform: str | None) -> dict[str, str]:
    """Return ``{column: pyarrow_dtype}`` for a single platform's extra fields.

    The ``additional_columns`` analogue (currently no platform-scoped fields). Returns
    an empty dict when ``platform`` is None.
    """
    out: dict[str, str] = {}
    if platform is None:
        return out
    for field in contract.get("fields", []):
        if field.get("scope") == "platform" and field.get("platform") == platform:
            out[field["name"]] = field.get("dtype")
    return out




def required_core_fields(contract: dict) -> list[str]:
    """Return the field names whose null value makes a row invalid (``required=true``).

    These drive the per-row hard-drop integrity gate in :meth:`ingest._standardize`.
    """
    return [f["name"] for f in contract.get("fields", []) if f.get("required")]




def derived_fields(contract: dict) -> set[str]:
    """Return the names of fields computed after ingestion (``derived = true``)."""
    return {f["name"] for f in contract.get("fields", []) if f.get("derived")}




def platforms(contract: dict) -> list[str]:
    """Return the distinct platforms that own at least one ``scope="platform"`` field."""
    seen: list[str] = []
    for field in contract.get("fields", []):
        plat = field.get("platform")
        if field.get("scope") == "platform" and plat and plat not in seen:
            seen.append(plat)
    return seen




def contract_column_metadata(contract: dict) -> dict[str, dict]:
    """Return ``{column: {role, scale, display_name, description, section}}``.

    The var_schema overlay payload — one entry per field that declares any
    var_schema metadata (``role`` / ``scale`` / ``display_name``); a field with
    none is not contract-owned and is skipped.
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
            # skip_recode short-circuits the recode plan (the column is produced
            # elsewhere), so the contract owns it: explicit per-field override,
            # else the derived-membership default.
            "skip_recode": bool(field.get("skip_recode", field.get("derived", False))),
        }
    return out




def contract_field_digest(contract: dict) -> dict:
    """Return a compact, order-independent view of the field set for hashing.

    Folded into :func:`fyp.recode_variables.compute_var_schema_hash` so a change
    to a stored dtype, a scope/platform reassignment, or the required/derived
    flags invalidates cached study parquets (mirroring the scrape contract).
    """
    return {
        "fields": {
            f["name"]: {
                "scope": f.get("scope"),
                "platform": f.get("platform"),
                "dtype": f.get("dtype"),
                "derived": bool(f.get("derived")),
                "required": bool(f.get("required")),
            }
            for f in contract.get("fields", [])
            if f.get("name")
        }
    }




def validate_contract(contract: dict) -> list[str]:
    """Validate the activity contract; return a list of error strings (empty = valid).

    Mirrors :func:`fyp.scrape_contract.validate_contract`.

    Args:
        contract: the parsed contract dict.

    Returns:
        A list of human-readable validation errors (empty when valid).
    """
    errors: list[str] = []
    fields = contract.get("fields", [])
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
    for field in fields:
        name = field.get("name")
        where = f"field '{name}'"
        if not name:
            errors.append("a field is missing 'name'")
            continue
        if name in seen_names:
            errors.append(f"duplicate field name '{name}'")
        seen_names.add(name)

        scope = field.get("scope")
        if scope not in VALID_SCOPES:
            errors.append(f"{where}: scope must be one of {sorted(VALID_SCOPES)}")
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
        transform = field.get("transform")
        if transform is not None and transform != "log1p":
            errors.append(f"{where}: invalid transform '{transform}' (only 'log1p' is supported)")

        if field.get("required") and field.get("derived"):
            errors.append(f"{where}: a field cannot be both 'required' and 'derived'")

    return errors
