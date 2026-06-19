"""Loader, validator, and field-spec builder for the declarative annotation
contract (``config/annotation_contract.toml``).

The contract is the single source from which the machine-annotation pipeline
generates the Gemini prompt, the structured-output ``response_schema``, and the
flattener field specs (see ``fyp.annotation_schema``). This module parses the
TOML, validates it, and turns each field into the
``(gemini_field, json_schema_node, flatten_rule)`` tuple the rest of the
pipeline already consumes — so the schema/flatten builders stay unchanged and
only their *source* moves into data.
"""

import tomllib
from pathlib import Path

VALID_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "array", "object"}
)

VALID_FLATTEN_RULES = frozenset(
    {
        "scalar",
        "list_join",
        "transcript_join",
        "scenes_join",
        "faces_unpack",
        "audio_unpack",
        "score_join",
    }
)

VALID_CONDITIONS = frozenset({"issue_event", "political_gt_threshold"})

_DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "annotation_contract.toml"
)

_TOKEN_RE = None  # lazily compiled in render_prompt_text





def default_contract_path() -> Path:
    """Return the repo-relative default path to the annotation contract."""
    return _DEFAULT_CONTRACT_PATH





def load_contract(path: str | Path | None = None) -> dict:
    """Load and validate the annotation contract from a TOML file.

    Args:
        path: Path to the contract TOML. Defaults to
            ``config/annotation_contract.toml`` next to the project root.

    Returns:
        The parsed contract dict.

    Raises:
        FileNotFoundError: if the contract file does not exist.
        ValueError: if the contract fails validation.
    """
    contract_path = Path(path) if path is not None else _DEFAULT_CONTRACT_PATH
    if not contract_path.exists():
        raise FileNotFoundError(f"Annotation contract not found: {contract_path}")
    with open(contract_path, "rb") as handle:
        contract = tomllib.load(handle)
    errors = validate_contract(contract)
    if errors:
        joined = "\n  - ".join(errors)
        raise ValueError(
            f"Invalid annotation contract ({contract_path}):\n  - {joined}"
        )
    return contract





def steps_by_number(contract: dict) -> dict[int, dict]:
    """Return the contract's steps keyed by integer step number."""
    return {int(k): v for k, v in contract.get("steps", {}).items()}





def enum_values(contract: dict, name: str) -> list[str]:
    """Return the ordered value list for a named enum."""
    return list(contract["enums"][name]["values"])





def conditional_field_groups(contract: dict) -> dict[str, tuple[str, ...]]:
    """Group conditional fields by their condition label, in contract order.

    Returns:
        Mapping of condition label (e.g. ``"issue_event"``) to the tuple of
        field names carrying it.
    """
    groups: dict[str, list[str]] = {}
    for field in contract.get("fields", []):
        cond = field.get("conditional")
        if cond:
            groups.setdefault(cond, []).append(field["name"])
    return {k: tuple(v) for k, v in groups.items()}





def _build_node(spec: dict, contract: dict) -> dict:
    """Build a JSON-schema node dict from a field or sub-field spec.

    Reproduces the exact shape the hand-written ``_str``/``_obj``/``_list_of``
    helpers produced, so the generated schema is byte-identical to the legacy
    ``FIELD_SPECS``.

    Args:
        spec: a field or sub-field spec (``type`` plus optional ``enum``,
            ``description``, ``minimum``/``maximum``, ``items``, ``maxItems``,
            ``properties``, ``required``).
        contract: the full contract (for resolving enum references).

    Returns:
        A JSON-schema node dict.
    """
    json_type = spec["type"]
    node: dict = {"type": json_type}

    if json_type == "string":
        if "description" in spec:
            node["description"] = spec["description"]
        if "enum" in spec:
            node["enum"] = enum_values(contract, spec["enum"])

    elif json_type in ("integer", "number"):
        if "minimum" in spec:
            node["minimum"] = spec["minimum"]
        if "maximum" in spec:
            node["maximum"] = spec["maximum"]
        if "description" in spec:
            node["description"] = spec["description"]

    elif json_type == "array":
        node["items"] = _build_node(spec["items"], contract)
        if "description" in spec:
            node["description"] = spec["description"]
        if "maxItems" in spec:
            node["maxItems"] = spec["maxItems"]

    elif json_type == "object":
        properties: dict = {}
        for prop in spec.get("properties", []):
            properties[prop["name"]] = _build_node(prop, contract)
        node["properties"] = properties
        node["required"] = list(spec.get("required", []))

    return node





def build_field_specs(contract: dict) -> list[tuple[str, dict, str]]:
    """Build the ``(gemini_field, json_schema_node, flatten_rule)`` list.

    Args:
        contract: the parsed contract.

    Returns:
        Field specs in contract order — the same structure as the legacy
        hand-written ``FIELD_SPECS``.
    """
    specs: list[tuple[str, dict, str]] = []
    for field in contract.get("fields", []):
        specs.append((field["name"], _build_node(field, contract), field["flatten"]))
    return specs





def render_prompt_text(text: str, field: dict, contract: dict) -> str:
    """Render ``{enum}`` / ``{enum:NAME}`` tokens in a prompt bullet.

    ``{enum}`` renders the field's own enum (a numbered ``value - description``
    list when the enum carries descriptions, else a comma-separated quoted
    list). ``{enum:NAME}`` renders a named enum as a comma-separated quoted list.

    Args:
        text: the raw prompt text for a field bullet.
        field: the field spec (for resolving ``{enum}``).
        contract: the full contract (for resolving named enums).

    Returns:
        The text with enum tokens expanded.
    """
    import re

    global _TOKEN_RE
    if _TOKEN_RE is None:
        _TOKEN_RE = re.compile(r"\{enum(?::([a-zA-Z0-9_]+))?\}")

    def _replace(match: "re.Match") -> str:
        name = match.group(1) or field.get("enum")
        if not name or name not in contract.get("enums", {}):
            return match.group(0)
        enum = contract["enums"][name]
        values = enum["values"]
        descriptions = enum.get("descriptions")
        if descriptions:
            return "\n".join(
                f"     {i}. {v} - {descriptions.get(v, '')}".rstrip(" -")
                for i, v in enumerate(values, 1)
            )
        return ", ".join(f'"{v}"' for v in values)

    return _TOKEN_RE.sub(_replace, text)





def _enum_refs_in_text(text: str) -> list[str]:
    """Return explicit ``{enum:NAME}`` references found in a prompt string."""
    import re

    return re.findall(r"\{enum:([a-zA-Z0-9_]+)\}", text or "")





def validate_contract(contract: dict) -> list[str]:
    """Validate the annotation contract; return a list of error strings.

    Mirrors the protective role of ``recode_variables.validate_var_schema`` for
    the var_schema: it lets a frequent editor catch mistakes before they reach a
    live Gemini call. An empty list means the contract is valid.

    Args:
        contract: the parsed contract dict.

    Returns:
        A list of human-readable validation errors (empty when valid).
    """
    errors: list[str] = []
    enums = contract.get("enums", {})
    steps = contract.get("steps", {})
    fields = contract.get("fields", [])

    if not fields:
        errors.append("contract has no [[fields]]")
    if "prompt" not in contract or "header" not in contract.get("prompt", {}):
        errors.append("missing [prompt].header")

    step_keys = set()
    for key in steps:
        try:
            step_keys.add(int(key))
        except (TypeError, ValueError):
            errors.append(f"step key '{key}' is not an integer")

    def _check_node(spec: dict, where: str) -> None:
        json_type = spec.get("type")
        if json_type not in VALID_TYPES:
            errors.append(f"{where}: invalid type '{json_type}'")
            return
        if "enum" in spec and spec["enum"] not in enums:
            errors.append(f"{where}: unknown enum ref '{spec['enum']}'")
        if json_type == "array":
            if "items" not in spec:
                errors.append(f"{where}: array missing 'items'")
            else:
                _check_node(spec["items"], f"{where}.items")
        if json_type == "object":
            prop_names = [p.get("name") for p in spec.get("properties", [])]
            if not prop_names:
                errors.append(f"{where}: object has no properties")
            for prop in spec.get("properties", []):
                _check_node(prop, f"{where}.{prop.get('name')}")
            for req in spec.get("required", []):
                if req not in prop_names:
                    errors.append(f"{where}: required '{req}' not in properties")

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
        if field.get("flatten") not in VALID_FLATTEN_RULES:
            errors.append(f"{where}: invalid flatten '{field.get('flatten')}'")
        if field.get("step") not in step_keys:
            errors.append(f"{where}: step {field.get('step')} not defined in [steps]")
        cond = field.get("conditional")
        if cond is not None and cond not in VALID_CONDITIONS:
            errors.append(f"{where}: unknown conditional '{cond}'")
        _check_node(field, where)
        for ref in _enum_refs_in_text(field.get("prompt", "")):
            if ref not in enums:
                errors.append(f"{where}: prompt references unknown enum '{ref}'")

    return errors
