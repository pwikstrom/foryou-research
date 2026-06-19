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
  * ``flatten_structured()`` — turns a structured response into the *exact* flat
    column shape that ``machine_annotation.flatten_one_machine_response``
    produces today, so the existing recode pipeline (and the golden corpus) are
    reused unchanged.
  * ``build_prompt()`` — renders the text prompt (system instruction) from the
    same contract by pure deterministic templating (no LLM), so the prompt can
    never drift from the schema or the flattener.

Design note — scores: the legacy prompt asks for ``"0-100, one-sentence
rationale"`` as a free string that ``recode_scores`` later splits. Here the same
information is modelled as a structured ``{score:int, rationale:str}`` object
(guaranteed-parseable) and flattened back to the legacy ``"<score>, <rationale>"``
string, so ``recode_scores`` is unaffected.
"""

import collections

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
SCENE_SENTIMENT_VALUES = _ac.enum_values(_CONTRACT, "scene_sentiment")
CONTENT_CATEGORY_VALUES = _ac.enum_values(_CONTRACT, "content_category")
POLITICAL_POSITIONING_VALUES = _ac.enum_values(_CONTRACT, "political_positioning")

# Conditional-field design (derived from the contract): framing analysis applies
# only to Issue-Based / Event-Based stories; the Australian-political fields only
# when political_score > the threshold. Structured output cannot express these
# conditionals in a single schema, so the fields are made nullable (the model may
# omit them) and ``apply_conditional_rules`` enforces the condition deterministically
# afterwards — matching the free-text path.
_COND_GROUPS = _ac.conditional_field_groups(_CONTRACT)
FRAMING_FIELDS = _COND_GROUPS.get("issue_event", ())
AUSSIE_CONDITIONAL_FIELDS = _COND_GROUPS.get("political_gt_threshold", ())
CONDITIONAL_FIELDS = frozenset(FRAMING_FIELDS) | frozenset(AUSSIE_CONDITIONAL_FIELDS)
ISSUE_EVENT_VALUES = set(_CONTRACT["conditions"]["issue_event_values"])
POLITICAL_THRESHOLD = _CONTRACT["conditions"]["political_threshold"]

# Ordered field contract. Each entry: (gemini_field, json_schema_node, flatten_rule).
# Built from the TOML; order mirrors the prompt steps and drives propertyOrdering.
FIELD_SPECS: list[tuple[str, dict, str]] = _ac.build_field_specs(_CONTRACT)


def get_annotation_json_schema() -> dict:
    """Return the full annotation contract as a portable JSON Schema dict.

    Conditional fields (``CONDITIONAL_FIELDS``) are marked nullable and left out
    of ``required`` so the model may omit them; ``apply_conditional_rules``
    enforces the actual condition. All other fields are required.

    Returns:
        A JSON-schema ``object`` whose properties are the Gemini fields in
        ``FIELD_SPECS`` order, with a ``propertyOrdering`` hint.
    """
    properties: dict = {}
    for name, node, _rule in FIELD_SPECS:
        spec = dict(node)
        if name in CONDITIONAL_FIELDS:
            spec["nullable"] = True
        properties[name] = spec
    ordering = [name for name, _node, _rule in FIELD_SPECS]
    required = [name for name in ordering if name not in CONDITIONAL_FIELDS]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
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
    if node.get("nullable"):
        schema.nullable = True

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


def get_required_keys() -> list[str]:
    """Return the pre-flight ``REQUIRED_KEYS`` (must-be-present fields).

    These are the contract fields flagged ``required_key = true`` — the keys the
    refinement path expects to find in a raw response.

    Returns:
        Field names in contract order.
    """
    return [f["name"] for f in _CONTRACT.get("fields", []) if f.get("required_key")]


def build_prompt() -> str:
    """Render the Gemini system-instruction prompt from the contract.

    Pure deterministic templating (no LLM): a global header, then each step's
    title + intro + per-field bullets (``{enum}`` tokens rendered from the
    contract, ``prompt_override`` used verbatim when present) + optional footer,
    then a global footer. Determinism keeps the prompt-text version hash stable.

    Returns:
        The full prompt text.
    """
    contract = _CONTRACT
    steps = _ac.steps_by_number(contract)
    fields_by_step: dict[int, list[dict]] = {}
    for field in contract.get("fields", []):
        fields_by_step.setdefault(field["step"], []).append(field)

    lines: list[str] = [contract["prompt"]["header"], ""]
    for num in sorted(steps):
        step = steps[num]
        lines.append(f"{num}. **{step['title']}**")
        if step.get("intro"):
            lines.append(f"   {step['intro']}")
        for field in fields_by_step.get(num, []):
            text = field.get("prompt_override") or field.get("prompt") or field.get("description") or ""
            text = _ac.render_prompt_text(text, field, contract)
            lines.append(f"   • '{field['name']}': {text}")
        if step.get("footer"):
            lines.append(f"   {step['footer']}")
        lines.append("")
    lines.append(contract["prompt"]["footer"])
    return "\n".join(lines)


def _join_pipe(values: list) -> str:
    """Join a list of stringifiable values with the legacy ``" | "`` separator."""
    return " | ".join(str(v) for v in values)


def flatten_structured(response: dict) -> dict:
    """Flatten a structured Gemini response to the legacy flat column shape.

    Produces exactly the keys ``flatten_one_machine_response`` produces (nested
    containers unpacked, lists pipe-joined, scores recombined to the legacy
    ``"<score>, <rationale>"`` string), so the downstream recode pipeline is
    unchanged.

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

        elif rule == "transcript_join":
            texts = [
                seg.get("text", "") if isinstance(seg, dict) else seg
                for seg in value
                if isinstance(seg, (dict, str))
            ]
            flat[name] = _join_pipe(texts)

        elif rule == "scenes_join":
            descriptions = [s.get("description", "") for s in value if isinstance(s, dict)]
            sentiments = [s.get("sentiment", "") for s in value if isinstance(s, dict)]
            flat["scenes"] = _join_pipe(descriptions)
            top = collections.Counter(sentiments).most_common(1)
            flat["scene_sentiments"] = top[0][0] if top else ""

        elif rule == "faces_unpack":
            buckets: dict[str, list[str]] = {}
            for face in value:
                if not isinstance(face, dict):
                    continue
                for key, sub in face.items():
                    buckets.setdefault(f"faces_{key}", []).append(str(sub))
            for key, items in buckets.items():
                flat[key] = _join_pipe(items)

        elif rule == "audio_unpack":
            if isinstance(value, dict):
                for key, sub in value.items():
                    if isinstance(sub, list):
                        flat[key] = _join_pipe([s for s in sub if isinstance(s, str)])
                    else:
                        flat[key] = sub

        elif rule == "score_join":
            if isinstance(value, dict):
                score = value.get("score", "")
                rationale = value.get("rationale", "")
                flat[name] = f"{score}, {rationale}" if rationale != "" else f"{score}"
            else:
                flat[name] = value

    # Defensive: collapse any lingering list to its first element (mirrors the
    # tail of the legacy flattener).
    for key, val in list(flat.items()):
        if isinstance(val, list):
            flat[key] = val[0] if val else ""

    return flat


def apply_conditional_rules(flat: dict, response: dict) -> dict:
    """Enforce the study's conditional-field design on a flattened response.

    Framing-analysis fields apply only when ``type_of_story`` is Issue-Based or
    Event-Based; the Australian-political fields only when ``political_score`` is
    greater than ``POLITICAL_THRESHOLD``. When the condition is not met the
    fields are set to the legacy ``"-"`` sentinel. This makes structured output
    match the free-text path, where the same rule governs whether the model
    fills these fields at all.

    Args:
        flat: a flattened response dict (mutated in place and returned).
        response: the original structured response (for the numeric score).

    Returns:
        The same ``flat`` dict with conditional fields normalized.
    """
    type_of_story = flat.get("type_of_story")
    is_issue_event = isinstance(type_of_story, str) and type_of_story in ISSUE_EVENT_VALUES
    for field in FRAMING_FIELDS:
        if not is_issue_event:
            flat[field] = "-"
        else:
            flat.setdefault(field, "-")

    score = None
    political = response.get("political_score")
    if isinstance(political, dict):
        score = political.get("score")
    is_political = isinstance(score, (int, float)) and not isinstance(score, bool) and score > POLITICAL_THRESHOLD
    for field in AUSSIE_CONDITIONAL_FIELDS:
        if not is_political:
            flat[field] = "-"
        else:
            flat.setdefault(field, "-")

    return flat
