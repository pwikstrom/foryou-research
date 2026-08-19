import datetime as _dt
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.organize_datasets import create_study_recoded_dataset
from fyp.utils import ENGAGEMENT_TYPES, parse_extra_data_tokens


def get_robust_bounds(series):
    """Return slider bounds as ``(low, high, top_capped)``.

    The lower bound is the true minimum. The upper bound is the true maximum
    unless extreme outliers stretch it: when the max exceeds 3x the 99th
    percentile, the bound is capped at the 99th percentile and flagged. The
    frontend renders a capped top as an open-ended segment ("value+") and
    applies no upper filter when the handle sits there, so outliers above the
    cap are never excluded.
    """
    if series.empty:
        return 0, 0, False

    # Drop NaNs
    s = series.dropna()
    if s.empty:
        return 0, 0, False

    low = float(s.min())
    high = float(s.max())

    p99 = float(s.quantile(0.99))
    if p99 > low and high > 3 * p99:
        return low, p99, True

    return low, high, False





def log_axis_offset(series: pd.Series) -> float:
    """Return the offset ``o`` used by the log display transform ``log10(x + o)``.

    Integer-like data (smallest positive value >= 1, e.g. play counts) keeps the
    classic ``+1`` offset. Fractional data (e.g. per-play ratios) gets a
    data-driven offset so a sub-1 heavy tail spreads across decades instead of
    collapsing into a near-linear axis: half the 1st percentile of positive
    values, floored at one millionth of the maximum. Zeros map to ``log10(o)``,
    just left of the smallest positive decade.

    Args:
        series: Numeric values (NaNs allowed; negatives ignored).

    Returns:
        The offset as a positive float.
    """
    positive = series.dropna()
    positive = positive[positive > 0]
    if len(positive) == 0:
        return 1.0
    min_pos = float(positive.min())
    if min_pos >= 1.0:
        return 1.0
    p01 = float(positive.quantile(0.01))
    max_pos = float(positive.max())
    return float(max(p01 / 2.0, max_pos * 1e-6, 1e-12))






# Interior percentiles shipped to the frontend so numeric filter sliders can be
# frequency-scaled (equal data mass per slider segment).
SLIDER_QUANTILE_PCTS = (5, 10, 25, 50, 75, 90, 95, 99)






def slider_quantiles(series: pd.Series) -> dict:
    """Return interior percentile values for frequency-scaled filter sliders.

    Args:
        series: Numeric column values (NaNs allowed).

    Returns:
        Mapping of percentile (as string, e.g. ``"25"``) to value. Empty when
        the column has too few values to estimate quantiles meaningfully.
    """
    s = series.dropna()
    if len(s) < 20:
        return {}
    qs = s.quantile([p / 100 for p in SLIDER_QUANTILE_PCTS])
    return {str(p): float(v) for p, v in zip(SLIDER_QUANTILE_PCTS, qs) if pd.notna(v)}




# Multiplicative spread (p99 / p10 of positive values) above which a numeric
# column reads better on a log axis. Counts and per-play / per-day rates span
# many decades and clear this bar; bounded 0-1 scores barely span one.
LOG_SCALE_SPREAD_THRESHOLD = 25.0




def derive_log_scale(series: pd.Series) -> bool:
    """Decide whether a numeric column should display on a log10 scale.

    Heavy-tailed non-negative columns — counts and per-play / per-day rates that
    span orders of magnitude, including sub-1 ratios — read better on a log axis;
    bounded scores (0-1 probabilities, percentages) and short ranges do not. The
    decision is made once on the full study distribution so it stays stable
    across filtered views (a filtered subset must never flip the axis).

    The test is the multiplicative spread of the positive values: the ratio of
    their 99th to 10th percentile. Negatives disqualify a log scale outright.

    Args:
        series: Numeric column values (NaNs allowed).

    Returns:
        True when a log10 display scale is appropriate.
    """
    s = series.dropna()
    if s.empty or float(s.min()) < 0:
        return False
    positive = s[s > 0]
    if len(positive) < 50:
        return False
    lo = float(positive.quantile(0.10))
    hi = float(positive.quantile(0.99))
    if lo <= 0:
        return False
    return (hi / lo) >= LOG_SCALE_SPREAD_THRESHOLD




def derive_bin_count(use_log: bool) -> int:
    """Return the histogram bin target for a numeric column.

    Heavy-tailed (log-scaled) columns get a finer grid so the long tail keeps
    resolution after the adaptive merge; bounded columns get a coarser grid.

    Args:
        use_log: Whether the column uses a log10 display scale.

    Returns:
        The target bin count fed to :func:`calculate_adaptive_histogram`.
    """
    return 50 if use_log else 10






def column_value_counts(series: pd.Series, dtype: str, date_like: bool = False) -> list[tuple[str, int]]:
    """Return every (value, count) pair for a filterable column, most frequent first.

    Includes single-occurrence values: :func:`get_metadata` applies its own
    count>1 drop and top-200 cap for the dropdown, while the per-variable
    value-search endpoint deliberately keeps the full tail so rare values
    (e.g. an author with a single video) stay reachable.

    Values are stringified exactly as ``filter_dataframe`` compares them —
    date-like category columns normalise to the date part (``str[:10]``) and
    list elements go through ``str(x)`` with per-row deduplication
    (document frequency).

    Args:
        series: The raw column values.
        dtype: The classified column type (``"category"`` or ``"list"``).
        date_like: For category columns, force the date-part normalisation
            (datetime-typed series are normalised regardless).

    Returns:
        List of ``(value, count)`` tuples sorted by descending count.
    """
    if dtype == "list":
        all_items = []
        for row in series.dropna():
            if isinstance(row, (list, np.ndarray)):
                # Deduplicate within row to count Document Frequency (rows
                # with the tag) instead of Term Frequency
                try:
                    all_items.extend(list(set(str(x) for x in row)))
                except Exception:
                    pass
        return [(str(k), int(v)) for k, v in Counter(all_items).most_common()]

    col_data = series
    if pd.api.types.is_datetime64_any_dtype(series) or date_like:
        col_data = series.astype(str).str[:10]
    return [(str(k), int(v)) for k, v in col_data.value_counts().items()]






def get_metadata(df, column_types, verbose=False):
    """
    Returns metadata for frontend:
    - columns: { name: type }
    - stats: min/max for numbers, unique values for categories, null_counts
    """
    t1 = _dt.datetime.now()
    if verbose:
        print("    Calculating things for viewer and explorer...")
    metadata = {}
    for col, dtype in column_types.items():
        # Calculate Null Count
        null_count = int(df[col].isna().sum())

        base_meta = {
            "null_count": null_count
        }

        # Special case: `extra_data` is a folded comma-separated record of
        # engagement activities (fave / comment / share / follow / save).
        # Expose it as a list-typed filter whose values are the known
        # engagement tokens with document-frequency counts.
        if col == 'extra_data':
            counts = {t: 0 for t in ENGAGEMENT_TYPES}
            for cell in df[col].dropna():
                for t in parse_extra_data_tokens(cell):
                    if t in counts:
                        counts[t] += 1
            items_list = [{"value": t, "count": counts[t]}
                          for t in ENGAGEMENT_TYPES if counts[t] > 0]
            base_meta.update({
                "type": "list",
                "values": items_list,
                "total_unique": len(items_list),
            })
            metadata[col] = base_meta
            continue

        if dtype == "number":
            min_val, max_val, max_capped = get_robust_bounds(df[col])
            # Log scale and bin count are derived from the full-study
            # distribution here, the single place they are decided, so every
            # consumer (sliders, density histogram, timeline) reads one stable
            # answer rather than recomputing on a filtered subset.
            log_flag = derive_log_scale(df[col])
            base_meta.update({
                "type": "number",
                "min": min_val,
                "max": max_val,
                "max_capped": max_capped,
                "log": log_flag,
                "bins": derive_bin_count(log_flag),
                "log_offset": log_axis_offset(df[col]),
                "quantiles": slider_quantiles(df[col])
            })
            metadata[col] = base_meta
        elif dtype == "category":
            # Limit for UI filters — show top 200 most frequent values.
            # Drop single-occurrence labels — filtering to one yields a 1-video
            # slice (noise in small studies). The frontend hides a column once
            # ≤1 selectable value remains. total_unique counts only selectable
            # (count>1) values so the "showing top X of Y" notice stays honest.
            pairs = column_value_counts(df[col], "category",
                                        date_like="date" in col.lower())
            multi = [(k, v) for k, v in pairs if v > 1]
            total_unique = len(multi)

            unique_vals = [{"value": k, "count": v} for k, v in multi[:200]]

            base_meta.update({
                "type": "category",
                "values": unique_vals,
                "total_unique": total_unique
            })
            metadata[col] = base_meta

        elif dtype == "list":
            # Document-frequency counts over list elements, dropping
            # single-occurrence tags (see the category note above).
            pairs = column_value_counts(df[col], "list")
            multi = [(k, v) for k, v in pairs if v > 1]
            total_unique = len(multi)

            items_list = [{"value": k, "count": v} for k, v in multi[:200]]

            base_meta.update({
                "type": "list",
                "values": items_list,
                "total_unique": total_unique
            })
            metadata[col] = base_meta
        
        # Explicitly ignore long_text and identifier
        elif dtype in ["long_text", "identifier"]:
            continue
    
    if verbose:
        print(f"    ...done calculating things for viewer and explorer. Time: {_dt.datetime.now()-t1}")
    return metadata





def search_columns(column_types: dict, *queries) -> set:
    """Columns a free-text search over ``queries`` actually scans.

    Mirrors :func:`filter_dataframe`'s Global Search block: every
    category/long_text/list column, plus the number columns only when some
    comma-separated term looks numeric. Used by the routes to project a
    search request's frame instead of falling back to the full width.
    """
    wanted = {c for c, t in (column_types or {}).items()
              if t in ("category", "long_text", "list")}
    terms = [t.strip() for q in queries if isinstance(q, str)
             for t in q.split(",") if t.strip()]
    if any(t.replace('.', '', 1).isdigit() for t in terms):
        wanted |= {c for c, t in (column_types or {}).items() if t == "number"}
    return wanted






def _arrow_values(series: pd.Series):
    """The PyArrow ChunkedArray behind an Arrow-backed Series, else ``None``.

    Columns read from the recoded parquets are Arrow-backed, so the search
    kernels below can work straight off their buffers. A column added at
    request time on numpy storage returns ``None`` and takes the pandas path.
    """
    return getattr(getattr(series, "array", None), "_pa_array", None)






def _list_column_search_mask(values, term: str) -> np.ndarray:
    """Rows of a list-of-string column having any element that contains ``term``.

    ``list_parent_indices`` numbers rows within its own chunk, so the running
    ``base`` offset restores whole-column positions. Nothing is concatenated
    or joined: a 600 MB list column is matched where it already lies.

    Args:
        values: Arrow ChunkedArray of a list-of-string column.
        term: Lowercased search term; matching is case-insensitive.

    Returns:
        Positional boolean mask, one entry per row of ``values``.
    """
    mask = np.zeros(len(values), dtype=bool)
    base = 0
    for chunk in values.chunks:
        if len(chunk):
            hits = pc.fill_null(
                pc.match_substring(chunk.flatten(), term, ignore_case=True),
                False)
            parents = np.asarray(pc.list_parent_indices(chunk))
            mask[base + parents[np.asarray(hits)]] = True
        base += len(chunk)
    return mask






def _column_search_mask(series: pd.Series, term: str) -> np.ndarray:
    """Positional mask of rows whose value contains ``term``, case-insensitively.

    ``match_substring`` is the same Arrow kernel pandas' ``str.contains(
    case=False)`` dispatches to, but reaching it through the ``.str`` accessor
    first casts the column to pandas' own string dtype — a full copy of every
    byte with offsets widened 4 -> 8 bytes per row. A global search sweeps 78
    of this schema's 119 columns, and holding a copy of each one at once
    exhausted a 16 GiB instance on a 2.4M-row study (2026-08-17). Matching in
    place allocates only the mask.

    Columns Arrow cannot match as text directly — timestamps, booleans, and
    the number columns a numeric term adds — still take that cast, one column
    at a time, released as soon as its mask is built.

    Args:
        series: One column of the frame being filtered.
        term: Lowercased search term.

    Returns:
        Positional boolean mask, one entry per row of ``series``.
    """
    values = _arrow_values(series)
    if values is not None:
        pa_type = values.type
        if pa.types.is_string(pa_type) or pa.types.is_large_string(pa_type):
            hits = pc.fill_null(
                pc.match_substring(values, term, ignore_case=True), False)
            return np.asarray(hits.to_numpy(zero_copy_only=False), dtype=bool)

        is_list = pa.types.is_list(pa_type) or pa.types.is_large_list(pa_type)
        if is_list and (pa.types.is_string(pa_type.value_type)
                        or pa.types.is_large_string(pa_type.value_type)):
            if " " in term:
                # Elements used to be joined with a space before matching, so a
                # term spanning two of them matched. Preserve that by joining
                # only for the terms that can span — the join is transient and
                # this column alone, not the corpus.
                hits = pc.fill_null(
                    pc.match_substring(pc.binary_join(values, " "), term,
                                       ignore_case=True), False)
                return np.asarray(hits.to_numpy(zero_copy_only=False),
                                  dtype=bool)
            return _list_column_search_mask(values, term)

    return series.astype("string[pyarrow]").str.contains(
        term, case=False, regex=False, na=False).to_numpy(dtype=bool)






def filter_dataframe(df, column_types, filters, search_query=None):

    # All vectorized criteria are collected as positional boolean masks over
    # the FULL frame and AND-ed before a single row selection materialises the
    # result. The old shape — `filtered_df = filtered_df[mask]` once per
    # criterion — re-materialised every projected column for every active
    # filter, so each added checkbox cost another full-frame copy on a
    # multi-million-row study. Python-per-row criteria (list overlap,
    # extra_data token overlap) are deferred until after that single
    # narrowing, so they scan surviving rows only, as before.
    combined = None   # np.bool_ mask over df; None = no vectorized criteria
    deferred = []     # (col, val) pairs evaluated per-row after narrowing

    def _and_mask(mask):
        nonlocal combined
        if isinstance(mask, pd.Series):
            # Arrow-backed comparisons can carry NA; a row with NA never
            # matches a criterion (same outcome the old per-step indexing had
            # for NaN comparisons).
            mask = mask.fillna(False).to_numpy(dtype=bool)
        combined = mask if combined is None else (combined & mask)

    for col, criteria in filters.items():
        # Handle virtual Collection Tags filter
        if col == 'Collection Tags':
            val = criteria.get("value")
            if isinstance(val, (list, np.ndarray)) and len(val) > 0 and 'collection_id' in df.columns:
                try:
                    # Lazy import to avoid circular dependency with data_service
                    from .data_service import get_collection_tags
                    annotations = get_collection_tags()
                except Exception:
                    annotations = {}
                selected_tags = set(str(v) for v in val)
                matching_cids = set()
                for cid, anno in annotations.items():
                    anno_tags = set(str(t).strip() for t in anno.get('annotation_tags', []))
                    if anno_tags & selected_tags:
                        matching_cids.add(str(cid))
                _and_mask(df['collection_id'].astype(str).isin(matching_cids))
            continue

        if col not in df.columns:
            continue

        val = criteria.get("value")
        # If no value criteria, skip
        if val is None or val == "":
            continue

        # Special case: token-overlap filter on the folded `extra_data`
        # engagement record. `val` is a list of selected engagement types
        # (e.g. ['fave', 'comment']); a row passes if any of those tokens
        # appears in its parsed `extra_data` cell. Python per row — deferred.
        if col == 'extra_data':
            if isinstance(val, (list, np.ndarray)) and len(val) > 0:
                deferred.append((col, val))
            continue

        dtype = column_types.get(col)

        if dtype == "number":
            # Robustness: If frontend sends a list (checkboxes) for a numeric column (e.g. bools, discrete ints),
            # treat it as a categorical "isin" filter.
            if isinstance(val, (list, np.ndarray)):
                _and_mask(df[col].astype(str).isin([str(v) for v in val]))
            else:
                min_val = val.get("min") if val else None
                max_val = val.get("max") if val else None
                if min_val is not None:
                    _and_mask(df[col] >= float(min_val))
                if max_val is not None:
                    _and_mask(df[col] <= float(max_val))

        elif dtype == "category":
            if isinstance(val, (list, np.ndarray)) and len(val) > 0:
                is_dt = pd.api.types.is_datetime64_any_dtype(df[col])
                if is_dt or "date" in col.lower():
                    _and_mask(df[col].astype(str).str[:10].isin([str(v)[:10] for v in val]))
                else:
                    col_series = df[col]
                    col_dtype = col_series.dtype
                    if (isinstance(col_dtype, pd.ArrowDtype)
                            and (pa.types.is_string(col_dtype.pyarrow_dtype)
                                 or pa.types.is_large_string(col_dtype.pyarrow_dtype))):
                        # Arrow string columns compare in-place — astype(str)
                        # would round-trip every value through a python object
                        # (seconds per filter on a multi-million-row study).
                        # Non-string selections can never match the old
                        # astype(str) semantics anyway, so drop them here.
                        str_vals = [v for v in val if isinstance(v, str)]
                        _and_mask(col_series.isin(str_vals).fillna(False).to_numpy(dtype=bool))
                    else:
                        _and_mask(col_series.astype(str).isin(val))

        elif dtype == "list":
            if isinstance(val, (list, np.ndarray)) and len(val) > 0:
                deferred.append((col, val))

    # ONE row selection for every vectorized criterion. A shallow copy when
    # nothing narrowed keeps the no-filter path allocation-free (the caller's
    # frame is never mutated either way).
    if combined is not None:
        filtered_df = df[combined]
    else:
        filtered_df = df.copy(deep=False)

    # Python-per-row criteria on the already-narrowed frame.
    for col, val in deferred:
        if col == 'extra_data':
            selected = set(str(v).lower() for v in val)
            mask = filtered_df[col].astype('string').map(
                lambda s: bool(parse_extra_data_tokens(s) & selected)
                if pd.notna(s) else False
            )
            filtered_df = filtered_df[mask]
            continue

        search_set = set(str(v) for v in val)  # Ensure strings

        def robust_check(x):
            if not isinstance(x, (list, np.ndarray)): return False
            try:
                # Ensure x items are also hashable/strings
                check_set = set(str(item) for item in x)
                return bool(check_set & search_set)
            except:
                return False

        value_mask = filtered_df[col].apply(robust_check)
        filtered_df = filtered_df[value_mask]

    # Global Search Logic
    if search_query and isinstance(search_query, str):
        terms = [t.strip().lower() for t in search_query.split(",") if t.strip()]
        if terms:
            # We want rows where ALL terms appear ANYWHERE in the row.
            # Masks are positional throughout: each one is built from this
            # frame's own columns in row order, so the selection at the end
            # needs no index alignment.
            n_rows = len(filtered_df)
            final_mask = np.ones(n_rows, dtype=bool)

            # Data-driven searchable set: every string/collection column except
            # identifiers. ``classify_columns`` already separates opaque IDs (huge
            # ints, URLs, >90%-unique strings → "identifier") from human-readable
            # text/categorical/list fields, so free-text search targets the latter
            # and never matches inside IDs, hashes, or storage links.
            searchable_cols = [
                col for col in filtered_df.columns
                if column_types.get(col) in ("category", "long_text", "list")
            ]
            
            for term in terms:
                term_mask = np.zeros(n_rows, dtype=bool)
                term_is_numeric = term.replace('.', '', 1).isdigit()

                cols_to_search = searchable_cols.copy()
                if term_is_numeric:
                    # Add numeric columns if the term looks like a number
                    for col in filtered_df.columns:
                        if column_types.get(col) == "number":
                            cols_to_search.append(col)

                for col in cols_to_search:
                    try:
                        term_mask |= _column_search_mask(filtered_df[col], term)
                    except Exception:
                        continue
                final_mask &= term_mask

            filtered_df = filtered_df[final_mask]

    return filtered_df





def calculate_adaptive_histogram(data, min_val, max_val, bins=50, max_empty_ratio=0.1):
    """
    Recursively reduces bin count if too many bins are empty.
    """
    counts, bin_edges = np.histogram(data, bins=bins, range=(min_val, max_val), density=True)
    
    # Check emptiness
    # Count bins with 0 data
    empty_bins = np.sum(counts == 0)
    empty_ratio = empty_bins / bins
    
    if empty_ratio > max_empty_ratio and bins > 5:
        # Reduce bins by ~50% (Agilent approach)
        new_bins = int(bins * 0.5)
        # Ensure we don't get stuck if bins * 0.5 rounds to same int (unlikely with >5)
        if new_bins == bins: new_bins -= 1
        return calculate_adaptive_histogram(data, min_val, max_val, bins=new_bins, max_empty_ratio=max_empty_ratio)
    
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    return counts, bin_centers









def make_serializable(obj):
    """Helper to convert non-JSON-serializable types."""

    if obj is None:
        return None
        
    # Check for containers FIRST to avoid pd.isna() returning an array
    if isinstance(obj, dict):
        return {str(k): make_serializable(v) for k, v in obj.items()}

    if isinstance(obj, np.ndarray):
        return [make_serializable(x) for x in obj.tolist()]
        
    if isinstance(obj, (list, tuple)):
        return [make_serializable(x) for x in obj]
    
    # Check for scalar NAs (NaN, NaT, None)
    # This is safe now because we've handled most containers
    try:
        if pd.isna(obj):
            return None
    except:
        pass

    if isinstance(obj, (pd.Timestamp, _dt.datetime)):
        return obj.isoformat()
        
    if hasattr(obj, 'tolist'):  # generic numpy scalar fallback
        return obj.tolist()
        
    return obj


def classify_columns(df: pd.DataFrame) -> dict:
    """Classify DataFrame columns into type categories based on Arrow dtypes and heuristics.

    Returns:
        Dict mapping column name to type string: 'list', 'number', 'identifier', 'long_text', or 'category'.
    """
    column_types = {}

    for col in df.columns:
        dtype = df[col].dtype

        # 1. Check for Lists (List, LargeList, FixedSizeList)
        is_list = False
        if isinstance(dtype, pd.ArrowDtype):
            pa_type = dtype.pyarrow_dtype
            if (pa.types.is_list(pa_type) or
                pa.types.is_large_list(pa_type) or
                pa.types.is_fixed_size_list(pa_type)):
                is_list = True

        if is_list:
            column_types[col] = "list"
            continue

        # 2. Check for Numbers (Integers, Floats)
        # We explicitly exclude booleans from "number" to treat them as categorical/other
        is_numeric = False
        is_bool = False

        if isinstance(dtype, pd.ArrowDtype):
            pa_type = dtype.pyarrow_dtype
            if pa.types.is_boolean(pa_type):
                is_bool = True
            elif pa.types.is_integer(pa_type) or pa.types.is_floating(pa_type):
                is_numeric = True
        else:
            # Fallback for numpy dtypes (though we expect Arrow)
            if pd.api.types.is_bool_dtype(dtype):
                is_bool = True
            elif pd.api.types.is_numeric_dtype(dtype):
                is_numeric = True

        if is_numeric:
             try:
                 max_val = df[col].max()
                 if pd.isna(max_val):
                     column_types[col] = "number"
                 elif max_val > 1e15:
                     column_types[col] = "identifier"
                 else:
                     column_types[col] = "number"
             except:
                 column_types[col] = "number"
             continue

        # 3. Strings / Categories
        # Boolean also falls through here to be treated as category (heuristic)

        # Check for Long Text / Category / Identifier
        # We still use data-based heuristics for this distinction as Arrow
        # string type is generic. The non-null count comes from Arrow's O(1)
        # null_count instead of a full dropna() materialisation — on a
        # multi-million-row study the old two-dropna()-plus-pandas-nunique
        # shape cost seconds PER string column at load time.
        col_series = df[col]
        if isinstance(dtype, pd.ArrowDtype):
            n_rows = len(col_series) - col_series.array._pa_array.null_count
        else:
            n_rows = int(col_series.notna().sum())

        # Sample up to 1000 non-null values from the head slice; only a column
        # whose first chunk is all-null needs the full-column fallback.
        series_sample = col_series.head(100_000).dropna()
        if series_sample.empty and n_rows > 0:
            series_sample = col_series.dropna()
        series_sample = series_sample.head(1000)

        series_sample = series_sample[series_sample != fyp_cf['labels']['OTHER_THINGS']]

        if series_sample.empty:
            column_types[col] = "category"
            continue

        lengths = series_sample.astype(str).str.len()
        lengths = lengths[lengths > 0]

        if not lengths.empty and lengths.mean() > 60:
             column_types[col] = "long_text"
        else:
             if n_rows > 100:
                 try:
                     # Arrow-native distinct count (C, multithreaded); NAs are
                     # excluded to match pandas nunique(dropna=True).
                     arr = col_series.array._pa_array
                     n_unique = pc.count_distinct(arr, mode="only_valid").as_py()
                 except Exception:
                     n_unique = col_series.nunique()
                 if n_unique > 0.9 * n_rows:
                     column_types[col] = "identifier"
                 else:
                     column_types[col] = "category"
             else:
                 column_types[col] = "category"

    return column_types


def load_data(study: str, verbose: bool = False):

    if verbose:
        print("    Loading study data for viewer/explorer...")
    df = None

    if not data_io.exists(
        storage_location = "cache",
        filename = f"{study}_recoded.parquet",
        verbose=verbose
        ):
        # Cold-start path: only attempt to build the recoded dataset for a
        # study that actually exists in the config. An unknown name would
        # otherwise crash deep inside create_study_recoded_dataset and
        # surface as a 500 in every route that calls get_explorer_data.
        study_defs = fyp_cf.get("study_defs", {}) or {}
        if study not in study_defs:
            if verbose:
                print(f"    Study '{study}' is not defined in config; nothing to load.")
            return None, {}

        print("@@ No cached recoded study dataset found. I must run the recoding process to create it. Please wait a moment...")
        df = create_study_recoded_dataset(
            study_name = study,
            save_to_cache=True,
            verbose = verbose
        )
        print("@@ Back after finalising the recoding process.")
    else:
        df = data_io.load_parquet(
            storage_location="cache",
            filename=f"{study}_recoded.parquet",
            verbose=verbose,
            )

    if df is None:
        print("ERROR: This process cannot run without a study dataset. Process failed.")
        return None, {}

    column_types = classify_columns(df)

    return df, column_types





def _list_value_counts_top(col_data: pd.Series, n: int = 20) -> dict:
    """Top-``n`` value counts for an Arrow-backed list column, Arrow-native.

    The pandas route (``explode().value_counts()``) round-trips every element
    through Python objects and dominates the stats cost on large studies.
    ``pc.list_flatten`` + ``pc.value_counts`` do the same computation inside
    Arrow. Ties at the cut-off may order differently than pandas (both are
    stable sorts over their own internal ordering), which is acceptable for a
    top-20 display. Raises on any shape surprise — the caller falls back to
    the pandas path.

    Args:
        col_data: Arrow-backed Series with a list dtype.
        n: Number of most frequent values to return.

    Returns:
        Dict of value -> count, most frequent first.
    """
    import pyarrow.compute as pc

    chunked = col_data.array._pa_array
    flat = pc.list_flatten(chunked)
    flat = flat.drop_null()
    if len(flat) == 0:
        return {}
    vc = pc.value_counts(flat.combine_chunks())
    counts = vc.field('counts').to_numpy()
    order = np.argsort(-counts, kind='stable')[:n]
    top_values = vc.field('values').take(pa.array(order)).to_pylist()
    top_counts = counts[order]
    return {str(v): int(c) for v, c in zip(top_values, top_counts)}


def get_current_stats(df, column_types, number_meta=None, verbose=False):
    """Build per-column display stats (density histograms for numbers).

    Args:
        df: The (possibly filtered) frame to summarise.
        column_types: Mapping of column name to classified type.
        number_meta: Per-column metadata from :func:`get_metadata`, built on the
            FULL study frame. Supplies the canonical ``log`` / ``bins`` decision
            so a filtered view never flips a column's axis or bin count.
        verbose: When True, print timing.
    """
    pd.set_option('future.no_silent_downcasting', True)

    t1 = _dt.datetime.now()

    if verbose:
        print("    Calculating stats for viewer and explorer...")

    count = len(df)
    stats = {}
    if number_meta is None: number_meta = {}

    if count == 0:
        return {"count": 0, "stats": {}}

    def _column_stats(col, dtype):
        """Stats value for one column, or ``None`` for types with no stats.

        Pure per-column read of ``df`` — safe to run concurrently for
        different columns (no shared mutable state; Arrow/numpy kernels
        release the GIL, which is what makes the thread pool below pay off).
        """
        if dtype == "number":
             col_data = df[col]

             if pd.api.types.is_integer_dtype(col_data):
                  if col_data.nunique() < 20:
                      vc = col_data.value_counts().sort_index().to_dict()
                      return {str(k): v for k, v in vc.items()}

             series = col_data.dropna()
             series = series[series >= 0]

             if series.empty:
                 return {"type": "density", "x": [], "y": []}

             count_val = len(series)
             # std() of a single-value series is undefined and returns NA on a
             # PyArrow-backed column, so float(NA) raises TypeError — guard it
             # (and the other reducers) so filtering down to one video doesn't
             # 500 the Explore tab.
             _std = series.std()
             mean_val = float(series.mean())
             std_val = float(_std) if pd.notna(_std) else 0.0
             min_val = float(series.min())
             max_val = float(series.max())
             
             col_meta = number_meta.get(col, {})
             use_log = bool(col_meta.get('log'))
             transform = "log10" if use_log else "linear"

             clamped_series = series
             log_offset = log_axis_offset(clamped_series) if use_log else 1.0

             try:
                 if min_val == max_val:
                     x_val = np.log10(min_val + log_offset) if transform == "log10" else min_val
                     return {
                        "type": "density",
                        "x": [float(x_val)],
                        "y": [float(count_val)],
                        "transform": transform,
                        "log_offset": float(log_offset),
                        "min": min_val,
                        "max": max_val,
                        "mean": mean_val,
                        "std": std_val,
                        "count": count_val
                    }

                 bins_target = col_meta.get('bins')
                 if not isinstance(bins_target, int) or bins_target <= 0:
                     bins_target = derive_bin_count(use_log)


                 if transform == "log10":
                     # Log Transform: log10(x + offset). The offset is 1 for
                     # count-like data and data-driven for fractional data
                     # (see log_axis_offset).
                     if isinstance(clamped_series.dtype, pd.ArrowDtype):
                         log_data = np.log10(clamped_series.to_numpy() + log_offset)
                     else:
                         log_data = np.log10(clamped_series + log_offset)

                     log_min = np.log10(min_val + log_offset)
                     log_max = np.log10(max_val + log_offset)

                     counts, bin_centers = calculate_adaptive_histogram(log_data, log_min, log_max, bins=bins_target)

                     # Decade ticks labelled in ORIGINAL units. Zeros sit at
                     # log10(offset), so the "0" tick marks that position.
                     tick_vals = []
                     tick_text = []
                     if min_val <= 0 and max_val >= 0:
                        tick_vals.append(np.log10(log_offset))
                        tick_text.append("0")
                     # Build decade ticks only for a finite range, so a non-finite
                     # max_val can never spin this loop forever. Start at the
                     # offset's decade so sub-1 ranges (e.g. per-play ratios)
                     # still get labelled ticks.
                     if np.isfinite(max_val) and max_val > 0:
                         p = int(np.ceil(np.log10(log_offset)))
                         while 10**p <= max_val:
                            v = 10**p
                            if v >= min_val:
                                tick_vals.append(np.log10(v + log_offset))
                                tick_text.append(f"{v:,}" if v >= 1 else f"{v:.{-p}f}")
                            p += 1

                     return {
                        "type": "density",
                        "x": bin_centers.tolist(),
                        "y": counts.tolist(),
                        "transform": transform,
                        "log_offset": float(log_offset),
                        "min": min_val,
                        "max": max_val,
                        "tick_vals": tick_vals,
                        "tick_text": tick_text,
                        "mean": mean_val,
                        "std": std_val,
                        "count": count_val
                     }

                 else:
                     arr_data = clamped_series.to_numpy()

                     counts, bin_centers = calculate_adaptive_histogram(arr_data, min_val, max_val, bins=bins_target)


                     return {
                        "type": "density",
                        "x": bin_centers.tolist(),
                        "y": counts.tolist(),
                        "transform": transform,
                        "min": min_val,
                        "max": max_val,
                        "mean": mean_val,
                        "std": std_val,
                        "count": count_val
                     }

             except Exception as e:
                 print(f"Error stats {col}: {e}")
                 return {}

        elif dtype == "category":
            return df[col].value_counts().head(20).to_dict()

        elif dtype == "list":
             if isinstance(df[col].dtype, pd.ArrowDtype) and 'list' in str(df[col].dtype):
                  try:
                      return _list_value_counts_top(df[col], n=20)
                  except Exception:
                      pass
                  try:
                      exploded = df[col].explode().dropna()
                      return exploded.value_counts().head(20).to_dict()
                  except:
                      pass

             all_items = []
             s = df[col].dropna()
             for row in s:
                  if isinstance(row, (list, np.ndarray)):
                      # Deduplicate within row to count Document Frequency
                      try:
                          all_items.extend(list(set(str(x) for x in row)))
                      except:
                          pass
             return dict(Counter(all_items).most_common(20))

        return None

    # Columns are independent; on a big frame run them across a small pool.
    # Arrow value_counts and the numpy histogram/log kernels release the GIL,
    # so this scales with the container's CPUs (prod hub: 4 vCPU) instead of
    # summing ~25 sequential per-column passes. Small frames stay sequential —
    # pool overhead would dominate.
    items = list(column_types.items())
    if count >= 200_000 and len(items) >= 4:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda kv: _column_stats(*kv), items))
    else:
        results = [_column_stats(*kv) for kv in items]
    for (col, _), value in zip(items, results):
        if value is not None:
            stats[col] = value

    if verbose:
        print(f"    ...done calculating stats for viewer and explorer. Time: {_dt.datetime.now()-t1}")


    return {"count": count, "stats": stats}
