import json

import pandas as pd
import pyarrow as pa


def fix_surrogates(text):
    if not isinstance(text, str):
        return text
    # This trick encodes surrogates to UTF-16 and decodes them correctly
    return text.encode('utf-16', 'surrogatepass').decode('utf-16', 'replace')





def downgrade_arrow_type(pa_type: pa.DataType) -> pa.DataType:
    """Rewrite a pyarrow type to use non-``large_*`` variants recursively.

    Polars writes parquet with ``large_string`` / ``large_list`` types,
    and its pandas bridge produces the same. Pandas 2.2.x has partial
    kernel coverage for the large variants — most importantly
    ``DataFrame.explode()`` is a silent no-op on ``large_list`` columns
    (works correctly on plain ``list``), and ``dictionary_encode``
    (called by ``factorize``, ``Categorical``, ``crosstab``, etc.) has
    no kernel for ``large_list``. The two sizes are semantically
    identical at the logical level; only the offset width differs
    (int64 vs int32). Downgrading restores every pandas op that breaks
    on the large variants, at no semantic cost when values fit.
    """
    if pa.types.is_large_string(pa_type):
        return pa.string()
    if pa.types.is_large_binary(pa_type):
        return pa.binary()
    if pa.types.is_large_list(pa_type):
        return pa.list_(downgrade_arrow_type(pa_type.value_type))
    if pa.types.is_list(pa_type):
        inner = downgrade_arrow_type(pa_type.value_type)
        return pa_type if inner == pa_type.value_type else pa.list_(inner)
    if pa.types.is_struct(pa_type):
        fields = []
        changed = False
        for f in pa_type:
            new_t = downgrade_arrow_type(f.type)
            if new_t != f.type:
                changed = True
            fields.append(pa.field(f.name, new_t, nullable=f.nullable))
        return pa.struct(fields) if changed else pa_type
    return pa_type





def downgrade_series_if_large(series: pd.Series) -> pd.Series:
    """Cast a pyarrow-backed Series from ``large_*`` variants to the
    regular variants, preserving values and null mask.

    Returns the original series when it is not ArrowDtype or when the
    cast is a no-op. Also returns the original if the cast would
    overflow (rare: > 2 GB of total string bytes / > 2 billion list
    elements). See :func:`downgrade_arrow_type` for why this matters.
    """
    if not isinstance(series.dtype, pd.ArrowDtype):
        return series
    current = series.dtype.pyarrow_dtype
    target = downgrade_arrow_type(current)
    if target == current:
        return series
    try:
        arr = series.array._pa_array.cast(target)
        return pd.Series(
            pd.arrays.ArrowExtensionArray(arr),
            index=series.index,
            name=series.name,
        )
    except Exception:
        return series





def downgrade_large_arrow_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Apply :func:`downgrade_series_if_large` to every column in ``df``.

    Returns a new DataFrame only if any column actually changed — otherwise
    the input is returned unchanged to avoid an unnecessary copy.
    """
    new_cols = None
    for col in df.columns:
        orig = df[col]
        new = downgrade_series_if_large(orig)
        if new is not orig:
            if new_cols is None:
                new_cols = {c: df[c] for c in df.columns}
            new_cols[col] = new
    if new_cols is None:
        return df
    return pd.DataFrame(new_cols, index=df.index)[list(df.columns)]




def fix_complex_types(some_iterable, verbose=False):

    if not len(some_iterable.shape) == 1:
        raise ValueError("Input must be a 1D iterable")

    if verbose:
        print("    [PYARROW dtypes - complex] Starting special treatment of complex types...")
        print("    [PYARROW dtypes - complex] Input iterable length:", some_iterable.shape[0])

    some_iterable[some_iterable.isna()] = pd.NA

    def _get_type_counts(series):
        return series.dropna().map(type).value_counts()

    def _display_types(type_counts):
        return " | ".join(f"{t.__name__.upper()}: {c}" for t, c in type_counts.items())

    type_counts = _get_type_counts(some_iterable)

    if verbose:
        print("    [PYARROW dtypes - complex] Type counts:", _display_types(type_counts))

    # Convert dicts to JSON strings
    if dict in type_counts.index:
        mask = some_iterable.dropna().map(lambda x: isinstance(x, dict))
        some_iterable.loc[mask[mask].index] = some_iterable.loc[mask[mask].index].map(json.dumps)

        type_counts = _get_type_counts(some_iterable)

        if verbose:
            print("    [PYARROW dtypes - complex] Dicts converted to json strings")
            print("    [PYARROW dtypes - complex] Type counts after dict conversion:", _display_types(type_counts))

    # Validate and normalise list elements
    if list in type_counts.index:
        row_types = some_iterable.dropna().map(type)
        list_indeces = row_types[row_types == list].index
        element_types = list({type(j) for i in list_indeces for j in some_iterable.at[i]})

        if verbose:
            print("    [PYARROW dtypes - complex] Element types in lists:", " | ".join(str(t) for t in element_types))

        if len(element_types) > 1:
            raise ValueError("Lists in the iterable contains elements of different types")

        if len(element_types) == 1 and element_types[0] in (list, dict):
            if verbose:
                print(f"    [PYARROW dtypes - complex] Lists in the iterable contains elements of type {element_types[0]} - converting to json strings")
            for i in list_indeces:
                some_iterable.at[i] = [json.dumps(j) for j in some_iterable.at[i]]

    # Single type — done
    if len(type_counts) == 1:
        if verbose:
            print("    [PYARROW dtypes - complex] All rows in the iterable is of the same type")
        return some_iterable

    # Mixed types with lists — coerce scalars into single-element lists
    if list in type_counts.index:
        row_types = some_iterable.dropna().map(type)
        nonlist_indeces = row_types[row_types != list].index
        target_type = element_types[0]

        try:
            some_iterable.loc[nonlist_indeces] = some_iterable.loc[nonlist_indeces].map(lambda x: [target_type(x)])
        except Exception:
            if verbose:
                print(f"    [PYARROW dtypes - complex] Failed to convert non-list elements to lists and type {target_type}. Trying one row at a time")
            for i in nonlist_indeces:
                try:
                    some_iterable.at[i] = [target_type(some_iterable.at[i])]
                except Exception:
                    if verbose:
                        print(f"    [PYARROW dtypes - complex] Failed to convert row {i} to list and type {target_type}. Setting to pd.NA")
                    some_iterable.at[i] = pd.NA

        if verbose:
            print("    [PYARROW dtypes - complex] Non-list elements converted to lists")

        return some_iterable

    # Mixed types with strings — coerce everything to pyarrow string
    if str in type_counts.index:
        if verbose:
            print("    [PYARROW dtypes - complex] Multiple types, one is 'str' - converting all to pyarrow strings")
        some_iterable = some_iterable.astype('string[pyarrow]')

    return some_iterable



def convert_index_dtype_pyarrow(an_index):

    # Handle MultiIndex recursively
    if isinstance(an_index, pd.MultiIndex):
        new_levels = [convert_index_dtype_pyarrow(lvl) for lvl in an_index.levels]
        return an_index.set_levels(new_levels)

    # Use Series.convert_dtypes to handle int, float, string, datetime, etc.
    # robustly mapping to pyarrow backends.
    name = an_index.name

    # Convert to Series to access convert_dtypes
    s = pd.Series(an_index)

    # Attempt optimistic pyarrow conversion
    s_pa = s.convert_dtypes(dtype_backend="pyarrow")
    
    # Reconstruct Index preserving name
    new_index = pd.Index(s_pa)
    new_index.name = name
    return new_index







def convert_dtypes_to_pyarrow(df_in, verbose=False):

    # Fast path: when every column is already a pyarrow ArrowDtype, the
    # pipeline below is a no-op. The expensive `.describe()` overflow check at
    # the end (~1.1s on `everything_recoded.parquet`) exists to catch int64
    # values >2^53 in object/numpy-backed columns, which cannot occur in data
    # already typed by pyarrow on disk. Skipping is safe and saves ~50% of
    # `load_parquet()` wall-clock on the largest cache files.
    if all(isinstance(d, pd.ArrowDtype) for d in df_in.dtypes):
        if verbose:
            print("    [PYARROW dtypes] All columns already ArrowDtype - skipping conversion.")
        # Even on the fast path we must downgrade any `large_*` arrow types
        # to their regular variants. Parquet files written by polars use
        # `large_list<large_string>`, and pandas 2.2.x has partial kernel
        # coverage there — notably `DataFrame.explode()` silently no-ops on
        # large_list columns. Downgrade is logically a no-op (same values,
        # int32 vs int64 offsets) so it's always safe.
        return downgrade_large_arrow_columns(df_in.copy())

    df = df_in.copy()

    # ---------------------------------------------------------
    # 1. OPTIMISTIC BATCH CONVERSION
    # ---------------------------------------------------------
    if verbose:
        print("    [PYARROW dtypes] Attempting batch conversion of DF dtype to pyarrow...")
    
    try:
        # This handles the vast majority of "easy" columns (int, float, clean strings)
        # much faster than iterating column by column.
        df = df.convert_dtypes(dtype_backend='pyarrow')
    except Exception as e:
        if verbose:
            print(f"    [PYARROW dtypes] Batch conversion failed ({e}). Falling back to column-wise checks.")

    # ---------------------------------------------------------
    # 2. IDENTIFY AND FIX PROBLEMATIC COLUMNS
    # ---------------------------------------------------------
    # We only need to spend time on columns that are STILL 'object' 
    # (meaning pyarrow couldn't natively handle them).
    # Note: convert_dtypes automatically converts objects to strings if possible.
    # If it fails/ambiguous, it leaves them as object.
    
    cols_to_check = [c for c in df.columns if df[c].dtype == "object"]

    if len(cols_to_check) > 0 and verbose:
        print(f"    [PYARROW dtypes] Refining {len(cols_to_check)} columns that failed simple batch conversion...")

    for col in cols_to_check:
        # A) Try explicit conversion (sometimes works individually if batch had a holistic issue, though rare)
        try:
            df[col] = df[col].convert_dtypes(dtype_backend='pyarrow')
        except:
            pass
        
        # If still object, it likely has issues (surrogates, mixed types, etc.)
        if df[col].dtype == "object":
            if verbose:
                print(f"    [PYARROW dtypes] {col} - Fixing surrogates")
            
            # B) Fix surrogates
            try:
                # We apply map only if necessary to save time, but safe to just apply
                df[col] = df[col].map(fix_surrogates)
                df[col] = df[col].convert_dtypes(dtype_backend='pyarrow')
            except Exception as e:
                if verbose:
                    print(f"    [PYARROW dtypes] {col} - ERROR:Surrogate fix didn't fully resolve ({e}).")

        # If STILL object, it's likely complex types (lists, dicts, etc.)
        if df[col].dtype == "object":
            if verbose:
                print(f"    [PYARROW dtypes] {col} is still object - sending it to special treatment of complex types...")
            try:
                # First, ensure contents are normalized (e.g. dicts -> json strings)
                df[col] = fix_complex_types(df[col].copy(), verbose=verbose)
                
                # Now, standard convert_dtypes often fails on lists of strings, leaving them as object.
                # We try to explicitly convert to a pyarrow array and back again.
                try:
                    
                    # Create pyarrow array from the series
                    # type_inference=True is default, but explicit casting can help if we know it's string
                    arrow_array = pa.array(df[col])
                    
                    # Check if the resulting array is actually a list type (or other complex type we want)
                    # If it's just 'string' or 'int', convert_dtypes would have likely caught it, 
                    # but if it's List<String>, convert_dtypes might miss it.
                    if pa.types.is_list(arrow_array.type) or pa.types.is_struct(arrow_array.type):
                         if verbose:
                             print(f"    [PYARROW dtypes] {col} - Explicitly converting to {arrow_array.type} via pyarrow.array...")
                         df[col] = pd.Series(
                             arrow_array, 
                             dtype=pd.ArrowDtype(arrow_array.type),
                             index=df[col].index
                         )
                    else:
                         # Fallback to standard convert_dtypes if it wasn't a complex arrow type
                         df[col] = df[col].convert_dtypes(dtype_backend='pyarrow')

                except Exception as e:
                    if verbose:
                        print(f"    [PYARROW dtypes] {col} - Explicit pyarrow Array conversion failed: {e}")
                    # Fallback to standard 
                    df[col] = df[col].convert_dtypes(dtype_backend='pyarrow')

            except Exception as e:
                # Last resort: if complex fix fails, force string conversion for anything not null
                if verbose: 
                    print(f"    [PYARROW dtypes] {col} - Failed to fix complex types: {e}. Forcing string conversion.")
                df[col] = df[col].astype("string[pyarrow]")
        
        if verbose and df[col].dtype != "object":
             print(f"    [PYARROW dtypes] {col} - Successfully converted to {df[col].dtype}")

    # ---------------------------------------------------------
    # 3. FINAL SAFETY CHECKS (NUMERICS)
    # ---------------------------------------------------------
    numeric_cols_to_check = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]

    if len(numeric_cols_to_check) > 0:
        # trying to calculate describe() to catch overflow issues (integers > 2^53)
        # that would be rejected by explicit float-casting in describe's percentile calc.
        try:
            if verbose:
                print(f"    [PYARROW dtypes] Found {len(numeric_cols_to_check)} numeric columns - checking all for overflows...")
            df[numeric_cols_to_check].describe()
        except Exception:
            if verbose:
                print("    [PYARROW dtypes] Failed to describe numeric columns in one go - checking each column:")

            # Iterate through all columns that claim to be numeric now
            for c in numeric_cols_to_check:
                try:
                    df[c].describe()
                except Exception as e:
                    if verbose:
                        print(f"    [PYARROW dtypes] WARNING: {e} | {c} doesn't work well as a number - converting to string")
                    df[c] = df[c].astype("string[pyarrow]")
        
    if verbose:
        print("    [PYARROW dtypes] ...conversion complete.")

    return df



