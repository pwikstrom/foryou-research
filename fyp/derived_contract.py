"""Loader, validator, and accessors for the declarative derived contract
(``config/derived_contract.toml``).

The derived contract owns the var_schema metadata for the enrichment columns
COMPUTED AT MERGE TIME in :func:`fyp.organize_datasets.new_merge`
(``days_since_created`` / ``completion_rate`` / ``scraped_fail``) and the
embeddings-derived niche columns (``niche`` / ``niche_name``). It is metadata-only
— unlike the scrape/activity contracts it drives no ingestion or computation; the
merge-time calculations stay in ``organize_datasets``. Its job is to move
role / scale / display_name / description / section for these columns out of
``var_schema.csv`` and into a contract, exactly like the other contracts, and to
fold a field-set digest into the var_schema hash.

``plays_per_day`` is intentionally NOT here — it is derived at scrape time and is
owned by the scrape contract.
"""

import tomllib
from pathlib import Path

_DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "derived_contract.toml"
)




def default_contract_path() -> Path:
    """Return the repo-relative default path to the derived contract."""
    return _DEFAULT_CONTRACT_PATH




def load_contract(path: str | Path | None = None) -> dict:
    """Load and validate the derived contract from a TOML file.

    Args:
        path: Path to the contract TOML. Defaults to
            ``config/derived_contract.toml`` next to the project root.

    Returns:
        The parsed contract dict.

    Raises:
        FileNotFoundError: if the contract file does not exist.
        ValueError: if the contract fails validation.
    """
    contract_path = Path(path) if path is not None else _DEFAULT_CONTRACT_PATH
    if not contract_path.exists():
        raise FileNotFoundError(f"Derived contract not found: {contract_path}")
    with open(contract_path, "rb") as handle:
        contract = tomllib.load(handle)
    errors = validate_contract(contract)
    if errors:
        joined = "\n  - ".join(errors)
        raise ValueError(f"Invalid derived contract ({contract_path}):\n  - {joined}")
    return contract




def derived_fields(contract: dict) -> set[str]:
    """Return the names of contract fields (all derived by definition)."""
    return {f["name"] for f in contract.get("fields", []) if f.get("name")}




def contract_column_metadata(contract: dict) -> dict[str, dict]:
    """Return ``{column: {role, scale, display_name, description, section}}``.

    The var_schema overlay payload — one entry per field that declares any
    var_schema metadata (``role`` / ``scale`` / ``display_name``).
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
        }
    return out




def contract_field_digest(contract: dict) -> dict:
    """Return a compact, order-independent view of the field set for hashing.

    Folded into :func:`fyp.recode_variables.compute_var_schema_hash` so a derived
    contract edit invalidates cached study parquets.
    """
    return {
        "fields": {
            f["name"]: {"dtype": f.get("dtype"), "role": f.get("role"), "scale": f.get("scale")}
            for f in contract.get("fields", [])
            if f.get("name")
        }
    }




def validate_contract(contract: dict) -> list[str]:
    """Validate the derived contract; return a list of error strings (empty = valid)."""
    errors: list[str] = []
    fields = contract.get("fields", [])
    if not fields:
        errors.append("contract has no [[fields]]")

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
        if not field.get("section"):
            errors.append(f"{where}: missing 'section'")
        role = field.get("role")
        scale = field.get("scale")
        if valid_roles is not None and role is not None and role not in valid_roles:
            errors.append(f"{where}: invalid role '{role}'")
        if valid_scales is not None and scale is not None and scale not in valid_scales:
            errors.append(f"{where}: invalid scale '{scale}'")

    return errors
