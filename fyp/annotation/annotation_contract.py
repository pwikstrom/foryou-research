"""Loader, validator, and field-spec builder for the declarative annotation
contract (``config/annotation_contract.toml``).

The contract is the single source from which the machine-annotation pipeline
generates the Gemini prompt, the structured-output ``response_schema``, and the
flattener field specs (see ``fyp.annotation_schema``). This module parses the
TOML, validates it, and turns each field into the
``(gemini_field, json_schema_node, flatten_rule)`` tuple the rest of the
pipeline consumes.

The per-field surface is intentionally small: everything except ``name`` is
optional, and the flatten rule + full JSON-schema node + the ``required`` set
are *inferred* from the field's ``type`` / ``enum`` / ``array`` / ``keys``
(``scale`` too, except for free-text strings — see :func:`infer_scale`).
Prompt ``[[section]]`` grouping is a legacy shape: contracts without sections
render a flat bullet list. The three flatten rules are:

  * ``scalar``        — a string / integer leaf.
  * ``list_join``     — an array of strings / enums (pipe-joined).
  * ``object_unpack`` — an object (or array of objects) whose sub-keys explode
    into ``<field>_<key>`` columns.
"""

import hashlib
import os
import re
import tomllib
from pathlib import Path

import fyp
from fyp.logging_setup import get_logger

logger = get_logger(__name__)

# NOTE: fyp.data_io is imported LAZILY inside functions (see _data_io()). A
# module-level import creates the same fyp_config import cycle documented in
# fyp/annotation_versioning.py — fyp_config's load-time overlays call
# load_contract(), so this module must not pull in data_io/fyp_config at import.

# A ``[fields.keys]`` sub-key declared as a bounded integer: ``"int(0,100): desc"``
# (or ``"int: desc"`` with no bounds). Lets an object sub-key be a clean number
# the generic numeric recode rescales — no per-field parser.
_INT_SUBKEY_RE = re.compile(r"^int\s*(?:\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\))?\s*:\s*(.*)$", re.DOTALL)

# The leaf/container types a field (or object sub-key) may declare.
VALID_TYPES = frozenset({"string", "int", "object"})

# Anchored on the fyp package (not this file) so the paths survive module
# moves within the package tree.
_DEFAULT_CONTRACT_PATH = (
    Path(fyp.__file__).resolve().parent.parent / "config" / "annotation_contract.toml"
)

# UI help texts for the form editor — the servable transcription of the baked
# contract's explanatory comments (see contract_help()).
_HELP_PATH = (
    Path(fyp.__file__).resolve().parent.parent / "config" / "annotation_contract_help.toml"
)

# The runtime, admin-editable copy of the contract lives in data storage
# (location ``users``, alongside var_presentation.json / admin_settings.json).
# When present and valid it supersedes the baked file above without a redeploy;
# absent or invalid, the baked file is the factory default. See
# :func:`refresh_runtime_contract`.
RUNTIME_LOCATION = "users"
RUNTIME_FILENAME = "annotation_contract.toml"
RUNTIME_META_FILENAME = "annotation_contract_meta.json"
BACKUP_PREFIX = "annotation_contract_backup_"

# Process-local snapshot of the effective contract. NEVER polled per call — it is
# refreshed only at explicit points (process boot, load_var_schema, Cloud Task
# entry via reload_var_schema_if_changed, and the upload/revert endpoints), which
# pins a whole annotation batch to one contract. See the plan's consistency rule.
_SNAPSHOT: dict = {"loaded": False}




def _data_io():
    """Lazy fyp.data_io accessor (breaks the fyp_config import cycle)."""
    import fyp.data_io as data_io

    return data_io




def _baked_only() -> bool:
    """Return True when ``FYP_BAKED_CONTRACTS_ONLY`` forces the baked contract.

    The golden safety-net runner sets this so a dev machine's local runtime
    storage can never contaminate the cost-free regression suite; it is also an
    emergency ops lever to ignore a bad runtime contract without touching storage.
    """
    return os.environ.get("FYP_BAKED_CONTRACTS_ONLY", "").strip().lower() in ("1", "true", "yes")




def default_contract_path() -> Path:
    """Return the repo-relative default path to the annotation contract."""
    return _DEFAULT_CONTRACT_PATH




def parse_and_validate(text: str) -> tuple[dict | None, list[str]]:
    """Parse TOML text and validate it as an annotation contract.

    Shared by the snapshot loader and the upload endpoint so both agree on what
    "valid" means.

    Args:
        text: The raw TOML text.

    Returns:
        ``(contract, errors)``. ``contract`` is ``None`` only when the text does
        not parse as TOML; on a parse success it is the parsed dict even if
        ``errors`` is non-empty. Callers must treat a non-empty ``errors`` list as
        a rejection.
    """
    try:
        contract = tomllib.loads(text)
    except Exception as e:
        return None, [f"TOML parse error: {e}"]
    return contract, validate_contract(contract)




def _etag(text: str, source: str) -> str:
    """Return a source-prefixed content etag (``runtime:``/``baked:`` + sha256)."""
    return f"{source}:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]




def _read_baked_text() -> str:
    """Read the baked contract file's raw text."""
    with open(_DEFAULT_CONTRACT_PATH, encoding="utf-8") as handle:
        return handle.read()




def _apply_baked_snapshot(error: str | None) -> None:
    """Load the baked contract into the snapshot (the factory-default state).

    A parse/validate error in the SHIPPED contract is recorded, not raised — the
    snapshot path must never crash boot (``load_contract(path)`` still raises for
    tests/tools). ``mtime`` is set by the caller.
    """
    text = _read_baked_text()
    contract, errors = parse_and_validate(text)
    _SNAPSHOT["contract"] = contract
    _SNAPSHOT["source"] = "baked"
    _SNAPSHOT["etag"] = _etag(text, "baked")
    _SNAPSHOT["error"] = error or ("; ".join(errors) if errors else None)




def refresh_runtime_contract() -> bool:
    """Refresh the process-local contract snapshot from data storage.

    Probes ``users/annotation_contract.toml`` via a single ``getmtime`` call; when
    its mtime differs from the last-seen value it reloads and validates. A valid
    runtime file becomes the effective contract; an absent/unreadable/invalid one
    degrades to the baked file (with a loud WARNING and the reason recorded in
    :func:`contract_status` for the admin card). Never raises.

    Returns:
        True when the effective contract's content changed (etag differs), so
        callers can bust dependent caches.
    """
    old_etag = _SNAPSHOT.get("etag")

    if _baked_only():
        _apply_baked_snapshot(error=None)
        _SNAPSHOT["mtime"] = None
        _SNAPSHOT["loaded"] = True
        return _SNAPSHOT.get("etag") != old_etag

    try:
        dio = _data_io()
        try:
            mtime = dio.getmtime(storage_location=RUNTIME_LOCATION, filename=RUNTIME_FILENAME)
        except FileNotFoundError:
            mtime = None

        # Nothing to do when we have already processed this exact file state
        # (same mtime, including the "absent" None state) — this is the cheap
        # steady-state path: one getmtime, no reparse.
        if _SNAPSHOT.get("loaded") and mtime == _SNAPSHOT.get("mtime"):
            return False

        if mtime is None:
            _apply_baked_snapshot(error=None)
            _SNAPSHOT["mtime"] = None
        else:
            text = dio.load_text(storage_location=RUNTIME_LOCATION, filename=RUNTIME_FILENAME)
            if text is None:
                logger.warning("WARNING: runtime annotation contract present but unreadable; using baked contract.")
                _apply_baked_snapshot(error="runtime contract unreadable")
                _SNAPSHOT["mtime"] = mtime
            else:
                contract, errors = parse_and_validate(text)
                if errors:
                    joined = "; ".join(errors)
                    logger.warning(f"WARNING: runtime annotation contract invalid ({joined}); using baked contract.")
                    _apply_baked_snapshot(error=joined)
                    _SNAPSHOT["mtime"] = mtime
                else:
                    _SNAPSHOT["contract"] = contract
                    _SNAPSHOT["source"] = "runtime"
                    _SNAPSHOT["etag"] = _etag(text, "runtime")
                    _SNAPSHOT["error"] = None
                    _SNAPSHOT["mtime"] = mtime
    except Exception as e:
        logger.warning(f"WARNING: runtime annotation contract probe failed ({e}); using baked contract.")
        try:
            _apply_baked_snapshot(error=f"runtime probe failed: {e}")
            _SNAPSHOT["mtime"] = None
        except Exception:
            pass

    _SNAPSHOT["loaded"] = True
    return _SNAPSHOT.get("etag") != old_etag




def _ensure_loaded() -> None:
    """Load the snapshot once, lazily, on first access."""
    if not _SNAPSHOT.get("loaded"):
        refresh_runtime_contract()




def contract_etag() -> str:
    """Return the effective contract's content etag (source-prefixed)."""
    _ensure_loaded()
    return _SNAPSHOT.get("etag") or "unknown"




def contract_status() -> dict:
    """Return the effective-contract status for the admin card / status API.

    ``{source, etag, mtime, error, updated_at, updated_by, original_filename}``.
    The audit fields come from ``annotation_contract_meta.json`` and are only
    populated for a runtime source. Never raises.
    """
    _ensure_loaded()
    meta: dict = {}
    if _SNAPSHOT.get("source") == "runtime" and not _baked_only():
        try:
            meta = _data_io().load_json(storage_location=RUNTIME_LOCATION, filename=RUNTIME_META_FILENAME) or {}
        except Exception:
            meta = {}
    return {
        "source": _SNAPSHOT.get("source"),
        "etag": _SNAPSHOT.get("etag"),
        "mtime": _SNAPSHOT.get("mtime"),
        "error": _SNAPSHOT.get("error"),
        "updated_at": meta.get("updated_at"),
        "updated_by": meta.get("updated_by"),
        "original_filename": meta.get("original_filename"),
    }




def effective_contract_text() -> str:
    """Return the raw TOML text of the effective contract (runtime or baked).

    Used by the download endpoint. Falls back to the baked text on any error.
    """
    _ensure_loaded()
    if _SNAPSHOT.get("source") == "runtime" and not _baked_only():
        try:
            text = _data_io().load_text(storage_location=RUNTIME_LOCATION, filename=RUNTIME_FILENAME)
            if text is not None:
                return text
        except Exception:
            pass
    return _read_baked_text()




def load_contract(path: str | Path | None = None) -> dict:
    """Load and validate the annotation contract.

    Args:
        path: When given, load and validate that exact TOML file (the historical
            behavior, raising on error — used by tests and offline tools). When
            ``None`` (the default and every pipeline call site), return the
            process-local effective contract snapshot (runtime file if present +
            valid, else the baked file), loaded lazily on first access and
            refreshed only at the explicit refresh points.

    Returns:
        The parsed contract dict.

    Raises:
        FileNotFoundError: if an explicit ``path`` does not exist.
        ValueError: if an explicit ``path`` fails validation.
    """
    if path is not None:
        contract_path = Path(path)
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

    _ensure_loaded()
    return _SNAPSHOT.get("contract")




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
                m = _INT_SUBKEY_RE.match(_subkey_spec(spec))
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
                if _INT_SUBKEY_RE.match(_subkey_spec(spec)):
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




def _subkey_spec(spec) -> str:
    """Return the schema-defining string for a ``[fields.keys]`` sub-key.

    A sub-key may be declared as a plain string (``"enum:gender"``) or as an
    inline table that carries var_schema metadata alongside the spec
    (``{ spec = "enum:gender", role = "skip", ... }``). Both forms resolve to the
    same spec string here, so every existing string parser (node builder, numeric
    range/array detectors, the ``enum:`` validator) works unchanged regardless of
    which form the contract uses.
    """
    if isinstance(spec, dict):
        return str(spec.get("spec", ""))
    return spec if isinstance(spec, str) else ""




def _subkey_metadata(spec) -> dict | None:
    """Return the var_schema metadata declared on a richer sub-key, or ``None``.

    Only the inline-table sub-key form carries ``role`` / ``scale`` /
    ``display_name`` / ``description``; a plain-string sub-key has none.
    """
    if isinstance(spec, dict):
        return {
            "role": spec.get("role"),
            "scale": spec.get("scale"),
            "display_name": spec.get("display_name"),
            "description": spec.get("description"),
        }
    return None




def _subkey_node(spec, contract: dict) -> dict:
    """Build a JSON-schema node for one ``[fields.keys]`` sub-key.

    The value is a short string (or the ``spec`` of an inline-table sub-key):
    ``"enum:NAME"`` → an enum string property, ``"list: <desc>"`` → an
    array-of-string property, anything else → a string property whose description
    is the text itself.
    """
    spec = _subkey_spec(spec)
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




def infer_scale(field: dict) -> str | None:
    """Infer a scalar/list field's var_schema ``scale`` from its schema shape.

    The schema shape determines the scale for every field except a free-text
    string, where ``categorical`` (short labels) vs ``text`` (long prose) is a
    recode-behavior choice the contract must make explicitly:

      * ``array`` (any list)      → ``list``
      * ``type = "int"``          → ``numeric``
      * ``enum`` (single value)   → ``categorical``
      * free-text string          → ``None`` (ambiguous — declare ``scale``)

    Object fields return ``None``; their sub-keys are inferred individually via
    :func:`infer_subkey_scale`.
    """
    if field.get("type") == "object":
        return None
    if _is_array(field):
        return "list"
    if field.get("type") == "int":
        return "numeric"
    if field.get("enum"):
        return "categorical"
    return None




def infer_subkey_scale(spec, parent_array: bool = False) -> str | None:
    """Infer a ``[fields.keys]`` sub-key's ``scale`` from its spec string.

    ``list:`` specs are lists; ``int`` specs are numeric (under an array parent
    the pipe-joined numbers collapse to a mean — see
    :func:`contract_numeric_array_fields`); any other sub-key under an
    ``array = true`` parent pipe-joins across elements, so it is a list; a
    single-object ``enum:`` sub-key is categorical. A free-text sub-key of a
    single object is ambiguous (``None``) — declare ``scale`` explicitly.

    Args:
        spec: the sub-key spec (string or inline-table form).
        parent_array: whether the owning object field declares ``array``.
    """
    spec_str = _subkey_spec(spec)
    if spec_str.startswith("list:"):
        return "list"
    if _INT_SUBKEY_RE.match(spec_str):
        return "numeric"
    if parent_array:
        return "list"
    if spec_str.startswith("enum:"):
        return "categorical"
    return None




def effective_scale(field: dict) -> str | None:
    """Return a field's declared ``scale``, or the inferred one when omitted."""
    return field.get("scale") or infer_scale(field)




def effective_subkey_scale(spec, parent_array: bool = False) -> str | None:
    """Return a sub-key's declared ``scale``, or the inferred one when omitted."""
    if isinstance(spec, dict) and spec.get("scale"):
        return str(spec["scale"])
    return infer_subkey_scale(spec, parent_array)




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




# A contract field whose flattened column is renamed downstream of the
# flattener. ``transcript`` is rebuilt as ``transcript_no_repetitions`` in
# ``machine_annotation.py`` (search that file for ``transcript_no_repetitions``).
_RENAMED_FIELDS = {"transcript": "transcript_no_repetitions"}

# Object fields whose ``<field>_<key>`` sub-key columns are prefix-stripped back
# to the bare sub-key name by ``recode_variables.rename_columns`` (the
# ``("audio_summary_", "")`` rule). Faces is NOT stripped, so it keeps the
# ``faces_`` prefix. Keep this in lockstep with that rename map.
_PREFIX_STRIPPED_OBJECTS = {"audio_summary"}




def contract_output_column(field_name: str, sub_key: str | None = None) -> str:
    """Return the final var_schema column name a contract field/sub-key becomes.

    Encodes the one-and-only rename/prefix-strip chain so every consumer agrees
    on the mapping (the flattener emits ``name`` / ``<name>_<key>``; then the
    transcript rename and the ``audio_summary_`` strip apply). This is the single
    bridge between a contract field and its ``var_schema.csv`` row.

    Args:
        field_name: the contract field ``name``.
        sub_key: the ``[fields.keys]`` sub-key, for object fields.

    Returns:
        The flattened/recoded column name as it appears in ``var_schema.csv``.
    """
    if sub_key is None:
        return _RENAMED_FIELDS.get(field_name, field_name)
    if field_name in _PREFIX_STRIPPED_OBJECTS:
        return sub_key
    return f"{field_name}_{sub_key}"




def contract_column_metadata(contract: dict) -> dict[str, dict]:
    """Return ``{final_column: {role, scale, display_name, description}}``.

    Covers every contract-owned output column that declares var_schema metadata —
    scalar/list fields (keyed by their flattened column) and object sub-keys
    (keyed via :func:`contract_output_column`). A field/sub-key that declares no
    var_schema metadata at all (no ``role`` / ``scale`` / ``display_name``) is
    skipped, so columns the contract does not own are never overlaid. A plain
    carried column declares ``scale`` / ``display_name`` but no ``role`` (blank
    role is the default), and is still overlaid. ``description`` falls back to the
    prompt ``desc`` only if no explicit ``description`` is set.

    Args:
        contract: the parsed contract dict.

    Returns:
        Mapping of var_schema column name → its contract-owned metadata.
    """
    out: dict[str, dict] = {}
    for field in contract.get("fields", []):
        name = field.get("name")
        if not name:
            continue
        if field.get("type") == "object":
            for key, spec in field.get("keys", {}).items():
                meta = _subkey_metadata(spec)
                if not meta or not (meta.get("role") or meta.get("scale") or meta.get("display_name")):
                    continue
                if not meta.get("scale"):
                    meta["scale"] = infer_subkey_scale(spec, parent_array=_is_array(field))
                if not meta.get("description"):
                    # Web-UI tooltip fallback: the spec's own description text
                    # (empty for enum:/bare int: specs), else the parent's desc.
                    spec_desc = parse_key_spec(_subkey_spec(spec)).get("desc")
                    meta["description"] = spec_desc or field.get("desc")
                out[contract_output_column(name, key)] = meta
        else:
            if not (field.get("role") or field.get("scale") or field.get("display_name")):
                continue
            out[contract_output_column(name)] = {
                "role": field.get("role"),
                "scale": effective_scale(field),
                "display_name": field.get("display_name"),
                "description": field.get("description", field.get("desc")),
            }
    return out




def validate_contract(contract: dict) -> list[str]:
    """Validate the annotation contract; return a list of error strings.

    Protects the schema the same way each contract's validator does: it lets a
    frequent editor catch mistakes before they reach a live Gemini call. An empty list means the contract is valid.

    Args:
        contract: the parsed contract dict.

    Returns:
        A list of human-readable validation errors (empty when valid).
    """
    errors: list[str] = []
    enums = contract.get("enums", {})
    fields = contract.get("fields", [])
    section_list = contract.get("section", [])
    section_names = {s.get("name") for s in section_list}

    # var_schema role/scale vocabularies live in recode_variables; import lazily so
    # this module never pulls in fyp_config (which recode_variables imports) at load.
    try:
        from fyp.recode_variables import LEGACY_ROLE_ALIASES, VAR_SCHEMA_ROLES, VAR_SCHEMA_SCALES
        # Legacy role strings stay valid: older uploaded runtime contracts /
        # registry snapshots still carry them (normalized at var_schema load).
        valid_roles = set(VAR_SCHEMA_ROLES) | set(LEGACY_ROLE_ALIASES)
        valid_scales = set(VAR_SCHEMA_SCALES)
    except Exception:
        valid_roles, valid_scales = None, None

    def _check_role_scale(meta: dict, where: str) -> None:
        role = meta.get("role")
        scale = meta.get("scale")
        if valid_roles is not None and role is not None and role not in valid_roles:
            errors.append(f"{where}: invalid role '{role}'")
        if valid_scales is not None and scale is not None and scale not in valid_scales:
            errors.append(f"{where}: invalid scale '{scale}'")

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

        # Sections are optional prompt structure (legacy contracts only): with
        # [[section]] entries every field must belong to one; without them the
        # key must be absent (a stale value would silently vanish from the prompt).
        if section_list:
            if field.get("section") not in section_names:
                errors.append(f"{where}: section '{field.get('section')}' not in [[section]]")
        elif "section" in field:
            errors.append(f"{where}: 'section' declared but the contract has no [[section]] entries")

        ftype = field.get("type", "string")
        if ftype not in VALID_TYPES:
            errors.append(f"{where}: invalid type '{ftype}'")

        arr = field.get("array")
        if arr is not None and not isinstance(arr, (bool, int)):
            errors.append(f"{where}: 'array' must be true or an integer")

        if "enum" in field:
            _check_enum_ref(field["enum"], where)

        # A free-text string is the one shape whose scale cannot be inferred:
        # 'categorical' (short labels) vs 'text' (long prose) picks the recode
        # function, so the contract must decide explicitly.
        if (
            ftype == "string"
            and not _is_array(field)
            and "enum" not in field
            and not field.get("scale")
        ):
            errors.append(
                f"{where}: free-text field needs an explicit scale — "
                "'categorical' (short labels) or 'text' (long prose)"
            )

        _check_role_scale(field, where)

        if ftype == "object":
            keys = field.get("keys")
            if not isinstance(keys, dict) or not keys:
                errors.append(f"{where}: object field needs a non-empty [fields.keys]")
            else:
                for key, spec in keys.items():
                    spec_str = _subkey_spec(spec)
                    if spec_str.startswith("enum:"):
                        _check_enum_ref(spec_str, f"{where}.{key}")
                    if isinstance(spec, dict):
                        _check_role_scale(spec, f"{where}.{key}")

    drop = contract.get("recode", {}).get("drop", {})
    if not isinstance(drop, dict):
        errors.append("[recode.drop] must be a table of column → list-of-words")
    else:
        for col, words in drop.items():
            if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
                errors.append(f"[recode.drop].{col}: must be a list of strings")

    return errors




# ---------------------------------------------------------------------------
# Form-editor support: help texts, sub-key spec-string round-tripping, and
# dict → TOML serialization (tomlkit).
# ---------------------------------------------------------------------------


def _tomlkit():
    """Lazy tomlkit accessor.

    tomlkit is only needed by the form-editor serialization path; importing it
    lazily keeps app boot resilient when the dependency is missing (e.g. an app
    image deployed before the base image was rebuilt with the new requirement).
    """
    import tomlkit

    return tomlkit




def contract_help() -> dict[str, str]:
    """Return the form-editor help texts keyed by dotted contract path.

    Loads ``config/annotation_contract_help.toml`` — the UI-servable
    transcription of the baked contract's explanatory comments. Keys are
    dotted paths (``"fields.array"``, ``"prompt.header"``, panel-level
    ``"enums"``, plus ``"overview"``). Never raises; returns ``{}`` on any
    read/parse failure.
    """
    try:
        with open(_HELP_PATH, "rb") as handle:
            return {str(k): str(v) for k, v in tomllib.load(handle).get("help", {}).items()}
    except Exception:
        return {}




def parse_key_spec(spec: str) -> dict:
    """Decompose a ``[fields.keys]`` spec string into its structured parts.

    Mirrors :func:`_subkey_node`'s parsing exactly so the form editor and the
    schema builder always agree. The four forms:

      * ``"enum:NAME"``        → ``{"kind": "enum", "enum": NAME, "desc": ""}``
      * ``"list: <desc>"``     → ``{"kind": "list", "desc": <desc>}``
      * ``"int(lo,hi): <d>"`` / ``"int: <d>"``
                               → ``{"kind": "int", "desc": <d>[, "min", "max"]}``
      * anything else          → ``{"kind": "text", "desc": <spec>}``

    Args:
        spec: the compact spec string (NOT the inline-table sub-key form —
            pass ``spec["spec"]`` for those).

    Returns:
        The structured parts; :func:`format_key_spec` is the inverse.
    """
    spec = spec if isinstance(spec, str) else ""
    if spec.startswith("enum:"):
        return {"kind": "enum", "enum": spec[len("enum:"):].strip(), "desc": ""}
    if spec.startswith("list:"):
        return {"kind": "list", "desc": spec[len("list:"):].strip()}
    m = _INT_SUBKEY_RE.match(spec)
    if m:
        parts: dict = {"kind": "int", "desc": m.group(3).strip()}
        if m.group(1) is not None:
            parts["min"] = int(m.group(1))
            parts["max"] = int(m.group(2))
        return parts
    return {"kind": "text", "desc": spec}




def format_key_spec(parts: dict) -> str:
    """Format structured sub-key parts back into the compact spec string.

    Inverse of :func:`parse_key_spec` (round-trips every spec form in the
    baked contract).

    Args:
        parts: ``{"kind", ...}`` as returned by :func:`parse_key_spec`.

    Returns:
        The compact spec string.
    """
    kind = parts.get("kind", "text")
    desc = str(parts.get("desc") or "").strip()
    if kind == "enum":
        return f"enum:{str(parts.get('enum') or '').strip()}"
    if kind == "list":
        return f"list: {desc}" if desc else "list:"
    if kind == "int":
        bounds = ""
        if parts.get("min") is not None and parts.get("max") is not None:
            bounds = f"({int(parts['min'])},{int(parts['max'])})"
        return f"int{bounds}: {desc}" if desc else f"int{bounds}:"
    return desc




def _plain(value):
    """Return a tomlkit item as its plain-Python equivalent (or as-is)."""
    unwrap = getattr(value, "unwrap", None)
    return unwrap() if callable(unwrap) else value




def _keys_table(keys: dict, tk):
    """Build the ``[fields.keys]`` sub-table (dict sub-keys → inline tables)."""
    table = tk.table()
    for sub_key, spec in keys.items():
        if isinstance(spec, dict):
            inline = tk.inline_table()
            for k, v in spec.items():
                inline[k] = v
            table[sub_key] = inline
        else:
            table[sub_key] = spec
    return table




def _build_item(key: str, value, tk):
    """Build the tomlkit item for one contract key (dispatch on shape).

    ``keys`` dicts render as a sub-table of inline tables (the baked file's
    shape); every other dict renders as a regular sub-table; lists/scalars
    pass through (tomlkit converts natively).
    """
    if key == "keys" and isinstance(value, dict):
        return _keys_table(value, tk)
    if isinstance(value, dict):
        table = tk.table()
        for k, v in value.items():
            table[k] = _build_item(k, v, tk)
        return table
    return value




def _table_from(entry: dict, tk):
    """Build a tomlkit table for one ``[[section]]`` / ``[[fields]]`` entry."""
    table = tk.table()
    for k, v in entry.items():
        table[k] = _build_item(k, v, tk)
    return table




def _update_table_in_place(table, new: dict, tk) -> None:
    """Mutate a tomlkit table to match ``new``, touching only changed keys.

    Comments attached to untouched keys survive verbatim; a changed key is
    reassigned (its own trailing comment is lost, nothing else).
    """
    for key in list(table.keys()):
        if key not in new:
            del table[key]
    for key, value in new.items():
        if key not in table or _plain(table[key]) != value:
            table[key] = _build_item(key, value, tk)




def _sync_table(doc, name: str, new: dict | None, tk) -> None:
    """Sync one top-level table (``prompt`` / ``enums`` / ``recode``) in place."""
    if not new:
        if name in doc:
            del doc[name]
        return
    if name not in doc:
        doc[name] = tk.table()
    _update_table_in_place(doc[name], new, tk)




def _sync_aot(doc, name: str, new_list: list, tk) -> None:
    """Sync a top-level array-of-tables (``section`` / ``fields``) in place.

    When the entry names match pairwise (the common edit-in-place case) each
    table is mutated key-by-key, preserving all surrounding comments. On an
    add/remove/reorder the array is rebuilt, reusing unchanged tables (their
    internal comments survive; free-floating comments between tables may not).
    """
    if not new_list:
        if name in doc:
            del doc[name]
        return
    existing = doc.get(name)
    existing_tables = list(existing) if existing is not None else []
    base_names = [_plain(t).get("name") for t in existing_tables]
    new_names = [entry.get("name") for entry in new_list]

    if base_names == new_names:
        for table, entry in zip(existing_tables, new_list):
            if _plain(table) != entry:
                _update_table_in_place(table, entry, tk)
        return

    by_name = {n: t for n, t in zip(base_names, existing_tables)}
    aot = tk.aot()
    for entry in new_list:
        table = by_name.get(entry.get("name"))
        if table is not None and _plain(table) == entry:
            aot.append(table)
        else:
            aot.append(_table_from(entry, tk))
    doc[name] = aot




# Canonical top-level order for a regenerated contract file.
_TOP_LEVEL_ORDER = ("prompt", "recode", "enums", "section", "fields")

# Short header comments injected above each part of a regenerated file (the
# full documentation lives in config/annotation_contract_help.toml).
_FRESH_COMMENTS = {
    "prompt": "Fixed prompt text around the generated field bullets.",
    "recode": "Recode hints: field-specific stop words, keyed by flattened output column.",
    "enums": "Named closed value sets (bare list, or value -> description table).",
    "section": "Prompt sections (order = document order).",
    "fields": "The Gemini output fields, in prompt/schema order.",
}




def _serialize_fresh(contract: dict) -> str:
    """Regenerate a contract file from scratch (no base text to round-trip).

    Injects short canonical header comments so even a regenerated file carries
    orientation pointers; the full guidance lives in the help file.
    """
    tk = _tomlkit()
    doc = tk.document()
    doc.add(tk.comment("Declarative annotation contract - generated by the form editor."))
    doc.add(tk.comment("What each key does: config/annotation_contract_help.toml"))
    doc.add(tk.comment("(and the annotated baked default: config/annotation_contract.toml)"))

    ordered = [k for k in _TOP_LEVEL_ORDER if k in contract]
    ordered += [k for k in contract if k not in _TOP_LEVEL_ORDER]
    for key in ordered:
        value = contract[key]
        doc.add(tk.nl())
        if key in _FRESH_COMMENTS:
            doc.add(tk.comment(_FRESH_COMMENTS[key]))
        if isinstance(value, list):
            aot = tk.aot()
            for entry in value:
                aot.append(_table_from(entry, tk))
            doc[key] = aot
        else:
            doc[key] = _build_item(key, value, tk)
    return tk.dumps(doc)




def serialize_contract(contract: dict, base_text: str | None = None) -> str:
    """Serialize a contract dict to TOML text (the form editor's save path).

    With ``base_text`` (normally the current effective contract's TOML) the
    serialization round-trips: the base document is parsed with tomlkit and
    mutated in place, so comments on untouched keys/tables survive verbatim
    — an unchanged contract returns ``base_text`` byte-identical. Without a
    base (or when round-tripping fails), the file is regenerated from scratch
    with canonical header comments.

    The output is verified to re-parse to exactly ``contract`` before it is
    returned.

    Args:
        contract: the plain contract dict (the parsed-TOML shape).
        base_text: the TOML text to round-trip against, if any.

    Returns:
        TOML text that parses back to ``contract``.

    Raises:
        ValueError: if no serialization strategy reproduces ``contract``
            exactly (e.g. a value TOML cannot represent, such as ``None``).
    """
    candidates: list[str] = []
    if base_text is not None:
        try:
            if tomllib.loads(base_text) == contract:
                return base_text
            tk = _tomlkit()
            doc = tk.parse(base_text)
            for name in ("prompt", "recode", "enums"):
                _sync_table(doc, name, contract.get(name), tk)
            for name in ("section", "fields"):
                _sync_aot(doc, name, contract.get(name, []), tk)
            known = set(_TOP_LEVEL_ORDER)
            for key in list(doc.keys()):
                if key not in known and key not in contract:
                    del doc[key]
            for key, value in contract.items():
                if key not in known and (key not in doc or _plain(doc[key]) != value):
                    doc[key] = _build_item(key, value, tk)
            candidates.append(tk.dumps(doc))
        except Exception:
            pass

    try:
        candidates.append(_serialize_fresh(contract))
    except Exception:
        pass

    for text in candidates:
        try:
            if tomllib.loads(text) == contract:
                return text
        except Exception:
            continue
    raise ValueError(
        "contract serialization failed: no strategy reproduced the contract "
        "exactly (does it contain values TOML cannot represent, e.g. null?)"
    )
