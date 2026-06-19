"""Generators for the Gemini annotation contract: response-schema builder,
structured-output flattener, and prompt renderer — all derived from one
declarative source, ``config/annotation_contract.toml`` (Workstream E).

A single ordered description of the Gemini output contract lives in the TOML
(loaded via ``fyp.annotation_contract``); ``FIELD_SPECS`` is built from it. From
``FIELD_SPECS`` this module derives:

  * ``build_response_schema()`` — a ``google.genai`` ``Schema`` that constrains
    decoding so the model always returns valid, conforming JSON.
  * ``get_annotation_json_schema()`` — the same contract as a portable JSON
    Schema dict (provider-agnostic; useful for docs and future adapters).
  * ``flatten_structured()`` — turns a structured response into a flat column
    shape (lists pipe-joined, objects exploded to ``<field>_<key>`` columns) for
    the recode pipeline.
  * ``build_prompt()`` — renders the text prompt (system instruction) from the
    same contract by pure deterministic templating (no LLM), so the prompt can
    never drift from the schema or the flattener.
"""

import google.genai.types as gt

from fyp import annotation_contract as _ac

# The declarative contract is loaded once at import; the prompt, response schema
# and flattener field specs all derive from it.
_CONTRACT = _ac.load_contract()

# Enum value lists (derived from the contract; kept as module-level aliases for
# readability and any back-compat reference).
YES_NO = _ac.enum_values(_CONTRACT, "yes_no")
GENDER_VALUES = _ac.enum_values(_CONTRACT, "gender")
ETHNICITY_VALUES = _ac.enum_values(_CONTRACT, "ethnicity")
TYPE_OF_STORY_VALUES = _ac.enum_values(_CONTRACT, "type_of_story")
CONTENT_CATEGORY_VALUES = _ac.enum_values(_CONTRACT, "content_category")

# Ordered field contract. Each entry: (gemini_field, json_schema_node, flatten_rule).
# Built from the TOML; order mirrors the prompt sections and drives propertyOrdering.
FIELD_SPECS: list[tuple[str, dict, str]] = _ac.build_field_specs(_CONTRACT)


def get_annotation_json_schema() -> dict:
    """Return the full annotation contract as a portable JSON Schema dict.

    Every field is required (the contract has no conditional fields).

    Returns:
        A JSON-schema ``object`` whose properties are the Gemini fields in
        ``FIELD_SPECS`` order, with a ``propertyOrdering`` hint.
    """
    properties: dict = {}
    for name, node, _rule in FIELD_SPECS:
        properties[name] = dict(node)
    ordering = [name for name, _node, _rule in FIELD_SPECS]
    return {
        "type": "object",
        "properties": properties,
        "required": ordering,
        "propertyOrdering": ordering,
    }


def _json_to_genai_schema(node: dict) -> gt.Schema:
    """Recursively convert a JSON-schema dict node into a ``genai`` ``Schema``.

    Handles object/array/string/integer/number/boolean nodes plus ``enum``,
    ``required``, ``maxItems``/``minItems`` and ``propertyOrdering``.

    Args:
        node: A JSON-schema node.

    Returns:
        The equivalent ``google.genai.types.Schema``.
    """
    json_type = node.get("type", "string")
    type_map = {
        "object": gt.Type.OBJECT,
        "array": gt.Type.ARRAY,
        "string": gt.Type.STRING,
        "integer": gt.Type.INTEGER,
        "number": gt.Type.NUMBER,
        "boolean": gt.Type.BOOLEAN,
    }
    schema = gt.Schema(type=type_map.get(json_type, gt.Type.STRING))

    if "description" in node:
        schema.description = node["description"]
    if "enum" in node:
        schema.enum = node["enum"]

    if json_type == "object":
        props = node.get("properties", {})
        schema.properties = {k: _json_to_genai_schema(v) for k, v in props.items()}
        ordering = node.get("propertyOrdering") or list(props.keys())
        schema.property_ordering = ordering
        if node.get("required"):
            schema.required = node["required"]
    elif json_type == "array":
        schema.items = _json_to_genai_schema(node["items"])
        if "maxItems" in node:
            schema.max_items = node["maxItems"]
        if "minItems" in node:
            schema.min_items = node["minItems"]

    return schema


def build_response_schema() -> gt.Schema:
    """Build the ``genai`` response schema for constrained decoding."""
    return _json_to_genai_schema(get_annotation_json_schema())


def _bullet_text(field: dict) -> str:
    """Render a field's prompt bullet: its ``desc`` plus an auto-rendered enum.

    A described enum is rendered as a numbered ``value - description`` list on a
    new line; a plain enum is appended inline as ``One of: "A", "B".``.
    """
    desc = field.get("desc") or field.get("prompt") or ""
    enum_name = field.get("enum")
    if not enum_name:
        return desc
    values = _ac.enum_values(_CONTRACT, enum_name)
    descriptions = _ac.enum_descriptions(_CONTRACT, enum_name)
    if descriptions:
        numbered = "\n".join(
            f"     {i}. {v} - {descriptions.get(v, '')}".rstrip(" -")
            for i, v in enumerate(values, 1)
        )
        return f"{desc}\n{numbered}" if desc else numbered
    quoted = ", ".join(f'"{v}"' for v in values)
    clause = f"One of: {quoted}."
    return f"{desc} {clause}" if desc else clause


def build_prompt() -> str:
    """Render the Gemini system-instruction prompt from the contract.

    Pure deterministic templating (no LLM): a global header, then each section's
    title + intro + per-field bullets (enums auto-rendered from the contract) +
    optional footer, then a global footer. Determinism keeps the prompt-text
    version hash stable.

    Returns:
        The full prompt text.
    """
    contract = _CONTRACT
    fields_by_section: dict[str, list[dict]] = {}
    for field in contract.get("fields", []):
        fields_by_section.setdefault(field.get("section"), []).append(field)

    lines: list[str] = [contract["prompt"]["header"], ""]
    for num, section in enumerate(_ac.sections(contract), 1):
        lines.append(f"{num}. **{section['title']}**")
        if section.get("intro"):
            lines.append(f"   {section['intro']}")
        for field in fields_by_section.get(section["name"], []):
            lines.append(f"   • '{field['name']}': {_bullet_text(field)}")
        if section.get("footer"):
            lines.append(f"   {section['footer']}")
        lines.append("")
    lines.append(contract["prompt"]["footer"])
    return "\n".join(lines)


def _join_pipe(values: list) -> str:
    """Join a list of stringifiable values with the legacy ``" | "`` separator."""
    return " | ".join(str(v) for v in values)


def flatten_structured(response: dict) -> dict:
    """Flatten a structured Gemini response to a flat column shape.

    Scalars pass through; arrays are pipe-joined; objects (or arrays of objects)
    are exploded so every sub-key becomes a ``<field>_<key>`` column (list values
    pipe-joined, and pipe-joined across array elements).

    Args:
        response: A response dict conforming to the annotation schema.

    Returns:
        A single-level dict of flattened columns. Missing fields are skipped.
    """
    flat: dict = {}
    for name, _node, rule in FIELD_SPECS:
        if name not in response or response[name] is None:
            continue
        value = response[name]

        if rule == "scalar":
            flat[name] = value

        elif rule == "list_join":
            if isinstance(value, list):
                flat[name] = _join_pipe(value)

        elif rule == "object_unpack":
            records = value if isinstance(value, list) else [value]
            buckets: dict[str, list[str]] = {}
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                for key, sub in rec.items():
                    col = f"{name}_{key}"
                    piece = _join_pipe(sub) if isinstance(sub, list) else str(sub)
                    buckets.setdefault(col, []).append(piece)
            for col, items in buckets.items():
                flat[col] = _join_pipe(items)

    # Defensive: collapse any lingering list to its first element (mirrors the
    # tail of the legacy flattener).
    for key, val in list(flat.items()):
        if isinstance(val, list):
            flat[key] = val[0] if val else ""

    return flat
