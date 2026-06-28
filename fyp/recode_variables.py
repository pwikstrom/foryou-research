


import difflib
import hashlib
import json
import logging
import re
import traceback
from copy import copy
from datetime import datetime

import numpy as np
import pandas as pd

from fyp.fyp_config import fyp_cf
from fyp.types import convert_dtypes_to_pyarrow

logger = logging.getLogger(__name__)

WEEKDAY_MAPPER = { 1:"monday", 2:"tuesday",3:"wednesday",4:"thursday",5:"friday",6:"saturday",7:"sunday"}
GENERIC_MAPPER = fyp_cf["labels"]["GENERIC_MAPPER"]
IRRELEVANT_WORDS = fyp_cf["labels"]["IRRELEVANT_WORDS"]

NOT_CODED =  fyp_cf["labels"]["NOT_CODED"]
UNABLE_TO_DETECT = fyp_cf["labels"]["UNABLE_TO_DETECT"]
OTHER_THINGS = fyp_cf["labels"]["OTHER_THINGS"]
SPLITTER = fyp_cf["labels"]["SPLITTER"]





def rename_columns(some_events):
    """
    This function is indempotent
    """
    some_eventsC = some_events.copy()

    fixer_upper = [
        #("B_local_","local_"),
        #("B_source_tz_name",tz_name"),
        #("D_local_","local_"),
        (".","_"),
        ("data_",""),
        ("source_url_","source_"),
        ("_collected",""),
        ("framing_analysis_","FA_"),
        ("cultural_representation_analysis_","CRA_"),
        ("ideological_analysis_","IA_"),
        # The object_unpack flattener prefixes audio sub-keys with the field
        # name (audio_summary_speech_vs_music, ...); strip it back to the
        # var_schema variable names (speech_vs_music, ...). The legacy free-text
        # flattener already emits the bare names, so this is a no-op for it.
        ("audio_summary_",""),

        ]

    pd.set_option('future.no_silent_downcasting', True)

    for fu in fixer_upper:
        mapper = {c:c.replace(fu[0],fu[1]) for c in some_eventsC.columns if (c != c.replace(fu[0],fu[1])) and (c.replace(fu[0],fu[1]) not in some_eventsC.columns)}
        some_eventsC = some_eventsC.rename(columns=mapper).copy()
    
    return some_eventsC




def infer_timezone_offset(timestamps: pd.Series) -> float:
    """
    Infers timezone offset by finding the 4-hour window with minimum activity.
    Assumes this quietest window centers around 04:00 local time.
    
    Args:
        timestamps: Series of UTC timestamps
        
    Returns:
        Offset in hours (float) from UTC. e.g. +10.0 for Brisbane.
    """
    if len(timestamps) < 10:
        return 0.0 # Not enough data to infer
        
    # Create a DataFrame to aggregate by hour
    df_ts = pd.DataFrame({'ts': timestamps})
    df_ts['hour'] = df_ts['ts'].dt.hour
    
    # Count activity per UTC hour (0-23)
    hourly_counts = df_ts.groupby('hour').size().reindex(range(24), fill_value=0)
    
    # We want a rolling 4-hour window sum. 
    # To handle wrap-around (e.g. 23:00 -> 02:00), we concat the counts 
    hourly_counts_ext = pd.concat([hourly_counts, hourly_counts.iloc[:3]], ignore_index=True)
    
    # Calculate rolling sum
    rolling_sum = hourly_counts_ext.rolling(window=4).sum()
    
    # We strip the first 3 (NaNs/partial from standard rolling if not min_periods=0) 
    # but we used concat so we have valid range. 
    # The result has length 24 + 3 = 27.
    # Indices 0,1,2 are NaNs (window size 4).
    # Valid indices start at 3.
    # Index 3 corresponds to window [0,1,2,3] of extended array = [0,1,2,3] of original.
    # Index 26 corresponds to window [23,0,1,2].
    
    # Extract only the 24 valid windows representing starts 0..23 (wrapped)
    # Window ending at i (where i >= 3) corresponds to hours ...?
    # Let's map rolling_sum index to "Center Hour".
    # We want indices 3 to 26 inclusive (24 values).
    valid_sums = rolling_sum.iloc[3:].reset_index(drop=True)
    # valid_sums now has indices 0 to 23.
    # Index k in valid_sums came from rolling_sum index k+3.
    # rolling_sum index k+3 sums extended array [k, k+1, k+2, k+3].
    # Which corresponds to hours [k%24, (k+1)%24, (k+2)%24, (k+3)%24].
    # Center is roughly k + 1.5.
    
    min_val = valid_sums.min()
    min_indices = valid_sums[valid_sums == min_val].index.tolist()
    
    # Calculate circular mean of these indices
    # Convert hours (indices) to angles, mean vector, convert back
    angles = [2 * np.pi * idx / 24.0 for idx in min_indices]
    y = np.sum(np.sin(angles))
    x = np.sum(np.cos(angles))
    avg_angle = np.arctan2(y, x)
    avg_idx = avg_angle * 24.0 / (2 * np.pi)
    
    if avg_idx < 0:
        avg_idx += 24
        
    # avg_idx represents the "Start Hour" of the window (k).
    # Center of window is k + 2.0 (Midpoint of 4 discrete hour buckets [k, k+3]).
    # e.g. Window [2,3,4,5] -> Center is 4.0.
    # We assume this center is 03:00 Local (Shifted -1 from original 04:00).
    
    center_utc = avg_idx + 2.0
    if center_utc >= 24:
        center_utc -= 24
        
    # Offset = Local - UTC = 3.0 - Center (Shifted -1 from 4.0)
    offset = 3.0 - center_utc
    
    # Normalize to -9 to 15 (User specified range to handle date line wrap)
    # "Add 24 hours to timezones calculated to UTC-11" -> Map -11 to +13.
    # Standard range [-9, 15] covers West Coast US (-8) to NZ (+12/13).
    while offset < -9:
        offset += 24
    while offset > 15:
        offset -= 24
        
    return round(offset) # Round to nearest hour for simplicity (or keeping half hours?)
                         # User said rough guess. 
                         



SEMANTIC_COLUMNS = (
    "role",
    "scale",
    "accepted_labels",
    "allow_scalar_fallback",
)

VAR_SCHEMA_HASH_VERSION = "v2"


VAR_SCHEMA_ROLES = ("factor", "group_factor", "feature", "standard", "skip", "raw")
VAR_SCHEMA_SCALES = (
    "categorical",
    "collection",
    "datetime",
    "dichotomous",
    "factor",
    "interval",
    "ordinal",
    "ratio",
    "raw",
    "string",
)



_RECODE_FUNC_REGISTRY: dict | None = None


def get_recode_func_registry() -> dict:
    """Return the allow-list of callables that ``recode_func`` may name.

    Hard-coded rather than introspected so an audit of this dict is the
    only place a reviewer needs to look to know what code can be invoked
    from a CSV cell.  Lazily resolved after module body is fully loaded.
    """
    global _RECODE_FUNC_REGISTRY
    if _RECODE_FUNC_REGISTRY is not None:
        return _RECODE_FUNC_REGISTRY
    allowed = (
        "recode_long_strings",
        "recode_numeric",
        "recode_numeric_mean",
        "recode_stringified_list",
        "recode_tokenise",
    )
    import sys
    this_module = sys.modules[__name__]
    registry: dict = {}
    for name in allowed:
        func = getattr(this_module, name, None)
        if callable(func):
            registry[name] = func
        else:
            logger.warning("recode_func registry: %r is in the allow-list but not defined.", name)
    _RECODE_FUNC_REGISTRY = registry
    return registry



def build_field_normalization(var_schema_indexed: pd.DataFrame) -> dict[str, dict]:
    """Derive each field's recode normalization from the annotation contract.

    Replaces the retired var_schema ``mapper`` / ``ignore_strings`` columns. The
    contract's ``enum`` declaration is the single discriminator:

      * Closed-enum field (the contract gives it an ``enum``): structured output
        already constrains the value, so it is not folded through
        ``GENERIC_MAPPER`` — this keeps canonical enum values intact (and lets
        ``Multiple`` / ``-`` stay first-class for fields like main_gender).
      * Every other field: folded through ``GENERIC_MAPPER`` (a harmless no-op on
        value sets it does not touch), normalizing free-text junk.

    Per-field stop words come solely from the contract's ``[recode.drop]`` table
    (keyed by output column name). The global ``IRRELEVANT_WORDS`` stoplist is
    *not* applied here — it would delete legitimate short tags like "can" / "us";
    the recode op that needs it (``recode_tokenise``) applies it itself.

    Args:
        var_schema_indexed: the variable schema indexed by ``variable_name``
            (unused columns are tolerated; nothing beyond the index is required).

    Returns:
        ``{variable_name: {"mapper": dict, "ignore_strings": list}}``.
    """
    try:
        from fyp import annotation_contract as ac

        contract = ac.load_contract()
        enum_fields = ac.enum_field_names(contract)
        drop_words = ac.field_drop_words(contract)
    except Exception:
        enum_fields, drop_words = set(), {}

    normalization: dict[str, dict] = {}
    for name in var_schema_indexed.index:
        closed = name in enum_fields
        normalization[name] = {
            "mapper": {} if closed else GENERIC_MAPPER,
            "ignore_strings": list(drop_words.get(name, [])),
        }
    return normalization




# Generic recode op selected by a variable's ``scale`` — the retired ``recode_func``
# column. A field's recode is its data *kind*, not a per-variable procedure:
#   enum/dichotomous/collection  -> the list/enum cleaner (recode_stringified_list)
#   string/factor                -> the text cleaner (recode_long_strings)
#   numeric/datetime/blank       -> no transform (coercion happens in the scale block)
_RECODE_FUNC_BY_SCALE = {
    "categorical": "recode_stringified_list",
    "dichotomous": "recode_stringified_list",
    "collection": "recode_stringified_list",
    "string": "recode_long_strings",
    "factor": "recode_long_strings",
    "raw": "recode_tokenise",
}



# Uncertain-value handling, derived from a field's data kind (the retired
# ``unable_to_detect_policy`` column). Recode NORMALISES, it does not impute:
#   numeric    -> NaN   (drop the marker; imputation, if wanted, is an analysis step)
#   collection -> []    (empty list)
#   everything else -> keep the "unable to detect" marker
_UNCERTAIN_NUMERIC_SCALES = ("ratio", "interval", "ordinal")


def default_uncertain_policy(scale: str) -> str:
    """Return the uncertain-value policy for a ``scale`` (no imputation)."""
    s = (scale or "").strip()
    if s in _UNCERTAIN_NUMERIC_SCALES:
        return "drop"
    if s == "collection":
        return "empty"
    return "keep"



def build_recode_plan(var_schema_indexed: pd.DataFrame) -> dict:
    """Resolve each variable's recode callable from its ``scale`` + ``source``.

    Replaces the retired ``recode_func`` column. The op is chosen generically:

      * ``source`` starting with ``derived:`` -> no recode (already processed);
      * a numeric array (``int`` sub-key of an ``array`` object, e.g. per-face
        ages) -> the generic ``recode_numeric_mean``;
      * a numeric field the contract bounds (``int`` with ``min``/``max``, e.g. a
        0-100 score) -> the generic ``recode_numeric`` (extract + normalise);
      * otherwise the generic op for the field's ``scale`` (see
        :data:`_RECODE_FUNC_BY_SCALE`); numeric / datetime / blank scales get no
        transform (the scale-specific block handles coercion).

    Args:
        var_schema_indexed: var_schema indexed by ``variable_name`` (reads
            ``scale`` and ``source``).

    Returns:
        ``{variable_name: callable | None}``.
    """
    registry = get_recode_func_registry()
    has_scale = "scale" in var_schema_indexed.columns
    has_source = "source" in var_schema_indexed.columns
    try:
        from fyp import annotation_contract as ac

        contract = ac.load_contract()
        ranges = ac.contract_numeric_ranges(contract)
        bounded = {n for n in var_schema_indexed.index if n in ranges}
        array_numeric = {
            n for n in var_schema_indexed.index
            if n in ac.contract_numeric_array_fields(contract)
        }
    except Exception:
        bounded, array_numeric = set(), set()

    plan: dict = {}
    for name in var_schema_indexed.index:
        source = str(var_schema_indexed.at[name, "source"]).strip() if has_source else ""
        if source.startswith("derived:"):
            plan[name] = None
            continue
        if name in array_numeric:
            plan[name] = registry.get("recode_numeric_mean")
            continue
        if name in bounded:
            plan[name] = registry.get("recode_numeric")
            continue
        scale = str(var_schema_indexed.at[name, "scale"]).strip() if has_scale else ""
        func_name = _RECODE_FUNC_BY_SCALE.get(scale)
        plan[name] = registry.get(func_name) if func_name else None
    return plan



def parse_recode_func(value):
    """Resolve a ``recode_func`` cell to a callable, or None.

    Strict registry lookup — never runs ``eval`` and never executes
    arbitrary names.  Unknown / unparseable values become None and are
    logged so the runtime keeps going (no recode applied) rather than
    raising during pipeline import.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if not s:
        return None
    registry = get_recode_func_registry()
    func = registry.get(s)
    if func is None:
        logger.warning(
            "var_schema: unknown recode_func %r — treating as no-op. "
            "Add the function to fyp.recode_variables and to the allow-list "
            "in get_recode_func_registry().",
            s,
        )
        return None
    return func



def parse_accepted_labels(value):
    """Resolve an ``accepted_labels`` cell to a list of strings.

    Allowed forms:
      1. empty / NaN → ``[]``
      2. JSON array
      3. legacy bareword form ``[a, b, c]`` — comma-split, stripped

    No eval.  Used only by Gemini annotation pre-flight checks
    (see :func:`fyp.machine_annotation`); never feeds the recode pipeline.
    """
    if value is None:
        return []
    try:
        if pd.isna(value):
            return []
    except (TypeError, ValueError):
        pass
    if isinstance(value, list):
        return value
    s = str(value).strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except json.JSONDecodeError:
        pass
    # Legacy bareword form: [item, item, item]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1]
        if not inner.strip():
            return []
        return [tok.strip() for tok in inner.split(",") if tok.strip()]
    logger.warning("var_schema: accepted_labels value %r could not be parsed; using []", s)
    return []



class VarSchemaError(dict):
    """A single validation error.

    Dict-like so the API layer can ``jsonify`` it directly; the fields
    are stable: ``row``, ``variable_name``, ``column``, ``value``, ``message``.
    """

    def __init__(self, row: int, variable_name, column: str, value, message: str):
        super().__init__(
            row=int(row) if row is not None else None,
            variable_name=None if variable_name is None else str(variable_name),
            column=column,
            value=None if value is None else str(value),
            message=message,
        )



def _is_blank(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() == ""



def validate_var_schema(df: pd.DataFrame) -> list[VarSchemaError]:
    """Return a list of structured errors describing schema rows that
    would misbehave at runtime.  Empty list ⇒ schema is safe to save.

    Checks (in row order):
      * ``variable_name`` present and unique
      * ``role`` ∈ ``VAR_SCHEMA_ROLES`` (blank allowed)
      * ``scale`` ∈ ``VAR_SCHEMA_SCALES`` (blank allowed)
      * ``accepted_labels`` is empty / JSON array / legacy bareword list
      * ``sortable``, ``web_filter_prio``, ``web_timeline_prio``,
        ``web_viz_prio``, ``web_display_prio`` parse as integers when set
      * ``searchable`` ∈ ``{"", "1"}``
    """
    errors: list[VarSchemaError] = []
    if df is None or df.empty:
        return errors

    if "variable_name" not in df.columns:
        errors.append(VarSchemaError(0, None, "variable_name", None,
                                     "variable_name column is missing"))
        return errors

    seen_names: dict[str, int] = {}
    for idx, row in df.iterrows():
        row_idx = int(idx) if isinstance(idx, (int, np.integer)) else 0
        name_raw = row.get("variable_name")
        if _is_blank(name_raw):
            errors.append(VarSchemaError(row_idx, None, "variable_name", name_raw,
                                         "variable_name is required"))
            continue
        name = str(name_raw).strip()
        if name in seen_names:
            errors.append(VarSchemaError(row_idx, name, "variable_name", name,
                                         f"duplicate variable_name (first seen on row {seen_names[name]})"))
        else:
            seen_names[name] = row_idx

        # role
        role = row.get("role")
        if not _is_blank(role) and str(role).strip() not in VAR_SCHEMA_ROLES:
            errors.append(VarSchemaError(row_idx, name, "role", role,
                                         f"unknown role; expected one of {sorted(VAR_SCHEMA_ROLES)} or blank"))

        # scale
        scale = row.get("scale")
        if not _is_blank(scale) and str(scale).strip() not in VAR_SCHEMA_SCALES:
            errors.append(VarSchemaError(row_idx, name, "scale", scale,
                                         f"unknown scale; expected one of {sorted(VAR_SCHEMA_SCALES)} or blank"))

        # accepted_labels
        al = row.get("accepted_labels")
        if not _is_blank(al):
            s = str(al).strip()
            ok = False
            try:
                parsed = json.loads(s)
                ok = isinstance(parsed, list)
            except json.JSONDecodeError:
                pass
            if not ok and s.startswith("[") and s.endswith("]"):
                ok = True  # legacy bareword form
            if not ok:
                errors.append(VarSchemaError(row_idx, name, "accepted_labels", al,
                                             "accepted_labels must be a JSON array or a legacy bareword list"))

        # integer-priority columns
        for col in ("sortable", "web_filter_prio", "web_timeline_prio",
                    "web_viz_prio", "web_display_prio"):
            val = row.get(col)
            if _is_blank(val):
                continue
            try:
                int(str(val).strip())
            except ValueError:
                errors.append(VarSchemaError(row_idx, name, col, val,
                                             f"{col} must be an integer when set"))

        # boolean-shaped columns
        for col, allowed in (
            ("searchable", {"", "1"}),
        ):
            val = row.get(col)
            if _is_blank(val):
                continue
            if str(val).strip() not in allowed:
                errors.append(VarSchemaError(row_idx, name, col, val,
                                             f"{col} must be one of {sorted(allowed)} or blank"))

    return errors



def compute_var_schema_hash() -> str:
    """Return a deterministic SHA-256 hash of the active variable schema.

    Only the columns that actually drive recoding (``SEMANTIC_COLUMNS``) plus
    ``variable_name`` are hashed, together with the contract-derived recode
    normalization (the retired ``mapper`` / ``ignore_strings`` columns, now
    sourced from ``annotation_contract.toml``).  Cosmetic / web-UI columns
    (``web_*``, ``sortable``, ``searchable``, ``display_name``, ``section``,
    ``description``) are excluded so admin tweaks to presentation never
    invalidate cached study parquets.

    Output is prefixed with ``VAR_SCHEMA_HASH_VERSION`` so digests from
    different hash generations cannot collide.  Row order, column order, and
    pandas dtype-backend variations do not affect the result.
    """

    if "var_schema" not in fyp_cf or fyp_cf["var_schema"] is None or fyp_cf["var_schema"].empty:
        return f"{VAR_SCHEMA_HASH_VERSION}:empty"

    schema = fyp_cf["var_schema"].copy()
    keep = ["variable_name"] + [c for c in SEMANTIC_COLUMNS if c in schema.columns]
    schema = schema[[c for c in keep if c in schema.columns]]
    if "variable_name" in schema.columns:
        schema = schema.sort_values("variable_name").reset_index(drop=True)
    schema = schema.reindex(sorted(schema.columns), axis=1)
    payload = schema.to_csv(index=False).encode("utf-8")

    # Fold in the contract-derived recode normalization. The retired ``mapper`` /
    # ``ignore_strings`` columns now come from annotation_contract.toml + the
    # GENERIC_MAPPER config dict, so a contract enum / ``[recode.drop]`` edit (or a
    # GENERIC_MAPPER change) must still invalidate cached study parquets.
    norm_payload = b""
    try:
        indexed = fyp_cf["var_schema"].set_index("variable_name")
        norm = build_field_normalization(indexed)
        recode_plan = build_recode_plan(indexed)
        has_scale = "scale" in indexed.columns
        compact = {
            n: {
                "fold": bool(v["mapper"]),
                "drop": sorted(v["ignore_strings"]),
                # The retired recode_func / unable_to_detect_policy columns are now
                # derived (scale + source); fold the resolved op name and policy in
                # so a source/scale change that alters recoding invalidates caches.
                "op": getattr(recode_plan.get(n), "__name__", "none"),
                "policy": default_uncertain_policy(
                    str(indexed.at[n, "scale"]) if has_scale else ""
                ),
            }
            for n, v in norm.items()
        }
        gm_digest = hashlib.sha256(
            json.dumps(GENERIC_MAPPER, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        norm_payload = json.dumps(
            {"fields": compact, "gm": gm_digest}, sort_keys=True
        ).encode("utf-8")
    except Exception:
        pass

    digest = hashlib.sha256(payload + norm_payload).hexdigest()
    return f"{VAR_SCHEMA_HASH_VERSION}:{digest}"





def get_factors_and_features_from_var_schema(some_events_df = None, verbose = False):

    if "var_schema" not in fyp_cf or fyp_cf["var_schema"].empty:
        return [], []

    var_schema = fyp_cf["var_schema"]

    the_factors = sorted(list(set(var_schema[var_schema["role"].isin(['factor','group_factor'])].variable_name)))
    the_features = sorted(list(set(var_schema[var_schema["role"]=='feature'].variable_name)))
    if some_events_df is not None:
        the_factors = [c for c in the_factors if c in some_events_df.columns]
        the_features = [c for c in the_features if c in some_events_df.columns]

    if verbose and len(the_factors) > 0:
        print("    Factors:",", ".join(the_factors))
    if verbose and len(the_features) > 0:
        print("    Features:",", ".join(the_features))

    return the_factors, the_features



def get_grouping_factors_from_var_schema(some_events_df = None, verbose = False):

    if "var_schema" not in fyp_cf or fyp_cf["var_schema"].empty:
        return []

    var_schema = fyp_cf["var_schema"]

    the_grouping_factors = sorted(list(set(var_schema[var_schema["role"]=='group_factor'].variable_name)))
    if some_events_df is not None:
        the_grouping_factors = [c for c in the_grouping_factors if c in some_events_df.columns]
    
    if verbose  and len(the_grouping_factors) > 0:
        print("    Grouping Factors:",", ".join(the_grouping_factors))

    return the_grouping_factors





def derive_australian_relevance(df: pd.DataFrame) -> pd.DataFrame:
    """Backfill ``australian_relevance`` from ``primary_country`` where missing.

    The generalized contract replaced the ``australian_relevance`` yes/no field
    with ``primary_country`` (any country), so rows annotated under the new
    contract carry ``primary_country`` but no ``australian_relevance``, while rows
    from older versions still carry the model-output ``australian_relevance``.
    This coalesces the two so the existing dichotomous ``australian_relevance``
    feature stays populated: ``"yes"`` when the primary country is Australia,
    else ``"no"`` — applied only to rows that lack an existing value (older rows
    are left untouched). A no-op when ``primary_country`` is absent.

    Args:
        df: A recoded dataframe (annotation columns already lower-cased).

    Returns:
        The same dataframe with ``australian_relevance`` coalesced.
    """
    if "primary_country" not in df.columns:
        return df

    country = df["primary_country"].astype("string").str.strip().str.lower()
    derived = pd.Series(pd.NA, index=df.index, dtype="string")
    derived = derived.mask(country.notna() & country.ne(""), "no")
    derived = derived.mask(country.eq("australia"), "yes")

    if "australian_relevance" in df.columns:
        existing = df["australian_relevance"].astype("string").str.strip()
        has_existing = existing.notna() & existing.ne("")
        df["australian_relevance"] = existing.where(has_existing, derived).astype("string")
    else:
        df["australian_relevance"] = derived

    return df









def _is_emoji(s: str) -> bool:
    from emoji import EMOJI_DATA

    """Return True if the string is a valid emoji (including multi-char ones)."""
    return s in EMOJI_DATA







def recode_tokenise(
    a_description: str | pd.Series,
    recoding_policy: dict = {}) -> dict | pd.DataFrame:
    """Tokenise free text into ``hashtags`` / ``mentions`` / ``not_hashtags`` and a
    combined ``words`` list (every kept token, in order). One shared op for any
    text -> tags field — the scrape caption (``desc`` uses hashtags/mentions) and a
    plain instruction (``call_to_action`` uses ``words``). Stop words are dropped
    via the global IRRELEVANT_WORDS list; emojis are kept.
    """
    
    # Vectorized handling for Series
    if isinstance(a_description, pd.Series):
        # We'll use a fast regex approach to extract all relevant tokens once
        # Token pattern: #word or @word or word
        # We need to exclude IRRELEVANT_WORDS and handle emojis
        # Doing full logic in regex is hard, but we can extract all words and filter
        
        # NOTE: For complex logic like "exclude irrelevant words", a list comprehension
        # is often faster than pure pandas string ops if the ops are complex.
        # But let's try to be efficient. 
        
        # Actually, for 100k rows, a simple apply might be acceptable if the inner function is fast,
        # but let's try to speed it up.
        # The original logic splits by space, cleans chars, checks length/irrelevant/emoji.
        
        # Let's stick to the list comprehension for now as it's readable and Python 3.14 is fast.
        # Pre-compile translation table for fast cleaning
        # chars to remove: ",.:;!)(*/&|^%$#@<>?'`’1234567890"
        remove_chars = ",.:;!)(*/&|^%$#@<>?'`’1234567890"
        trans_table = str.maketrans("", "", remove_chars)
        
        # Optimized Apply
        def _fast_parse(text):
            if not isinstance(text, str) or not text:
                return {"hashtags": [], "mentions": [], "not_hashtags": [], "words": []}

            hashtags = []
            mentions = []
            not_hashtags = []
            words_all = []

            # fast split
            # text.split() is fast
            words = text.split()

            for w in words:
                # fast clean using translate
                # w.lower()
                clean_word = w.lower().translate(trans_table)

                if not clean_word: continue

                # logic
                if (len(clean_word) > 1 and clean_word not in IRRELEVANT_WORDS) or _is_emoji(clean_word):
                    words_all.append(clean_word)
                    if w.startswith("#"):
                        hashtags.append(clean_word)
                    elif w.startswith("@"):
                        mentions.append(clean_word)
                    else:
                        not_hashtags.append(clean_word)

            return {"hashtags": hashtags, "mentions": mentions, "not_hashtags": not_hashtags, "words": words_all}

        return a_description.apply(_fast_parse)

    # Legacy single string handling
    hashtags = []
    not_hashtags = []
    mentions = []
    words_all = []
    if not isinstance(a_description,str) or len(a_description) == 0:
        return {
            "hashtags":[],
            "mentions":[],
            "not_hashtags":[],
            "words":[]
        }
    words = a_description.split(" ")
    for w in words:
        if len(w)>0:
            first_char = w[0]
            clean_word = "".join([j for j in w.lower() if j not in ",.:;!)(*/&|^%$#@<>?'`’1234567890"])
            if (len(clean_word)>1 and clean_word not in IRRELEVANT_WORDS) or _is_emoji(clean_word):
                words_all += [clean_word]
                if first_char=="#":
                    hashtags += [clean_word]
                elif first_char=="@":
                    mentions += [clean_word]
                else:
                    not_hashtags += [clean_word]

    return {
        "hashtags":hashtags,
        "mentions":mentions,
        "not_hashtags":not_hashtags,
        "words":words_all
    }




_LEADING_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def recode_numeric(
    value: str | pd.Series,
    recoding_policy: dict = {}) -> float | pd.Series:
    """Generic numeric recode: extract the number from a value and, when the
    contract declares a bounded range for the field (``normalize_range`` in the
    policy), rescale it to a 0-1 ratio.

    A field's number lives in its value; the recode just reads it and optionally
    normalises — no per-field parser. Tolerant of legacy free-text forms such as
    ``"75, explanation"`` or ``"60% speech, 40% music"`` (the leading number is
    taken), so it reproduces the retired ``recode_scores`` / ``recode_speech_vs_music``
    on old data while consuming clean integers from a retyped contract.
    """
    rng = recoding_policy.get("normalize_range")

    if isinstance(value, pd.Series):
        nums = pd.to_numeric(
            value.astype(str).str.extract(r"(-?\d+(?:\.\d+)?)", expand=False),
            errors="coerce",
        )
        if rng is not None and rng[1] != rng[0]:
            nums = (nums - rng[0]) / (rng[1] - rng[0])
        return nums

    if pd.isna(value):
        return pd.NA
    match = _LEADING_NUMBER_RE.search(str(value))
    if not match:
        return pd.NA
    num = float(match.group(0))
    if rng is not None and rng[1] != rng[0]:
        return (num - rng[0]) / (rng[1] - rng[0])
    return num




# Positive numbers only — for an array-of-numbers value the hyphen in "20-30" is
# a range separator, not a minus sign.
_POSITIVE_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def recode_numeric_mean(
    value: str | pd.Series,
    recoding_policy: dict = {}) -> float | pd.Series:
    """Per-item mean for an array-of-numbers field (the value is pipe-joined).

    Each item is reduced to the mean of its own numbers (so a clean integer
    ``"25"`` -> 25 and a legacy range ``"20-30"`` -> 25), then the items are
    averaged. This weights per item — e.g. per face for ``faces.age_estimate`` —
    so it reproduces the retired ``recode_faces_age_estimate`` (mean of per-face
    age midpoints) on legacy data while consuming clean integers from a retyped
    contract. No backfill.
    """
    if isinstance(value, pd.Series):
        return value.apply(lambda x: recode_numeric_mean(x, recoding_policy))

    if pd.isna(value):
        return pd.NA
    per_item = []
    for item in str(value).split(SPLITTER):
        nums = [float(n) for n in _POSITIVE_NUMBER_RE.findall(item)]
        if nums:
            per_item.append(sum(nums) / len(nums))
    if not per_item:
        return pd.NA
    return sum(per_item) / len(per_item)




def recode_long_strings(
    s: str | list | pd.Series, 
    recoding_policy) -> str | pd.Series:

    
    if isinstance(s, pd.Series):
        def _get_first_if_list(x):
            if isinstance(x, list):
                return x[0] if len(x) > 0 else ""
            if isinstance(x, str):
                return x
            # NA or any other non-str/non-list value collapses to "". The Series
            # branch is the canonical behaviour here; the scalar branch below
            # mirrors it.
            return ""

        new_s = s.map(_get_first_if_list)
        new_s = new_s.replace("-", "")
        return new_s

    if not isinstance(s,(str,list)):
        return ""
    if isinstance(s,list) and len(s)>0:
        new_string = s[0]
    elif isinstance(s,list) and len(s)==0:
        new_string = ""
    else:
        new_string = copy(s)

    if new_string == "-":
        new_string = ""

    return new_string





def recode_challenges(
    challenges : str | pd.Series,
    recoding_policy : dict = {}) -> list | pd.Series:

    
    if isinstance(challenges, pd.Series):
        # Split on the literal " | " separator. Must pass regex=False because the
        # pipe would otherwise be parsed as a regex alternation operator, splitting
        # on whitespace instead of the full " | " token.
        mask_na = challenges.isna()
        s = challenges.astype(str).str.replace("  ", " ", regex=False).str.split(" | ", regex=False)

        def _clean_list(mod_list):
            if not isinstance(mod_list, list): return []
            return [v.strip() for v in mod_list if v.strip()]

        result = s.map(_clean_list)
        # NA inputs should round-trip to [] to match the scalar branch.
        if mask_na.any():
            result.loc[mask_na] = pd.Series(
                [[] for _ in range(int(mask_na.sum()))],
                index=result.index[mask_na],
            )
        return result

    if isinstance(challenges, str):
        return [
            v.strip().replace("  ", " ")
            for v in challenges.split(" | ")
            if v.strip()
        ]
    else:
        return []




# making a very rough simplification of main activity, picking the first word that
# ends with -ing. The assumption is that this is a verb (I know it isn't) and
# that it captures the video's main activity 
def recode_stringified_list(
    a_string_representing_a_list, 
    recoding_policy
    ) -> list | pd.Series:

    
    if isinstance(a_string_representing_a_list, pd.Series):
        # Full vectorisation is impractical given the mapper/ignore_strings lookup
        # and the strict per-character filter further down, so the Series branch
        # delegates to the scalar branch element-wise. Keeping this explicit
        # (rather than relying on a silent fallback in recode_events_df) makes
        # the scalar call-site visible for future profiling.
        return a_string_representing_a_list.apply(lambda x: recode_stringified_list(x, recoding_policy))

    # Legacy / Single Item logic
    no_data_fallback = UNABLE_TO_DETECT

    ignore_strings = recoding_policy.get("ignore_strings", [])
    #splitter = recoding_policy.get("splitter", None)
    mapper = recoding_policy.get("mapper", {})
    
    mini_mapper = {1: "yes", 0: "no", True: "yes", False: "no"}

    list_of_the_words = [] 

    # if the string that is representing a list is na, assume that it hasn't been coded
    if pd.isna(a_string_representing_a_list):
        list_of_the_words += [NOT_CODED]

    # if there is a string, but the length is zero
    elif len(str(a_string_representing_a_list)) < 1 or str(a_string_representing_a_list) in ["-"," "]:
        list_of_the_words += [no_data_fallback]

    else:
        a_string_representing_a_list = mini_mapper.get(a_string_representing_a_list,a_string_representing_a_list)

        for an_element in str(a_string_representing_a_list).lower().split(SPLITTER):
            if len(an_element)>0:
                an_element = an_element.replace("//", "").replace("&", " and ").replace("/", " or ")
                clean_word = "".join([j for j in an_element.lower() if j not in ",.:;!)(*/&|^%$#@<>?'`’1234567890"])
                clean_word = clean_word.strip()
                if (len(clean_word)>1 and clean_word not in ignore_strings)  or _is_emoji(clean_word):
                    list_of_the_words += [mapper.get(clean_word,clean_word)]
        
    if len(list_of_the_words) == 0:
        list_of_the_words += [no_data_fallback]

    return list_of_the_words




def implement_missing_data_policy(x, missing_data_policy, the_median=0):
    
    if isinstance(x, pd.Series):
        # 1. Check direct scalar matches and NaNs
        # Note: x == NOT_CODED works for scalars. 
        # If x has mixed types (lists), equality comparison might be tricky but usually handles it (False for list!=scalar).
        # But to be safe and avoid "ambiguous truth value" errors for [NOT_CODED] == NOT_CODED comparisons:
        # We handle lists separately.
        
        mask_basic = x.isna()
        
        # Safe scalar comparison for "== NOT_CODED"
        # If x is object, it might contain lists. x == scalar might trigger elementwise check if x was an array, 
        # but x is a Series. Series == scalar is fine.
        # But if an element of Series is a list/array, `element == scalar` might return an array (if numpy) or False (if list).
        # If it returns an array, Series.eq converts it to boolean? No, it raises ValueError if valid boolean result is ambiguous.
        # So we must NOT use `x == NOT_CODED` blindly on object columns that might contain arrays.
        
        # Strategy:
        # Use simple map(type) check to isolate lists? No, map is slow.
        # Use `x.astype(str) == str(NOT_CODED)`? Slow string conversion.
        
        # Only do strict checks if object.
        if x.dtype == object:

            
            mask_scalar = x.isin([NOT_CODED])
            
            # Now list check:
            # We need to ensure we can use .str.
            
            try:
                # This works if at least some strings/lists or object dtype allows it?
                # Actually, .str accessor on object series works if it contains mixed types.
                # But if all are ints, it fails.
                mask_list = (x.str.len() == 1) & (x.str[0] == NOT_CODED)
                mask_list = mask_list.fillna(False)
            except AttributeError:
                # No str accessor means no lists/strings usually?
                mask_list = False
            
            mask = mask_basic | mask_scalar | mask_list
            
        else:
            # Numeric or specific type
            mask = mask_basic | (x == NOT_CODED)
        
        if not mask.any():
            return x
            
        result = x.copy()
        
        if missing_data_policy == "empty":
            result.loc[mask] = pd.Series([[] for _ in range(mask.sum())], index=result.index[mask])
        elif missing_data_policy == "drop":
            result[mask] = pd.NA
        elif missing_data_policy == "median":
            result[mask] = the_median
        elif missing_data_policy == "keep":
            # if isna -> [NOT_CODED], else keep x (which is what?)
            # The original logic: if isna(x) -> [NOT_CODED].
            # If x was NOT_CODED (str), it returns x (NOT_CODED).
            
            # Implementation: Replace NA with [NOT_CODED]. leave "not coded" alone.
            mask_na = x.isna()
            result.loc[mask_na] = pd.Series([[NOT_CODED] for _ in range(mask_na.sum())], index=result.index[mask_na])
            
        elif missing_data_policy == "zero":
            # Check type of first non-missing element to decide 0 vs "no"?
            # Or pass a hint. The original code checks x itself.
            # "numeric" string check is weird in original? `isinstance(gg,"numeric")` is probably wrong (string "numeric")?
            # actually `isinstance(gg,"numeric")` checks if class is string "numeric" which is false.
            # It likely meant `isinstance(gg, (int, float))`.
            
            # Let's assume numeric -> 0, else "no".
            # We can check dtype of series.
            if pd.api.types.is_numeric_dtype(x):
                val = 0
            else:
                val = "no"
                
            # If input was list, return [val]
            # This complex conditional typing is hard to vectorize perfectly without context.
            # For now, simplistic approach:
            result[mask] = val
        
        return result

    if (isinstance(x,list) and len(x)==1 and x[0]==NOT_CODED) or (isinstance(x,str) and x==NOT_CODED) or ((not isinstance(x,list)) and pd.isna(x)):
        if missing_data_policy == "empty":
            return []
        elif missing_data_policy == "drop":
            return pd.NA
        elif missing_data_policy == "median":
            return the_median
        elif missing_data_policy == "keep":
            if pd.isna(x):
                return [NOT_CODED]
            else:
                return x
        elif missing_data_policy == "zero":
            gg = x if not isinstance(x,list) else x[0]
            if isinstance(gg,(int, float, np.int64, np.float64)):
                gg_out = 0
            else:
                gg_out = "no"
            if isinstance(x,list):
                return [gg_out]
            return gg_out
        else:
            return x

    else:
        return x





def implement_unable_to_detect_policy(x, unable_to_detect_policy, the_median=0):


    if isinstance(x, pd.Series):
        # Vectorized implementation
        mask_basic = x.isna()
        
        if x.dtype == object:
             mask_scalar = x.isin([UNABLE_TO_DETECT])
             try:
                 mask_list = (x.str.len() == 1) & (x.str[0] == UNABLE_TO_DETECT)
                 mask_list = mask_list.fillna(False)
             except AttributeError:
                 mask_list = False
             
             mask = mask_basic | mask_scalar | mask_list
        else:
             mask = mask_basic | (x == UNABLE_TO_DETECT)

        if not mask.any():
            return x
            
        result = x.copy()
        
        if unable_to_detect_policy == "empty":
            result.loc[mask] = pd.Series([[] for _ in range(mask.sum())], index=result.index[mask])
        elif unable_to_detect_policy == "drop":
            result[mask] = pd.NA
        elif unable_to_detect_policy == "median":
            result[mask] = the_median
        elif unable_to_detect_policy == "keep":
             mask_na = x.isna()
             result.loc[mask_na] = pd.Series([[UNABLE_TO_DETECT] for _ in range(mask_na.sum())], index=result.index[mask_na])
        elif unable_to_detect_policy == "zero":
             if pd.api.types.is_numeric_dtype(x):
                val = 0
             else:
                val = "no"
             result[mask] = val
             
        return result

    if (isinstance(x,list) and len(x)==1 and x[0]==UNABLE_TO_DETECT) or (isinstance(x,str) and x==UNABLE_TO_DETECT) or ((not isinstance(x,list)) and pd.isna(x)):
        if unable_to_detect_policy == "empty":
            return []
        elif unable_to_detect_policy == "drop":
            return pd.NA
        elif unable_to_detect_policy == "median":
            return the_median
        elif unable_to_detect_policy == "keep":
            if pd.isna(x):
                return [UNABLE_TO_DETECT]
            else:
                return x
        elif unable_to_detect_policy == "zero":
            gg = x if not isinstance(x,list) else x[0]
            if isinstance(gg,(int, float, np.int64, np.float64)):
                gg_out = np.int64(0)
            else:
                gg_out = "no"
            if isinstance(x,list):
                return [gg_out]
            return gg_out
        else:
            return x
    else:
        return x







def recode_events_df(
    study_dataset: pd.DataFrame = None,
    drop_single_value_cols: bool = True,
    ensure_pyarrow_compliance: bool = True,
    verbose: bool = False
    ):


    # Safe nunique for lists
    def _safe_nunique(s):
        try:
            return s.nunique()
        except TypeError:
            return s.astype(str).nunique()


    print("Recoding variables, implementing missing data policy and a whole range of other things...")

    # This thing now only works with a study dataset as input
    # It is not used in the web interface but only in the offline data prep

    if study_dataset is None:
        print("  This process cannot run without a study dataset as input. Process failed.")
        return None




    cool_events = study_dataset.copy()

    var_schema = fyp_cf["var_schema"].copy()

    var_schema.set_index("variable_name", inplace=True)

    # Per-field mapper + ignore_strings, and the per-field recode callable, are
    # both derived (the retired var_schema columns) and injected into each
    # field's recoding policy below — mapper/ignore_strings from the annotation
    # contract, the recode op from the field's scale + source.
    field_normalization = build_field_normalization(var_schema)
    recode_plan = build_recode_plan(var_schema)
    # Contract-declared 0-N ranges for bounded numeric fields, so recode_numeric
    # can normalise them to 0-1 without a per-field parser.
    try:
        from fyp import annotation_contract as ac

        _contract = ac.load_contract()
        _ranges = ac.contract_numeric_ranges(_contract)
        field_ranges = {n: _ranges[n] for n in var_schema.index if n in _ranges}
    except Exception:
        field_ranges = {}

    fyp_factors, _ = get_factors_and_features_from_var_schema(some_events_df = cool_events, verbose = verbose)


    # this will be overwritten in at a later stage - I just want to turn it into a string for now
    try:
        if "session_id" in cool_events.columns:
            cool_events["session_id"] = cool_events["session_id"].map(lambda x:f"S{int(x):05}" if pd.notna(x) else pd.NA)
    except Exception:
        # it's not vital that this goes well
        pass

    # this is a bit redundant too - these variables checked (are dropped again if necessary) at another stage
    variables_not_found_in_var_schema = list(set(cool_events.columns) - set(var_schema.index))
    if len(variables_not_found_in_var_schema) > 0:
        if verbose:
            join_str = "\n    - "
            print(f"Step 1. Dropping {len(variables_not_found_in_var_schema)} columns not found in the variable scheme:\n    - {join_str.join(variables_not_found_in_var_schema)}")
        cool_events = cool_events.drop(columns=variables_not_found_in_var_schema).copy()



    if drop_single_value_cols:
        single_value_columns = [c for c in cool_events.columns if _safe_nunique(cool_events[c])==1 and c not in fyp_factors]
        if verbose:
            join_str = "\n    - "
            print(f"Step 2. Dropping {len(single_value_columns)} single value columns:\n    - {join_str.join(single_value_columns)}. Shape: {cool_events.shape}")
        cool_events = cool_events.drop(columns=single_value_columns).copy()



    if verbose:
        print(f"Executing recode policies from variable schema. Shape: {cool_events.shape}")


    cool_columns = copy(cool_events.columns)
    # Frames produced by dict-column unpacking (pd.json_normalize below) that
    # can be safely deferred to a single concat after the loop. Deferring avoids
    # the O(n_rows × n_cols) copy that `pd.concat(axis=1)` would incur inside
    # the loop. We only defer a frame when none of its columns are scheduled
    # to be iterated by the remaining passes of this outer loop — otherwise
    # the original semantics (new columns visible to later iterations) must
    # be preserved and we materialize the concat immediately.
    deferred_unpacked_frames: list[pd.DataFrame] = []
    remaining_columns_by_index = [
        set(cool_columns[j+1:]) for j in range(len(cool_columns))
    ]
    # iterate over the columns in the events df
    for i,c in enumerate(cool_columns):
        preamble = f"    {(i+1):02}/{len(cool_columns):02}. {c}{' '*(40-len(c))}"
        preamble2 = f"    {' '*6} {c}{' '*(40-len(c))}"
        #if verbose: 
        #    print(preamble, end="", flush=True)

        # if this is in the var_schema...
        if c in var_schema.index:
            this_var_schema = var_schema.loc[c].to_dict()
            # Overlay the contract-derived mapper + ignore_strings, and the
            # scale-derived recode callable, into the recoding policy (all three
            # replaced dropped var_schema columns).
            this_var_schema.update(field_normalization.get(c, {}))
            this_var_schema["recode_func"] = recode_plan.get(c)
            this_var_schema["normalize_range"] = field_ranges.get(c)

            if this_var_schema.get("role", "undefined") != "skip":

                # ------------------------------------------------------
                # 1.'raw' means that the variable is going to be transformed into a set of new variables
                # and if there is a variable in the schema with the same name as this variable but with the
                # extension "_raw" it means that I want to keep the original variable (it is copied here).
                # If such a variable name isn't in the schema, then the original variable will be dropped.   
                # ------------------------------------------------------
                if this_var_schema.get("scale", "undefined") == "raw" and c+"_raw" in var_schema.index:
                    if verbose:
                        print(f"{preamble}Copied raw. ", end="", flush=True)
                    cool_events[c+"_raw"] = cool_events[c].copy()
                else:
                    if verbose:
                        print(preamble, end="", flush=True)

                # ------------------------------------------------------
                # 2. execute the recode function
                # ------------------------------------------------------
                func = this_var_schema.get("recode_func", None)
                if not pd.isna(func):
                    # Pass the full series. If the Series branch of the recode function
                    # raises, the fallback to a per-element .map() is OPT-IN via the
                    # `allow_scalar_fallback` flag in var_schema (default: False).
                    # Silent fallback has historically masked real bugs (e.g. main_activity
                    # calling .extract() instead of .str.extract(), content_category
                    # whitespace/fuzzy-match length mismatch), so the default is now to
                    # re-raise with the full traceback visible.
                    try:
                        cool_events[c] = func(cool_events[c], this_var_schema)
                        if verbose: print(f"Recoded successfully ({this_var_schema.get('scale', 'unknown scale')})")
                    except Exception as e:
                        traceback.print_exc()
                        raw_flag = this_var_schema.get("allow_scalar_fallback", False)
                        allow_fallback = str(raw_flag).strip().lower() in ("true", "1", "yes")
                        if not allow_fallback:
                            raise Exception(
                                f"Vectorized recode failed for column '{c}' and scalar fallback is "
                                f"not enabled. Fix the Series branch of {getattr(func, '__name__', func)!r} "
                                f"or set `allow_scalar_fallback=true` in var_schema for this column "
                                f"if the scalar fallback is intentional."
                            ) from e
                        print(f"Warning: Vectorized recode failed for '{c}' ({e}). Falling back to map (allow_scalar_fallback=true).")
                        try:
                            cool_events[c] = cool_events[c].map(lambda x: func(x, this_var_schema))
                        except Exception as map_e:
                            raise Exception(f"Error: Map recode also failed for '{c}': ({map_e}).") from map_e
                else:
                    if verbose: print(f"Has no recode func, so no change ({this_var_schema.get('scale', 'unknown scale')})")


                # ------------------------------------------------------
                # 3. normalise missing / unable-to-detect values by data kind.
                #    Recode does NOT impute — numeric uncertainty becomes NaN,
                #    collections become [], everything else keeps the marker.
                #    Median/zero imputation, if wanted, is an analysis-layer step.
                # ------------------------------------------------------
                uncertain_policy = default_uncertain_policy(this_var_schema.get("scale", ""))

                cool_events[c] = implement_unable_to_detect_policy(
                    cool_events[c],
                    uncertain_policy,
                    None)

                cool_events[c] = implement_missing_data_policy(
                    cool_events[c],
                    "drop",#this_var_schema.get("missing_data_policy","No policy"),
                    None)
                
                
                # ------------------------------------------------------
                # 4. Check for multiple values/types logic
                # ------------------------------------------------------
                
                # If we expect single values (categorical, dichotomous, etc.), ensure no lists > 1
                if this_var_schema.get("scale", "") in ["categorical","dichotomous","ordinal","ratio","interval","datetime"]:
                    # Fast check: if object type, might contain lists
                    if cool_events[c].dtype == object:
                        # 'get first if list' logic normalization
                        def _normalize_single(x):
                            if isinstance(x, list):
                                if len(x) > 1: return x # leave as list for validation
                                return x[0] if x else pd.NA
                            return x
                            
                        # apply normalization
                        cool_events[c] = cool_events[c].map(_normalize_single)
                        
                        # Use a sample check or fast check for remaining lists (validation)
                        has_lists = cool_events[c].map(lambda x: isinstance(x, list)).any()
                        if has_lists:
                             # calculate count for error message
                             count = cool_events[c].map(lambda x: isinstance(x, list)).sum()
                             raise ValueError(f"{c} has {count} values with more than one entry. Only a single value is allowed for categorical, dichotomous, ordinal, ratio, and interval variables.")




                # ------------------------------------------------------
                # 4&half. for ratio variables, I only accept numeric values
                # ------------------------------------------------------
                if (this_var_schema["scale"] in ["ratio"]):
                    cool_events[c] = cool_events[c].astype("double[pyarrow]")
                    # Check for integers using numpy float64 - safer for NaNs and avoids pyarrow mod error
                    if not (cool_events[c].dropna().astype("float64") % 1 != 0).any():
                        cool_events[c] = cool_events[c].astype("int64[pyarrow]")



                # ------------------------------------------------------
                # 5. for dichotomous variables, the value set is yes / no plus the
                #    uncertainty marker. The contract's yes_no enum also offers
                #    "Unclear", and a field can be missing — both normalise to the
                #    marker rather than crashing (they are not yes/no).
                # ------------------------------------------------------
                if (this_var_schema["scale"] in ["dichotomous"]):
                    def _normalize_dichotomous(x):
                        if isinstance(x, str):
                            return x if x in ("yes", "no") else UNABLE_TO_DETECT
                        try:
                            return x if pd.isna(x) else UNABLE_TO_DETECT
                        except (TypeError, ValueError):
                            return UNABLE_TO_DETECT
                    cool_events[c] = cool_events[c].map(_normalize_dichotomous)
                    

                # ------------------------------------------------------
                # 6. for dict variables, I unpack the dicts into new separate columns
                # ------------------------------------------------------
                # Check if first valid element is dict
                first_val = None
                try:
                    valid_c = cool_events[c].dropna()
                    if not valid_c.empty:
                        first_val = valid_c.iloc[0]
                except Exception:
                    pass

                if isinstance(first_val, dict):
                    # Proceed with unpacking
                    new_thing = pd.json_normalize(cool_events[c])
                    new_thing = new_thing.add_prefix(f"{c}_")
                    new_thing.index = cool_events.index
                    if verbose:
                        print(f"{preamble2}Recoded to new variables {', '.join(new_thing.columns)}")

                    new_thing_cols = copy(new_thing.columns)
                    for new_thing_c in new_thing_cols:
                        if new_thing_c not in var_schema.index or var_schema.loc[new_thing_c, "role"] == "skip":
                            if verbose:
                                print(f"{preamble2}Skipping new variable: {new_thing_c}")
                            new_thing = new_thing.drop(columns=new_thing_c)

                    # drop the original column or not
                    if var_schema.loc[c,"role"] == "raw":
                        # Lightweight column-level drop (no full-frame copy of
                        # the remaining columns); heavy concat is deferred
                        # below unless a later iteration needs the new cols.
                        cool_events = cool_events.drop(columns=[c])

                    # Defer the concat when no unpacked column collides with a
                    # future iteration's `c`. When any collision exists, fall
                    # back to in-place concat to keep original semantics.
                    if set(new_thing.columns).isdisjoint(remaining_columns_by_index[i]):
                        deferred_unpacked_frames.append(new_thing)
                    else:
                        cool_events = pd.concat([cool_events, new_thing], axis=1)
            else:
                if verbose:
                    print(f"{preamble}Skipping")
                cool_events = cool_events.drop(columns=[c]).copy()
        else:
            if verbose:
                print(f"{preamble}Not found in the variable scheme, skipping")
            cool_events = cool_events.drop(columns=[c]).copy()

    # Flush any deferred unpacked columns in a single concat — this collapses
    # what used to be N in-loop concats into one, avoiding quadratic copies
    # when many schema columns are dicts.
    if deferred_unpacked_frames:
        cool_events = pd.concat([cool_events, *deferred_unpacked_frames], axis=1)

    if ensure_pyarrow_compliance:
        cool_events = convert_dtypes_to_pyarrow(cool_events, verbose=verbose)

    
    print(f"...done recoding variables at {datetime.now()}")

    return cool_events 











# Try to import rapidfuzz for faster matching
try:
    from rapidfuzz import fuzz, process
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False

def recode_fuzzy_match(
    list_a: list, 
    list_b: list, 
    threshold: float = 0.8, 
    verbose: bool = False
) -> list:
    """
    Matches strings in list_a against strings in list_b by similarity.
    Replaces matched strings in A with their counterpart in B.
    Unmatched strings are replaced with 'fyp_cf['labels']['OTHER_THINGS']'.
    
    Optimizations:
    1. Exact matches are checked first (O(1) lookup).
    2. Uses rapidfuzz if available for faster fuzzy matching.
    3. Falls back to difflib if rapidfuzz is not installed.
    
    Args:
        list_a: List of strings to be recoded.
        list_b: List of target strings (the reference list).
        threshold: float between 0 and 1. Similarity score required to match.
        verbose: bool, if True prints warnings/info.
        
    Returns:
        List of strings with same length as list_a.
    """
    
    # Detect if input is a pandas Series to preserve index
    is_series = isinstance(list_a, pd.Series)
    input_index = list_a.index if is_series else None
    
    # ensure inputs are lists for processing
    if is_series:
        processing_list = list_a.tolist()
    elif not isinstance(list_a, list):
        if verbose:
            print(f"Warning: list_a is not a list (got {type(list_a)}). Returning empty list.")
        return []
    else:
        processing_list = list_a
        
    if not isinstance(list_b, list):
        if verbose:
            print(f"Warning: list_b is not a list (got {type(list_b)}). Returning {fyp_cf['labels']['OTHER_THINGS']}.")
        return [fyp_cf['labels']['OTHER_THINGS']] * len(processing_list) # Return list if input was list

    refined_list = []
    
    # Pre-clean list_b to ensure all elements are strings - handle pandas NA
    valid_candidates = [str(x) for x in list_b if pd.notna(x)]
    
    # Optimization 1: Exact Match Lookup
    candidates_set = set(valid_candidates)
    
    # Optimization 2: Use rapidfuzz if available
    rapid_threshold = threshold * 100
    
    for item in processing_list:
        # Handle list-like items (e.g. column of lists like [['tag1', 'tag2'], ['tag3']])
        # Also handle numpy arrays and tuples to avoid "ambiguous truth value" errors in pd.isna()
        if isinstance(item, (list, np.ndarray, tuple)):
            sub_result = []
            for sub_item in item:
                if pd.isna(sub_item) or not isinstance(sub_item, str):
                    sub_result.append(fyp_cf['labels']['OTHER_THINGS'])
                    continue
                
                # Check Exact Match First (Sub-item)
                if sub_item in candidates_set:
                    sub_result.append(sub_item)
                    continue

                # Fuzzy Match (Sub-item)
                best_sub_match = None
                if _HAS_RAPIDFUZZ:
                    result = process.extractOne(
                        sub_item, 
                        valid_candidates, 
                        scorer=fuzz.ratio, 
                        score_cutoff=rapid_threshold
                    )
                    if result:
                        best_sub_match = result[0]
                else:
                    highest_sub_ratio = 0.0
                    for candidate in valid_candidates:
                        ratio = difflib.SequenceMatcher(None, sub_item, candidate).ratio()
                        if ratio > highest_sub_ratio:
                            highest_sub_ratio = ratio
                            best_sub_match = candidate
                    
                    if highest_sub_ratio < threshold:
                        best_sub_match = None
                
                if best_sub_match:
                    sub_result.append(best_sub_match)
                else:
                    sub_result.append(fyp_cf['labels']['OTHER_THINGS'])
            
            refined_list.append(sub_result)
            continue

        # Handle non-string scalar items
        # Check if scalar is NA safely (already know it's not a list)
        if pd.isna(item) or not isinstance(item, str):
            refined_list.append(fyp_cf['labels']['OTHER_THINGS'])
            continue
            
        # Check Exact Match First (Scalar)
        if item in candidates_set:
            refined_list.append(item)
            continue
            
        # Fuzzy Matching (Scalar)
        best_match = None
        
        if _HAS_RAPIDFUZZ:
            # rapidfuzz implementation
            # extractOne returns (match, score, index)
            result = process.extractOne(
                item, 
                valid_candidates, 
                scorer=fuzz.ratio, # Use simple ratio to match difflib behavior roughly
                score_cutoff=rapid_threshold
            )
            
            if result:
                best_match = result[0]
                
        else:
            # difflib fallback implementation
            highest_ratio = 0.0
            
            for candidate in valid_candidates:
                ratio = difflib.SequenceMatcher(None, item, candidate).ratio()
                
                if ratio > highest_ratio:
                    highest_ratio = ratio
                    best_match = candidate
            
            # Check threshold for difflib result
            if highest_ratio < threshold:
                best_match = None
        
        if best_match:
            refined_list.append(best_match)
        else:
            refined_list.append(fyp_cf['labels']['OTHER_THINGS'])
            
    # Return Series if input was Series, correctly indexed
    if is_series:
        return pd.Series(refined_list, index=input_index)
        
    return refined_list
