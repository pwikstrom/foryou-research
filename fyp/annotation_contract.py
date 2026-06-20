"""Loader, validator, and field-spec builder for the declarative annotation
contract (``config/annotation_contract.toml``).

The contract is the single source from which the machine-annotation pipeline
generates the Gemini prompt, the structured-output ``response_schema``, and the
flattener field specs (see ``fyp.annotation_schema``). This module parses the
TOML, validates it, and turns each field into the
``(gemini_field, json_schema_node, flatten_rule)`` tuple the rest of the
pipeline consumes.

The per-field surface is intentionally small: everything except ``name`` and
``section`` is optional, and the flatten rule + full JSON-schema node + the
``required`` set are *inferred* from the field's ``type`` / ``enum`` / ``array``
/ ``keys``. The three flatten rules are:

  * ``scalar``        — a string / integer leaf.
  * ``list_join``     — an array of strings / enums (pipe-joined).
  * ``object_unpack`` — an object (or array of objects) whose sub-keys explode
    into ``<field>_<key>`` columns.
"""

import re
import tomllib
from pathlib import Path

# A ``[fields.keys]`` sub-key declared as a bounded integer: ``"int(0,100): desc"``
# (or ``"int: desc"`` with no bounds). Lets an object sub-key be a clean number
# the generic numeric recode rescales — no per-field parser.
_INT_SUBKEY_RE = re.compile(r"^int\s*(?:\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\))?\s*:\s*(.*)$", re.DOTALL)

# The leaf/container types a field (or object sub-key) may declare.
VALID_TYPES = frozenset({"string", "int", "object"})

_DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "annotation_contract.toml"
)




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




def sections(contract: dict) -> list[dict]:
    """Return the contract's prompt sections in document order."""
    return list(contract.get("section", []))




def enum_values(contract: dict, name: str) -> list[str]:
    """Return the ordered value list for a named enum.

    An enum is either a bare list (``["Yes", "No"]``) or a table mapping each
    value to a description (key order is the canonical order).
    """
    enum = contract["enums"][name]
    if isinstance(enum, dict):
        return list(enum.keys())
    return list(enum)




def enum_descriptions(contract: dict, name: str) -> dict | None:
    """Return the value→description map for an enum, or ``None`` if it has none."""
    enum = contract["enums"][name]
    if isinstance(enum, dict):
        return dict(enum)
    return None




def enum_field_names(contract: dict) -> set[str]:
    """Return the names of fields whose value is a closed enum.

    A closed-enum field is one structured output already constrains to a fixed
    value set, so the recode pipeline neither folds it through ``GENERIC_MAPPER``
    nor stoplists it. Used by ``recode_variables.build_field_normalization`` as
    the single discriminator for the retired ``mapper`` / ``ignore_strings``
    var_schema columns.
    """
    return {f["name"] for f in contract.get("fields", []) if f.get("enum")}




def field_numeric_range(contract: dict, name: str) -> tuple[int, int] | None:
    """Return ``(min, max)`` for an ``int`` field that declares both bounds.

    Used by the generic numeric recode to normalise a bounded integer (e.g. a
    0-100 score) into a 0-1 ratio, so no per-field parser is needed.
    """
    for field in contract.get("fields", []):
        if field.get("name") == name and field.get("type") == "int":
            if "min" in field and "max" in field:
                return (int(field["min"]), int(field["max"]))
            return None
    return None




def contract_numeric_ranges(contract: dict) -> dict[str, tuple[int, int]]:
    """Return ``{flattened_column: (min, max)}`` for every bounded numeric field.

    Covers top-level ``int`` fields (keyed by name) and object sub-keys declared
    ``"int(lo,hi): ..."`` (keyed by BOTH the bare sub-key and the ``<object>_<key>``
    form, so the lookup works whether or not the flattener strips the object
    prefix). The generic numeric recode uses this to rescale a value to 0-1.
    """
    ranges: dict[str, tuple[int, int]] = {}
    for field in contract.get("fields", []):
        name = field.get("name")
        if field.get("type") == "int" and "min" in field and "max" in field:
            ranges[name] = (int(field["min"]), int(field["max"]))
        if field.get("type") == "object":
            for key, spec in field.get("keys", {}).items():
                if isinstance(spec, str):
                    m = _INT_SUBKEY_RE.match(spec)
                    if m and m.group(1) is not None:
                        rng = (int(m.group(1)), int(m.group(2)))
                        ranges[key] = rng
                        ranges[f"{name}_{key}"] = rng
    return ranges




def contract_numeric_array_fields(contract: dict) -> set[str]:
    """Return flattened column names that are an array of numbers (one per item).

    A sub-key declared ``int`` inside an ``array``-of-objects field (e.g.
    ``faces.age_estimate``) flattens to a pipe-joined list of numbers; the generic
    ``recode_numeric_mean`` collapses it to the mean. Keyed by BOTH the bare
    sub-key and the ``<object>_<key>`` form (the flattener may strip the prefix).
    """
    out: set[str] = set()
    for field in contract.get("fields", []):
        if field.get("type") == "object" and field.get("array"):
            for key, spec in field.get("keys", {}).items():
                if isinstance(spec, str) and _INT_SUBKEY_RE.match(spec):
                    out.add(key)
                    out.add(f"{field.get('name')}_{key}")
    return out




def field_drop_words(contract: dict) -> dict[str, list[str]]:
    """Return the per-field recode stop words declared in ``[recode.drop]``.

    The table is keyed by the *flattened output column name* (so it can target
    object sub-keys such as ``notable_sounds`` as well as top-level fields), each
    mapping to a small list of extra words the recode pipeline drops on top of
    the global ``IRRELEVANT_WORDS`` stoplist. Co-locating these few field-specific
    extras in the contract is what lets the var_schema drop its ``ignore_strings``
    column entirely.
    """
    drop = contract.get("recode", {}).get("drop", {})
    return {k: list(v) for k, v in drop.items() if isinstance(v, list)}




def _is_array(field: dict) -> bool:
    """Return True when a field declares ``array`` (``true`` or an integer)."""
    arr = field.get("array")
    return arr is not None and arr is not False




def _array_max_items(field: dict) -> int | None:
    """Return the array cap when ``array`` is an integer (not ``true``)."""
    arr = field.get("array")
    if isinstance(arr, bool):
        return None
    if isinstance(arr, int):
        return arr
    return None




def _scalar_node(field: dict, contract: dict) -> dict:
    """Build the leaf (string / integer) JSON-schema node for a field."""
    if field.get("type") == "int":
        node: dict = {"type": "integer"}
        if "min" in field:
            node["minimum"] = field["min"]
        if "max" in field:
            node["maximum"] = field["max"]
        return node
    node = {"type": "string"}
    if "enum" in field:
        node["enum"] = enum_values(contract, field["enum"])
    return node




def _subkey_node(spec: str, contract: dict) -> dict:
    """Build a JSON-schema node for one ``[fields.keys]`` sub-key.

    The value is a short string: ``"enum:NAME"`` → an enum string property,
    ``"list: <desc>"`` → an array-of-string property, anything else → a string
    property whose description is the text itself.
    """
    if isinstance(spec, str) and spec.startswith("enum:"):
        name = spec[len("enum:"):].strip()
        return {"type": "string", "enum": enum_values(contract, name)}
    if isinstance(spec, str) and spec.startswith("list:"):
        desc = spec[len("list:"):].strip()
        node: dict = {"type": "array", "items": {"type": "string"}}
        if desc:
            node["description"] = desc
        return node
    if isinstance(spec, str):
        m = _INT_SUBKEY_RE.match(spec)
        if m:
            int_node: dict = {"type": "integer"}
            if m.group(1) is not None:
                int_node["minimum"] = int(m.group(1))
                int_node["maximum"] = int(m.group(2))
            desc = m.group(3).strip()
            if desc:
                int_node["description"] = desc
            return int_node
    node = {"type": "string"}
    if isinstance(spec, str) and spec:
        node["description"] = spec
    return node




def _object_item_node(field: dict, contract: dict) -> dict:
    """Build the object node for a ``type = "object"`` field from ``keys``."""
    properties: dict = {}
    for key, spec in field.get("keys", {}).items():
        properties[key] = _subkey_node(spec, contract)
    return {
        "type": "object",
        "properties": properties,
        "required": list(field.get("keys", {}).keys()),
    }




def _build_node(field: dict, contract: dict) -> dict:
    """Build the full JSON-schema node for a field (clean-slate: always set
    a ``description`` from ``desc`` on the outermost node)."""
    if field.get("type") == "object":
        inner = _object_item_node(field, contract)
    else:
        inner = _scalar_node(field, contract)

    if _is_array(field):
        node: dict = {"type": "array", "items": inner}
        max_items = _array_max_items(field)
        if max_items is not None:
            node["maxItems"] = max_items
    else:
        node = inner

    if field.get("desc"):
        node["description"] = field["desc"]
    return node




def _infer_flatten(field: dict) -> str:
    """Infer the flatten rule from a field's structure."""
    if field.get("type") == "object":
        return "object_unpack"
    if _is_array(field):
        return "list_join"
    return "scalar"




def build_field_specs(contract: dict) -> list[tuple[str, dict, str]]:
    """Build the ``(gemini_field, json_schema_node, flatten_rule)`` list.

    Args:
        contract: the parsed contract.

    Returns:
        Field specs in contract (document) order.
    """
    specs: list[tuple[str, dict, str]] = []
    for field in contract.get("fields", []):
        specs.append((field["name"], _build_node(field, contract), _infer_flatten(field)))
    return specs




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
    fields = contract.get("fields", [])
    section_names = {s.get("name") for s in contract.get("section", [])}

    if not fields:
        errors.append("contract has no [[fields]]")
    if "prompt" not in contract or "header" not in contract.get("prompt", {}):
        errors.append("missing [prompt].header")

    for name, enum in enums.items():
        if not isinstance(enum, (list, dict)):
            errors.append(f"enum '{name}' must be a list or a value→description table")
        elif not enum:
            errors.append(f"enum '{name}' is empty")

    def _check_enum_ref(ref: str, where: str) -> None:
        target = ref[len("enum:"):].strip() if ref.startswith("enum:") else ref
        if target not in enums:
            errors.append(f"{where}: unknown enum ref '{target}'")

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

        if field.get("section") not in section_names:
            errors.append(f"{where}: section '{field.get('section')}' not in [[section]]")

        ftype = field.get("type", "string")
        if ftype not in VALID_TYPES:
            errors.append(f"{where}: invalid type '{ftype}'")

        arr = field.get("array")
        if arr is not None and not isinstance(arr, (bool, int)):
            errors.append(f"{where}: 'array' must be true or an integer")

        if "enum" in field:
            _check_enum_ref(field["enum"], where)

        if ftype == "object":
            keys = field.get("keys")
            if not isinstance(keys, dict) or not keys:
                errors.append(f"{where}: object field needs a non-empty [fields.keys]")
            else:
                for key, spec in keys.items():
                    if isinstance(spec, str) and spec.startswith("enum:"):
                        _check_enum_ref(spec, f"{where}.{key}")

    drop = contract.get("recode", {}).get("drop", {})
    if not isinstance(drop, dict):
        errors.append("[recode.drop] must be a table of column → list-of-words")
    else:
        for col, words in drop.items():
            if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
                errors.append(f"[recode.drop].{col}: must be a list of strings")

    return errors
