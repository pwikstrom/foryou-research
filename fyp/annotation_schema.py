"""Declarative annotation-field spec, Gemini response-schema builder, and a
structured-output flattener for the machine-annotation pipeline.

This module is the Phase 2 (structured output) groundwork and the seed of the
Phase 3 inversion (generating the prompt + processing from a schema). It holds a
single ordered description of the Gemini output contract and derives from it:

  * ``build_response_schema()`` — a ``google.genai`` ``Schema`` that constrains
    decoding so the model always returns valid, conforming JSON.
  * ``get_annotation_json_schema()`` — the same contract as a portable JSON
    Schema dict (provider-agnostic; useful for docs and future adapters).
  * ``flatten_structured()`` — turns a structured response into the *exact* flat
    column shape that ``machine_annotation.flatten_one_machine_response``
    produces today, so the existing recode pipeline (and the golden corpus) are
    reused unchanged. The only variable an A/B test then measures is free-text
    vs structured generation — not different downstream processing.

Design note — scores: the legacy prompt asks for ``"0-100, one-sentence
rationale"`` as a free string that ``recode_scores`` later splits. Here the same
information is modelled as a structured ``{score:int, rationale:str}`` object
(guaranteed-parseable) and flattened back to the legacy ``"<score>, <rationale>"``
string, so ``recode_scores`` is unaffected.
"""

import collections

import google.genai.types as gt

YES_NO = ["Yes", "No"]

GENDER_VALUES = ["Female", "Male", "Nonbinary", "Multiple", "-"]

ETHNICITY_VALUES = [
    "Indigenous Australian",
    "Caucasian",
    "Middle Eastern",
    "South Asian",
    "Northeast Asian",
    "Southeast Asian",
    "African",
    "Native American",
    "Multiple",
    "-",
]

TYPE_OF_STORY_VALUES = ["Issue-Based", "Event-Based", "Human-Interest", "Descriptive"]

SCENE_SENTIMENT_VALUES = [
    "Positive High-Energy",
    "Positive Low-Energy",
    "Negative High-Energy",
    "Negative Low-Energy",
]

CONTENT_CATEGORY_VALUES = [
    "Performance",
    "Comedy",
    "Film & TV",
    "Anime & Comics",
    "Games",
    "Drama",
    "Art & Creativity",
    "Sports",
    "Daily Life",
    "Fashion & Beauty",
    "Interpersonal Relationships",
    "Food",
    "Animals",
    "Fitness & Physical Health",
    "DIY & Life Hacks",
    "Travel",
    "Mental Health & Wellbeing",
    "News",
    "Education",
    "Technology & Design & Reviews",
    "Finance",
    "Society",
]

POLITICAL_POSITIONING_VALUES = [
    "Pro Coalition/LNP/Nationals",
    "Anti Coalition/LNP/Nationals",
    "Pro Labor",
    "Anti Labor",
    "Pro Greens",
    "Anti Greens",
    "Pro Independents/small parties",
    "No clear position",
    "-",
]

# Conditional fields, per the study design encoded in the prompt:
# framing analysis applies only to Issue-Based / Event-Based stories; the
# Australian-political fields only when political_score > 40. Structured output
# cannot express these conditionals in a single schema, so the fields are made
# nullable (the model may omit them) and ``apply_conditional_rules`` enforces
# the condition deterministically afterwards — matching the free-text path,
# whose coverage of these fields is governed by the same rule.
ISSUE_EVENT_VALUES = {"Issue-Based", "Event-Based"}
POLITICAL_THRESHOLD = 40
FRAMING_FIELDS = (
    "framing_analysis_problem_definition",
    "framing_analysis_attribution_of_responsibility",
    "framing_analysis_moral_evaluation",
    "framing_analysis_treatment_recommendation",
)
AUSSIE_CONDITIONAL_FIELDS = (
    "aussie_political_message",
    "aussie_political_positioning",
)
CONDITIONAL_FIELDS = frozenset(FRAMING_FIELDS) | frozenset(AUSSIE_CONDITIONAL_FIELDS)


def _obj(properties: dict, required: list[str]) -> dict:
    """Build a JSON-schema object node with ordered properties.

    Args:
        properties: Ordered mapping of property name to its JSON-schema node.
        required: Property names that must be present.

    Returns:
        A JSON-schema ``object`` node.
    """
    return {"type": "object", "properties": properties, "required": required}


def _str(description: str = "", enum: list[str] | None = None) -> dict:
    """Build a JSON-schema string node, optionally enum-constrained."""
    node: dict = {"type": "string"}
    if description:
        node["description"] = description
    if enum is not None:
        node["enum"] = enum
    return node


def _list_of(item: dict, description: str = "", max_items: int | None = None) -> dict:
    """Build a JSON-schema array node."""
    node: dict = {"type": "array", "items": item}
    if description:
        node["description"] = description
    if max_items is not None:
        node["maxItems"] = max_items
    return node


# Ordered field contract. Each entry: (gemini_field, json_schema_node, flatten_rule).
# Order mirrors the six prompt steps and drives propertyOrdering.
_SCORE_OBJ = _obj(
    {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "rationale": _str("one-sentence rationale"),
    },
    ["score", "rationale"],
)

FIELD_SPECS: list[tuple[str, dict, str]] = [
    # --- Step 1: video profile ---
    (
        "transcript",
        _list_of(_obj({"speaker": _str(), "text": _str()}, ["text"])),
        "transcript_join",
    ),
    (
        "scenes",
        _list_of(
            _obj(
                {
                    "scene_index": {"type": "integer"},
                    "description": _str(),
                    "sentiment": _str(enum=SCENE_SENTIMENT_VALUES),
                },
                ["description", "sentiment"],
            )
        ),
        "scenes_join",
    ),
    ("objects", _list_of(_str()), "list_join"),
    ("symbols_and_brands", _list_of(_str()), "list_join"),
    ("text_overlays", _list_of(_str()), "list_join"),
    (
        "faces",
        _list_of(
            _obj(
                {
                    "gender": _str(enum=GENDER_VALUES),
                    "age_estimate": _str("age range like 20-30"),
                    "ethnicity": _str(),
                },
                ["gender", "age_estimate", "ethnicity"],
            )
        ),
        "faces_unpack",
    ),
    (
        "audio_summary",
        _obj(
            {
                "speech_vs_music": _str("e.g. '60% speech, 40% music'"),
                "background_music": _str(),
                "notable_sounds": _list_of(_str()),
            },
            ["speech_vs_music", "background_music", "notable_sounds"],
        ),
        "audio_unpack",
    ),
    ("main_activity", _str("verb + object, e.g. 'dancing in a studio'"), "scalar"),
    ("video_story", _str("concise narrative summary"), "scalar"),
    ("type_of_story", _str(enum=TYPE_OF_STORY_VALUES), "scalar"),
    (
        "content_category",
        _list_of(_str(enum=CONTENT_CATEGORY_VALUES), max_items=2),
        "list_join",
    ),
    ("australian_relevance", _str(enum=YES_NO), "scalar"),
    ("tiktok_native", _str(enum=YES_NO), "scalar"),
    ("trend", _str(enum=YES_NO), "scalar"),
    ("advertising", _str(enum=YES_NO), "scalar"),
    ("aigc", _str(enum=YES_NO), "scalar"),
    ("main_gender", _str(enum=GENDER_VALUES), "scalar"),
    ("main_ethnicity", _str(enum=ETHNICITY_VALUES), "scalar"),
    # --- Step 2: persuasion & scoring ---
    ("political_score", _SCORE_OBJ, "score_join"),
    ("sensitivity_score", _SCORE_OBJ, "score_join"),
    ("call_to_action", _str("action encouraged, or '-'"), "scalar"),
    # --- Step 3: Australian political context (conditional; '-' when N/A) ---
    ("aussie_political_message", _str("summary or '-'"), "scalar"),
    ("aussie_political_positioning", _str(enum=POLITICAL_POSITIONING_VALUES), "scalar"),
    # --- Step 4: framing analysis (conditional; '-' when N/A) ---
    ("framing_analysis_problem_definition", _str(), "scalar"),
    ("framing_analysis_attribution_of_responsibility", _str(), "scalar"),
    ("framing_analysis_moral_evaluation", _str(), "scalar"),
    ("framing_analysis_treatment_recommendation", _str(), "scalar"),
    # --- Step 5: cultural representation ---
    ("cultural_representation_analysis_key_groups", _str(), "scalar"),
    ("cultural_representation_analysis_complexity_vs_stereotypes", _str(), "scalar"),
    ("cultural_representation_analysis_symbolism_and_imagery", _str(), "scalar"),
    ("cultural_representation_analysis_inclusion_and_exclusion", _str(), "scalar"),
    # --- Step 6: ideological power analysis ---
    ("ideological_analysis_dominant_ideologies", _str(), "scalar"),
    ("ideological_analysis_power_dynamics", _str(), "scalar"),
    ("ideological_analysis_critique_or_reinforcement", _str(), "scalar"),
    ("ideological_analysis_cultural_or_historical_context", _str(), "scalar"),
]


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
