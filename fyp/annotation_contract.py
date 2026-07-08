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

import hashlib
import os
import re
import tomllib
from pathlib import Path

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

_DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "annotation_contract.toml"
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
                print("WARNING: runtime annotation contract present but unreadable; using baked contract.")
                _apply_baked_snapshot(error="runtime contract unreadable")
                _SNAPSHOT["mtime"] = mtime
            else:
                contract, errors = parse_and_validate(text)
                if errors:
                    joined = "; ".join(errors)
                    print(f"WARNING: runtime annotation contract invalid ({joined}); using baked contract.")
                    _apply_baked_snapshot(error=joined)
                    _SNAPSHOT["mtime"] = mtime
                else:
                    _SNAPSHOT["contract"] = contract
                    _SNAPSHOT["source"] = "runtime"
                    _SNAPSHOT["etag"] = _etag(text, "runtime")
                    _SNAPSHOT["error"] = None
                    _SNAPSHOT["mtime"] = mtime
    except Exception as e:
        print(f"WARNING: runtime annotation contract probe failed ({e}); using baked contract.")
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
                meta.setdefault("source", "Gemini")
                out[contract_output_column(name, key)] = meta
        else:
            if not (field.get("role") or field.get("scale") or field.get("display_name")):
                continue
            out[contract_output_column(name)] = {
                "role": field.get("role"),
                "scale": field.get("scale"),
                "display_name": field.get("display_name"),
                "description": field.get("description", field.get("desc")),
                # Every flattened annotation output column is Gemini-produced.
                "source": field.get("source") or "Gemini",
            }
    return out




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

    # var_schema role/scale vocabularies live in recode_variables; import lazily so
    # this module never pulls in fyp_config (which recode_variables imports) at load.
    try:
        from fyp.recode_variables import VAR_SCHEMA_ROLES, VAR_SCHEMA_SCALES
        valid_roles, valid_scales = set(VAR_SCHEMA_ROLES), set(VAR_SCHEMA_SCALES)
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
