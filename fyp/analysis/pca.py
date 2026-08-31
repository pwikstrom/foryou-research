

import datetime as _dt
import time as _time
from collections.abc import Sequence
from typing import Literal, Union

import numpy as np
import pandas as pd
import pyarrow as pa
from scipy import sparse as scipy_sparse
from scipy.spatial.distance import pdist as scipy_pdist
from scipy.spatial.distance import squareform as scipy_squareform
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

import fyp.data_io as data_io
from fyp.logging_setup import get_logger
from fyp.organize_datasets import create_study_recoded_dataset
from fyp.recode_variables import (
    get_factors_and_features_from_var_schema,
    get_grouping_factors_from_var_schema,
)
from fyp.types import (
    convert_dtypes_to_pyarrow,
    convert_index_dtype_pyarrow,
    downgrade_series_if_large,
)

logger = get_logger(__name__)




def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf




# The column the PCA frame carries for each group's video count. Structural
# (computed per group at build time, attached after scaling so it never enters
# the PCA basis); its var_schema metadata is declared in the derived contract.
VIDEOS_WATCHED_COL = "videos_watched"


# Cardinality gate for the dense group x category crosstab. At or below this
# the historical dense path runs untouched (byte-identical outputs); above it
# the exact high-cardinality path applies: surviving categories are chosen
# against the TRUE total before any dense frame is built, and the
# full-distribution statistics (entropy, top1, PC interpretation) come from
# long-format/sparse computations instead of the dense frame. A free-text-ish
# categorical (main_activity, ~98k distinct values) made the dense crosstab
# 13,840 x 91,799 x 8 B = 10.2 GB — doubled by the astype(float) — which
# OOM-killed the 32 GB task runner on 2026-08-08/09.
DENSE_CATEGORY_LIMIT = 1000

# Degenerate backstop for the high-cardinality path: when NO category clears
# `drop_rare_globally_below`, keep this many most-frequent categories instead
# of all of them (the dense path's keep-everything fallback would rebuild the
# very matrix this path exists to avoid).
RARE_FALLBACK_TOP_N = 200


def contract_numeric_transforms() -> dict[str, str]:
    """Per-column pre-aggregation transforms declared in the contracts.

    A contract field may carry ``transform = "log1p"`` (heavy-tailed numerics:
    play_count, plays_per_day, days_since_created), applied to the row values
    before the group mean. Group-level declarations (``videos_watched``) are
    not row-aggregated, so their entries are ignored here.

    Returns:
        Mapping of column name → transform keyword.
    """
    out: dict[str, str] = {}
    for loader_name in ("scrape_contract", "activity_contract", "derived_contract"):
        try:
            import importlib

            loader = importlib.import_module(f"fyp.{loader_name}")
            contract = loader.load_contract()
        except Exception:
            continue
        for field in contract.get("fields", []):
            name, transform = field.get("name"), field.get("transform")
            if name and transform:
                out[name] = transform
    return out


# Yes/No(/Unclear) categoricals bypass the PCA entirely: their substantive
# content is the share of "yes", which a component would only obfuscate
# ("Advertising (C0)" is a z-scored yes-share wearing a costume). Detection is
# data-driven on the counts frame so any future yes/no field gets it for free.
YES_NO_UNCLEAR = frozenset({"yes", "no", "unclear"})

# Suffix of the plain day-share column such variables emit instead of C0/
# entropy. Never matches the component regex (_C\d+$), so the read-side
# component cap passes it through untouched, and the service's longest-prefix
# display naming renders "Display name (share of feed)".
SHARE_OF_FEED_SUFFIX = "_share_of_feed"






def _recode_sentinels() -> set:
    """Lowercased recode missing-value sentinel labels from config."""
    try:
        labels = _cf()["labels"]
        return {str(labels["NOT_CODED"]).strip().lower(),
                str(labels["UNABLE_TO_DETECT"]).strip().lower()}
    except Exception:
        return {"not coded", "unable to detect"}






def is_yes_no_counts(counts_df: pd.DataFrame) -> bool:
    """True when a counts frame is a yes/no(/unclear) variable's.

    Every column name must lowercase into yes/no/unclear or a recode
    missing-value sentinel, with at least one non-sentinel column present.

    Args:
        counts_df: Group × category counts frame (post explode/crosstab).

    Returns:
        Whether the variable should bypass PCA for a yes-share column.
    """
    sentinels = _recode_sentinels()
    names = [str(c).strip().lower() for c in counts_df.columns]
    if not names:
        return False
    substantive = [n for n in names if n not in sentinels]
    return bool(substantive) and all(n in YES_NO_UNCLEAR for n in substantive)






def yes_share_from_counts(counts_df: pd.DataFrame) -> pd.Series:
    """Per-group share of "yes" among the yes/no/unclear answers.

    Sentinel columns are excluded from numerator and denominator. A group
    with no substantive answers gets NaN; a variable with no "yes" column
    yields 0.0 shares.

    Args:
        counts_df: Group × category counts frame.

    Returns:
        Float series in [0, 1] (or NaN), indexed like ``counts_df``.
    """
    lowered = {c: str(c).strip().lower() for c in counts_df.columns}
    substantive = [c for c, low in lowered.items() if low in YES_NO_UNCLEAR]
    yes_cols = [c for c, low in lowered.items() if low == "yes"]

    denominator = counts_df[substantive].sum(axis=1).astype("float64")
    numerator = (counts_df[yes_cols].sum(axis=1).astype("float64")
                 if yes_cols else pd.Series(0.0, index=counts_df.index))
    return (numerator / denominator.where(denominator > 0)).astype("float64")






Group = Union[dict[str, int], Sequence[str]]
Metric = Literal["jensen-shannon", "hellinger", "total-variation", "bray-curtis", "chi2"]
Mode = Literal["distance", "similarity"]
Weighting = Literal["none", "idf"]






def pairwise_matrix_for_categorical_groups(
        counts_df,
        metric: Metric = "jensen-shannon",
        mode: Mode = "similarity",
        #labels: Optional[List[str]] = None,
        smoothing: float = 1e-9,
        weighting: Weighting = "none",
        gamma: float | None = None,
        drop_rare_globally_below: float = 0.0,
    ):
    """
    Pairwise matrix for categorical groups with power-law friendly options.


    counts_df : pd.DataFrame
        Rows = groups, columns = categories, values = counts or frequencies.

    metric: "jensen-shannon", "hellinger", "total-variation", "bray-curtis", "chi2"
    mode: "distance" or "similarity"
    smoothing: small Dirichlet mass added to every category to avoid zero issues
    weighting: "none" or "idf" to reduce dominance of ubiquitous head categories
    gamma: optional probability tempering in (0,1], e.g., 0.8 to soften the head
    drop_rare_globally_below: drop categories whose global relative mass is below this threshold
    """



    def _row_normalize(mat: np.ndarray) -> np.ndarray:
        sums = mat.sum(axis=1, keepdims=True)
        with np.errstate(invalid="ignore"):
            probs = np.divide(mat, sums, out=np.zeros_like(mat), where=sums > 0)
        return probs



    def _chi2_distance(x: np.ndarray, y: np.ndarray) -> float:
        den = x + y
        num = (x - y) ** 2
        with np.errstate(divide="ignore", invalid="ignore"):
            frac = np.divide(num, den, out=np.zeros_like(num), where=den > 0)
        return 0.5 * frac.sum()  # unbounded

    def _apply_weighting(P: np.ndarray, weighting: Weighting) -> np.ndarray:
        if weighting == "none":
            return P
        # IDF across groups: rarer categories get higher weight
        # df_j is count of groups where category j is present
        G, C = P.shape
        df = (P > 0).sum(axis=0)
        # classic smooth-IDF
        w = np.log(1 + (G / np.clip(df, 1, None)))
        W = P * w  # weight then renormalize per row
        return _row_normalize(W)

    def _apply_tempering(P: np.ndarray, gamma: float | None) -> np.ndarray:
        # gamma in (0,1] shrinks head and lifts tail, gamma=1 is identity
        if gamma is None or abs(gamma - 1.0) < 1e-12:
            return P
        if gamma <= 0 or gamma > 1:
            raise ValueError("gamma must be in (0,1]")
        P_gamma = np.power(P, gamma)
        return _row_normalize(P_gamma)


    # main code starts here

    # Optionally drop globally rare categories (helps with extreme tails)
    if drop_rare_globally_below > 0:
        global_mass = counts_df.sum(axis=0)
        global_mass /= global_mass.sum() if global_mass.sum() > 0 else 1.0
        keep = global_mass[global_mass >= drop_rare_globally_below].index
        if len(keep) == 0:
            # nothing passes; keep everything to avoid empty matrices
            keep = counts_df.columns
        counts_df = counts_df[keep]

    counts = counts_df.to_numpy(dtype=float)
    G, C = counts.shape

    # Prepare both counts and probabilities
    counts_smooth = counts + smoothing  # safe for chi2 and bray-curtis too
    P = _row_normalize(counts_smooth)

    # Optional weighting and tempering on probabilities
    P = _apply_weighting(P, weighting)
    P = _apply_tempering(P, gamma)

    # Compute pairwise
    

    if metric == "jensen-shannon":
        # Scipy's jensenshannon is sqrt(JS_divergence). base=2 puts it in [0,1]
        D_condensed = scipy_pdist(P, metric='jensenshannon', base=2.0)
        D = scipy_squareform(D_condensed)
    
    elif metric == "hellinger":
        # Hellinger is 1/sqrt(2) * Euclidean distance of sqrt(probs)
        D_condensed = scipy_pdist(np.sqrt(P), metric='euclidean')
        D = scipy_squareform(D_condensed) / np.sqrt(2)
    
    elif metric == "total-variation":
        # TV is 0.5 * L1 distance
        D_condensed = scipy_pdist(P, metric='cityblock')
        D = scipy_squareform(D_condensed) * 0.5
    
    elif metric == "bray-curtis":
        # Built-in braycurtis
        D_condensed = scipy_pdist(counts_smooth, metric='braycurtis')
        D = scipy_squareform(D_condensed)
    
    elif metric == "chi2":
        # Chi2 is harder to vectorise cleanly with pdist without massive memory usage
        # Falling back to your loop for this one specific metric or using explicit expansion
        D = np.zeros((G, G), dtype=float)
        for i in range(G):
            for j in range(i + 1, G):
                d = _chi2_distance(counts_smooth[i], counts_smooth[j])
                D[i, j] = D[j, i] = d
    else:
        raise ValueError("Unsupported metric")

    if mode == "distance":
        np.fill_diagonal(D, 0.0)
        return pd.DataFrame(D, index=counts_df.index, columns=counts_df.index)

    # similarity
    if metric == "chi2":
        S = 1.0 / (1.0 + D)
    else:
        S = 1.0 - D
    np.fill_diagonal(S, 1.0)
    return pd.DataFrame(S, index=counts_df.index, columns=counts_df.index)

    



def calc_entropy_and_dominance(
        counts_df,
        top_n: int = 1) -> dict:

    """
    1. Calculate entropy (Shannon diversity) 
    2. Calculate dominance as the combined share of the top N categories for each group.

    Parameters
    ----------
    counts_df : pd.DataFrame
        Rows = groups, columns = categories, values = counts or frequencies.
    top_n : int, default=1
        Number of top categories to include when computing dominance.

    Returns
    -------
    pd.Series
        Dominance score for each group (sum of top_n category proportions).
    """


    if top_n < 1:
        raise ValueError("top_n must be at least 1")


    # convert to probabilities (row-normalized)
    probs = counts_df.div(counts_df.sum(axis=1), axis=0).fillna(0.0)

    # sum the top_n proportions for each row
    dom = probs.apply(lambda row: np.sort(row.values)[-top_n:].sum(), axis=1)

    entropy = -(probs * np.log2(np.clip(probs, 1e-12, 1))).sum(axis=1)

    return {"entropy":entropy} # simplifying things

    return {"dominance":dom, "entropy":entropy}




def interpret_axes_with_categories(
    counts_df = None,
    feat = None,
    top=5,
    cutoff=None,
) -> dict:
    """
    counts_df: rows=groups, cols=categories, values=counts
    feat: DataFrame with columns cat_PC1..k, index=matching group labels
    cutoff: minimum |correlation| for a category to be reported; None -> the
        [correlations] config section (default 0.2)
    Returns dict {axis: [(category, corr), ...]}
    """
    if cutoff is None:
        cutoff = float(_cf().get("correlations", {}).get("interpretation_cutoff", 0.2))

    
    probs = counts_df.div(counts_df.sum(axis=1), axis=0).fillna(0.0)
    probs = probs.loc[feat.index]  # align

    out = {}
    
    # --- Vectorized Correlation ---
    # Convert feat (Principal Components) and probs (Category Proportions) to aligned matrices
    # probs is N_groups x N_categories
    # feat is N_groups x N_components
    
    # 1. Align indices strictly
    common_index = probs.index.intersection(feat.index)
    P = probs.loc[common_index]
    F = feat.loc[common_index]

    if len(P) < 2: 
        # Not enough data for correlation
        return {col: {"top_positive": [], "top_negative": []} for col in feat.columns}

    # 2. Standardize both matrices (subtract mean, divide by std)
    # This allows correlation to be a simple matrix multiplication: (1/N-1) * (X.T @ Y)
    P_centered = P - P.mean(axis=0)
    P_std = P.std(axis=0)
    # Avoid division by zero for constant columns (std=0)
    P_scaled = P_centered.divide(P_std.replace(0, np.nan), axis=1)

    F_centered = F - F.mean(axis=0)
    F_std = F.std(axis=0)
    F_scaled = F_centered.divide(F_std.replace(0, np.nan), axis=1)

    # 3. Matrix Multiplication: (Categories x Groups) @ (Groups x Components) -> (Categories x Components)
    N = len(common_index)
    # Result is a DataFrame where index=categories, columns=components, values=correlation
    corr_matrix = (P_scaled.T @ F_scaled) / (N - 1)

    # 4. Extract top correlations per component
    return _interpretation_from_corr(corr_matrix, feat.columns, top=top, cutoff=cutoff)




def _interpretation_from_corr(corr_matrix, feat_columns, top=5, cutoff=None) -> dict:
    """Build the per-component interpretation dict from a correlation matrix.

    The selection logic extracted verbatim from
    :func:`interpret_axes_with_categories`, shared with the high-cardinality
    path (which computes ``corr_matrix`` sparsely) so both paths pick and
    format the same categories the same way.

    Args:
        corr_matrix: Categories × components correlation DataFrame.
        feat_columns: Component column names to report on.
        top: Number of top categories per direction.
        cutoff: Minimum |correlation|; None -> the [correlations] config value.

    Returns:
        Dict ``{component: {"top_positive": str, "top_negative": str,
        "top_positive_cat": str|None, "top_negative_cat": str|None}}``.
    """
    if cutoff is None:
        cutoff = float(_cf().get("correlations", {}).get("interpretation_cutoff", 0.2))

    out = {}
    for col in feat_columns:
        # Get correlations for this PC, drop NaNs (from constant columns)
        corrs = corr_matrix[col].dropna()

        # Top Positive
        top_pos = corrs.sort_values(ascending=False).head(top).items()
        top_pos = [(cat, cor) for cat, cor in top_pos if cor > cutoff and cat not in [_cf()["labels"]["OTHER_THINGS"]]]
        top_pos_str = "More likely: " + " | ".join([f"{cat.replace('  and  ', ' & ')}" for cat, cor in top_pos]) if top_pos else ""
        top_pos_cat = top_pos[0][0] if top_pos else None

        # Top Negative
        top_neg = corrs.sort_values(ascending=True).head(top).items()
        top_neg = [(cat, cor) for cat, cor in top_neg if cor < -cutoff and cat not in [_cf()["labels"]["OTHER_THINGS"]]]
        top_neg_str = "More likely: " + " | ".join([f"{cat.replace('  and  ', ' & ')}" for cat, cor in top_neg]) if top_neg else ""
        top_neg_cat = top_neg[0][0] if top_neg else None

        out[col] = {"top_positive": top_pos_str, "top_negative": top_neg_str, "top_positive_cat": top_pos_cat, "top_negative_cat": top_neg_cat}

    return out




def _sparse_corr_with_components(S, categories, feat) -> pd.DataFrame:
    """Pearson correlation of every category's probability series with each PC.

    Sparse counterpart of the dense standardize-and-multiply in
    :func:`interpret_axes_with_categories` — same ddof=1 convention, same
    NaN-for-constant-columns behaviour — computed without ever materialising
    the groups × categories probability frame.

    Args:
        S: ``(n_groups, n_categories)`` sparse matrix of full-denominator
            category probabilities (rows aligned to ``feat.index``).
        categories: Column labels for ``S`` (the shortened category names).
        feat: Groups × components DataFrame (the PC scores).

    Returns:
        Categories × components correlation DataFrame (NaN where a category's
        or a component's variance is zero).
    """
    F = feat.to_numpy(dtype=float)
    n = F.shape[0]
    f_mean = F.mean(axis=0)
    f_std = F.std(axis=0, ddof=1)

    col_sum = np.asarray(S.sum(axis=0)).ravel()
    col_sumsq = np.asarray(S.multiply(S).sum(axis=0)).ravel()
    p_mean = col_sum / n
    p_var = (col_sumsq - n * p_mean**2) / (n - 1)
    p_std = np.sqrt(np.clip(p_var, 0.0, None))

    cross = np.asarray(S.T @ F)  # (C, k): sum over groups of p * f
    cov = (cross - np.outer(p_mean, f_mean) * n) / (n - 1)
    denom = np.outer(p_std, f_std)
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = cov / denom
    corr[denom == 0] = np.nan
    return pd.DataFrame(corr, index=pd.Index(categories), columns=feat.columns)



"""def interpret_pca_axes(
    c = None,
    scaled_pca_scores = None, 
    events_df_recoded = None):


    grouping_factors = get_grouping_factors_from_var_schema()

    # this looks awkward but it makes the selection realy clear
    components_associated_w_this_feature = []
    for kk in scaled_pca_scores.columns:
        if c in kk:
            if kk[-1].isnumeric():
                components_associated_w_this_feature += [kk]

    selected_pca_scores = scaled_pca_scores.set_index(grouping_factors)[components_associated_w_this_feature]

    cool_counts = transform_category_column_to_counts_df(
        events_df_recoded, 
        the_column=c, 
        grouping_factors=grouping_factors)

    xx = interpret_axes_with_categories(counts_df = cool_counts, feat = selected_pca_scores, top=3)
    for yy in xx:
        for zz in xx[yy]:
            print(yy,zz,xx[yy][zz])
    
    return xx"""






def transform_category_column_to_counts_df(
    some_events,
    the_column = None,
    grouping_factors: list = None,
    drop_rare_globally_below: float = None,
):
    """Group × category counts frame for one categorical feature.

    Args:
        some_events: The study's recoded events frame.
        the_column: The categorical column (scalar or list-valued).
        grouping_factors: Grouping columns (sorted internally).
        drop_rare_globally_below: The rare-category threshold the downstream
            :func:`_prepare_probability_matrix` will apply. When provided and
            the column exceeds :data:`DENSE_CATEGORY_LIMIT` distinct string
            categories, the dense frame is built for the surviving categories
            only — selected against the TRUE total, so the survivor set is
            identical to what the downstream drop would have kept — and the
            full-distribution statistics ride along in
            ``counts_df.attrs["pca_full_dist"]``. None keeps the historical
            all-categories dense frame.

    Returns:
        Float counts DataFrame (groups × categories, shortened column names).
    """
    if the_column is None:
        raise ValueError("No column provided")
    if grouping_factors is None:
        raise ValueError("No selected factors provided")

    grouping_factors = sorted(grouping_factors)


    def _shorten_strings(
        s_list,
        min_length = 20):

        for s in s_list:
            if type(s)!=str:
                return s_list

        target_length = min_length
        #original_max_length = max([len(s) for s in s_list])
        new_list = [s[:target_length] for s in s_list]

        while len(set(new_list)) != len(new_list):
            target_length += 5
            new_list = [s[:target_length] for s in s_list]

        #new_max_length = max([len(s) for s in new_list])
        return new_list

    # 1. Subset & Explode
    # Ensure we work on a copy to avoid SettingWithCopy warnings
    df = some_events[[the_column] + grouping_factors].copy()

    # Explode list-like elements; leave scalar columns untouched. A bare
    # df.explode() is unsafe under pandas 2.2 + pyarrow: it is a silent no-op
    # on large_list columns (under-counts multi-valued features), and it raises
    # AttributeError on pyarrow-backed StringDtype scalar columns because the
    # arrow explode kernel reads dtype.pyarrow_dtype, which StringDtype lacks.
    # Downgrade large_list to list so explode works, then only explode columns
    # that are genuinely list-like.
    df[the_column] = downgrade_series_if_large(df[the_column])
    col_pa_type = getattr(df[the_column].dtype, "pyarrow_dtype", None)
    is_list_col = (col_pa_type is not None and pa.types.is_list(col_pa_type)) or df[the_column].dtype == object
    df_exploded = df.explode(the_column) if is_list_col else df

    # 2. Filter (Vectorized)
    # Remove nulls and unwanted keywords
    if df_exploded[the_column].empty:
        # Handle case where column is empty or all null
         return pd.DataFrame(index=some_events.set_index(grouping_factors).index.unique())


    if df_exploded.empty:
         return pd.DataFrame(index=some_events.set_index(grouping_factors).index.unique())

    # 2b. High-cardinality gate. A free-text-ish categorical can carry tens of
    # thousands of near-unique values; the dense crosstab below costs
    # groups × categories × 8 B (doubled by the astype) and OOM-killed the
    # task runner. Above DENSE_CATEGORY_LIMIT the exact sparse path builds the
    # dense frame only for the categories the downstream rare-drop would keep.
    if drop_rare_globally_below is not None and drop_rare_globally_below > 0:
        col_vals = df_exploded[the_column]
        vc = col_vals.value_counts()
        stringy = pd.api.types.is_string_dtype(col_vals.dtype) or (
            col_vals.dtype == object
            and all(isinstance(x, str) for x in vc.index))
        if stringy and len(vc) > DENSE_CATEGORY_LIMIT:
            return _high_cardinality_counts_df(
                df_exploded, the_column, grouping_factors, vc,
                drop_rare_globally_below, _shorten_strings)

    # 3. Crosstab / Pivot
    # groupby factors + category column -> size -> unstack
    # Using crosstab is generally cleaner for frequency counts.
    #
    # Note: `columns` MUST be wrapped in a list, not passed as a bare Series.
    # pandas 2.2.x's crosstab doesn't normalise a single-Series argument
    # (it relies on the caller handing in list-likes). A bare Series here
    # triggers `list + Series` inside crosstab's `pass_objs` filter, which
    # dispatches to Series arithmetic; for pyarrow-backed string columns
    # the arithmetic then fails in `_box_pa` with
    # `ArrowInvalid: Could not convert ...`. Wrapping avoids the
    # arithmetic path entirely regardless of dtype.
    counts_df = pd.crosstab(
        index=[df_exploded[c] for c in grouping_factors],
        columns=[df_exploded[the_column]],
    )
    
    # Crosstab returns ints, convert to float as per original return type expectations
    counts_df = counts_df.astype(float)

    # 4. Shorten column names (keep existing logic helper)
    counts_df.columns = _shorten_strings(counts_df.columns)

    return counts_df




def _high_cardinality_counts_df(df_exploded, the_column, grouping_factors, vc,
                                drop_rare_globally_below, shorten) -> pd.DataFrame:
    """Survivors-only dense counts frame + exact full-distribution sidecar.

    The historical path built the dense crosstab over ALL categories and let
    `_prepare_probability_matrix` drop the rare ones afterwards. This path
    picks the identical survivor set FIRST — the threshold is applied against
    the true total, so any category clearing it here also clears it against
    the (smaller) survivors-only total downstream, making the downstream drop
    a no-op — and never materialises the full matrix.

    The consumers that genuinely need the full distribution (entropy, the
    ``top1`` modal category, PC sign-fixing and axis interpretation, the
    ``_raw`` probability columns) get exact equivalents computed from
    long-format counts and a sparse probability matrix, attached as
    ``counts_df.attrs["pca_full_dist"]``:

    * ``n_categories`` — total distinct categories (drives the <2-category
      early exit, which must consider the FULL cardinality).
    * ``entropy`` — per-group Shannon entropy over the full distribution.
    * ``top1`` — per-group modal category (shortened name; ties break to the
      lexicographically-first original name, matching dense ``idxmax``).
    * ``sparse_probs`` — groups × all-categories CSR of full-denominator
      probabilities, rows aligned to the returned frame's index.
    * ``categories`` — the shortened labels for ``sparse_probs``'s columns.

    Category names are shortened against the FULL category list (not just the
    survivors), so labels match what the all-categories dense path published.

    Args:
        df_exploded: The exploded (column, factors) frame.
        the_column: The categorical column name.
        grouping_factors: Sorted grouping columns.
        vc: ``df_exploded[the_column].value_counts()`` (NaN excluded).
        drop_rare_globally_below: Rare-category mass threshold.
        shorten: The caller's ``_shorten_strings`` helper.

    Returns:
        Float counts DataFrame over surviving categories, indexed by every
        group (groups with no surviving observations appear as all-zero rows,
        exactly as the downstream column-drop left them).
    """
    total = float(vc.sum())

    # Deterministic order: count desc, then original name asc for ties.
    mass = vc.sort_index(kind="stable").sort_values(ascending=False, kind="stable")
    survivors = mass[mass / total >= drop_rare_globally_below]
    if len(survivors) == 0:
        # Degenerate backstop (A4): nothing clears the threshold. The dense
        # path's keep-everything fallback would rebuild the full matrix; keep
        # a bounded head instead.
        logger.warning(
            f"    [PCA] {the_column}: no category reaches "
            f"{drop_rare_globally_below:.2%} of {int(total):,} observations — "
            f"keeping the {RARE_FALLBACK_TOP_N} most frequent of {len(mass):,}.")
        survivors = mass.iloc[:RARE_FALLBACK_TOP_N]
    elif len(survivors) > DENSE_CATEGORY_LIMIT:
        logger.warning(
            f"    [PCA] {the_column}: {len(survivors):,} categories clear the "
            f"{drop_rare_globally_below:.2%} threshold — capping the dense frame "
            f"at the {DENSE_CATEGORY_LIMIT:,} most frequent.")
        survivors = survivors.iloc[:DENSE_CATEGORY_LIMIT]
    logger.info(
        f"    [PCA] {the_column}: {len(vc):,} categories -> "
        f"{len(survivors)} survive the {drop_rare_globally_below:.2%} filter "
        f"({float(survivors.sum()) / total:.1%} of observations).")

    # Shorten against the FULL sorted category list — the historical dense
    # path shortened all-columns-at-once, and collision-driven extension means
    # a survivor's short name depends on the whole list.
    all_cats_sorted = sorted(str(c) for c in vc.index)
    short_map = dict(zip(all_cats_sorted, shorten(all_cats_sorted)))

    # Full group index (factor rows with a NaN category never reach the
    # crosstab, so filter first). The degenerate one-column crosstab is the
    # cheapest way to get the exact index object — same dtypes, same sort,
    # same MultiIndex shape — the full crosstab would have had.
    dfe = df_exploded[df_exploded[the_column].notna()]
    full_index = pd.crosstab(
        index=[dfe[c] for c in grouping_factors],
        columns=[pd.Series("_", index=dfe.index)],
    ).index

    # Dense frame for the survivors only, via the same crosstab machinery.
    sub = dfe[dfe[the_column].isin(set(survivors.index))]
    counts_df = pd.crosstab(
        index=[sub[c] for c in grouping_factors],
        columns=[sub[the_column]],
    )
    counts_df = counts_df.astype(float)
    counts_df = counts_df.reindex(full_index, fill_value=0.0)
    counts_df.columns = [short_map[str(c)] for c in counts_df.columns]

    # Exact full-distribution statistics from long-format counts.
    long = (dfe.groupby(grouping_factors + [the_column]).size()
            .rename("n").reset_index())
    long["_total"] = long.groupby(grouping_factors)["n"].transform("sum")
    p = long["n"].astype("float64") / long["_total"].astype("float64")

    if len(grouping_factors) > 1:
        group_keys = pd.MultiIndex.from_frame(long[grouping_factors])
    else:
        group_keys = pd.Index(long[grouping_factors[0]])

    entropy = (-(p * np.log2(p))).groupby(group_keys).sum()
    entropy = entropy.reindex(full_index)

    # Modal category: max count, ties to the first category in dense column
    # order (sorted original names) — the tie-break idxmax used.
    ordered = long.assign(_orig=long[the_column].astype(str)).sort_values(
        ["n", "_orig"], ascending=[False, True], kind="stable")
    top1 = (ordered.drop_duplicates(subset=grouping_factors, keep="first")
            .set_index(grouping_factors)[the_column].astype(str).map(short_map))
    top1 = top1.reindex(full_index)

    # Sparse full-denominator probability matrix (groups × all categories).
    row_codes = full_index.get_indexer(group_keys)
    cat_codes = pd.Categorical(
        long[the_column].astype(str), categories=all_cats_sorted).codes
    sparse_probs = scipy_sparse.csr_matrix(
        (p.to_numpy(), (row_codes, cat_codes)),
        shape=(len(full_index), len(all_cats_sorted)))

    counts_df.attrs["pca_full_dist"] = {
        "n_categories": len(vc),
        "entropy": entropy,
        "top1": top1,
        "sparse_probs": sparse_probs,
        "categories": [short_map[c] for c in all_cats_sorted],
    }
    return counts_df








def _prepare_probability_matrix(
    counts_df,
    smoothing=1e-9,
    weighting="idf",
    gamma=0.8,
    drop_rare_globally_below=0.001,
):
    """
    Prepare weighted/tempered probability matrix from counts.
    Extracted from pairwise_matrix_for_categorical_groups to reuse
    the same preprocessing without computing pairwise distances.
    
    Returns the processed probability matrix as a DataFrame with
    the same index as the (possibly filtered) counts_df.
    """
    
    def _row_normalize(mat):
        sums = mat.sum(axis=1, keepdims=True)
        with np.errstate(invalid="ignore"):
            probs = np.divide(mat, sums, out=np.zeros_like(mat), where=sums > 0)
        return probs

    # Drop globally rare categories
    if drop_rare_globally_below > 0:
        global_mass = counts_df.sum(axis=0)
        total = global_mass.sum()
        if total > 0:
            global_mass /= total
        keep = global_mass[global_mass >= drop_rare_globally_below].index
        if len(keep) == 0:
            keep = counts_df.columns
        counts_df = counts_df[keep]

    counts = counts_df.to_numpy(dtype=float)
    counts_smooth = counts + smoothing
    P = _row_normalize(counts_smooth)

    # IDF weighting
    if weighting == "idf":
        G, C = P.shape
        df = (P > 0).sum(axis=0)
        w = np.log(1 + (G / np.clip(df, 1, None)))
        P = P * w
        P = _row_normalize(P)

    # Tempering
    if gamma is not None and abs(gamma - 1.0) > 1e-12:
        if 0 < gamma <= 1:
            P = np.power(P, gamma)
            P = _row_normalize(P)

    return pd.DataFrame(P, index=counts_df.index, columns=counts_df.columns)










def transform_categories_to_components_and_diversity(
    counts_df=None,
    smoothing=1e-9,
    weighting="idf",
    gamma=0.8,
    drop_rare_globally_below=0.001,
    max_components=5,
    target_explained_variance=0.8,
    verbose=False
):
    """
    Transform categorical count data into principal components and diversity metrics.
    
    Uses direct PCA on weighted/tempered probability vectors (fast) rather than
    the previous MDS→PCA pipeline (slow). Produces equivalent geometric structure.
    
    Parameters
    ----------
    counts_df : pd.DataFrame
        Rows = groups, columns = categories, values = counts.
    smoothing : float
        Small Dirichlet mass added to every category to avoid zero issues.
    weighting : str
        "none" or "idf" to reduce dominance of ubiquitous head categories.
    gamma : float or None
        Probability tempering in (0,1], e.g., 0.8 to soften the head.
    drop_rare_globally_below : float
        Drop categories whose global relative mass is below this threshold.
    max_components : int
        Maximum number of principal components to retain.
    target_explained_variance : float
        Target cumulative explained variance ratio for component selection.
    """

    if counts_df is None:
        raise ValueError("counts_df must be provided")

    # High-cardinality sidecar (see _high_cardinality_counts_df): when present,
    # the frame holds only the surviving categories and the full-distribution
    # statistics ride in attrs — entropy/top1/interpretation must come from
    # there, not from the (truncated) dense frame.
    full_dist = counts_df.attrs.get("pca_full_dist")

    if full_dist is not None:
        entropy_and_dominance = {"entropy": full_dist["entropy"]}
    else:
        entropy_and_dominance = calc_entropy_and_dominance(counts_df, 1)

    # Check validation - if there is only 1 category, we can't do PCA.
    # The gate is on the variable's TOTAL cardinality: a high-cardinality
    # variable whose dense frame shrank to one surviving column still takes
    # the PCA path, exactly as the all-categories frame did after the
    # downstream rare-drop.
    n_total_categories = (full_dist["n_categories"] if full_dist is not None
                          else counts_df.shape[1])
    if n_total_categories < 2:
        if verbose:
            print(f"Skipping PCA for {counts_df.shape[1]} category. Returning 0-variance component.")
        
        # Create a single component of zeros
        pc_df = pd.DataFrame(0.0, index=counts_df.index, columns=["C0"])
        
        # 1 component explains 0 variance (technically undefined but 0 is safe)
        n_components = 1
        explained = [0.0]
        
        print(f"{n_components} components explain {sum(explained[:n_components]):.2%} of the variance", end="\n", flush=True)

        result_df = pd.concat([pc_df, pd.DataFrame(entropy_and_dominance),pd.DataFrame(counts_df.T.idxmax(), columns=["top1"])],axis=1)

        # Interpretation is empty/trivial
        xx = {}
        for col in pc_df.columns:
            xx[col] = {"top_positive": "", "top_negative": "", "explained_variance_pct": 0.0}

        return result_df, pc_df, xx



    # Direct PCA on weighted/tempered probability vectors
    prob_matrix = _prepare_probability_matrix(
        counts_df,
        smoothing=smoothing,
        weighting=weighting,
        gamma=gamma,
        drop_rare_globally_below=drop_rare_globally_below,
    )

    # Standardize before PCA (center and scale each category dimension)
    scaler = StandardScaler()
    prob_scaled = scaler.fit_transform(prob_matrix.values)

    # PCA directly on the probability vectors
    n_max = min(max_components, prob_scaled.shape[1] - 1, prob_scaled.shape[0] - 1)
    if n_max < 1:
        n_max = 1
    pca = PCA(n_components=n_max)
    pca_coords = pca.fit_transform(prob_scaled)

    # Check how much variance each component explains
    explained = pca.explained_variance_ratio_
    
    # Handle the case where total variance is 0, causing NaNs in explained_variance_ratio_
    explained = np.nan_to_num(explained, nan=0.0)

    explained_cumsum = explained.cumsum()
    for i in range(len(explained_cumsum), 0, -1):
        if (explained_cumsum[i-1] < target_explained_variance):
            required_components = i+1
            n_components = min(max_components, i+1, pca_coords.shape[1])
            break
        required_components = 1
        n_components = 1
        

    if verbose:
        logger.info(f"Explained variance per component: {', '.join([f'{p:.3f}' for p in explained])}")
        logger.info(f"Cumulative explained variance: {', '.join([f'{p:.3f}' for p in explained.cumsum()])}")

    print(f"{n_components} components explain {sum(explained[:n_components]):.2%} of the variance", end="", flush=True)
    if required_components != n_components:
        print(f"  |  {required_components} required to be able to explain {target_explained_variance:.0%} of the variance")
    else:
        print()

    pc_df = pd.DataFrame(pca_coords[:, :n_components], index=prob_matrix.index)
    pc_df.columns = [f"C{c}" for c in range(n_components)]

    # Resolve PCA sign ambiguity (eigenvectors can point in either +/- direction arbitrarily)
    # For dichotomous variables specifically, we want "yes" to be the positive/top direction.
    if full_dist is not None:
        # One sparse correlation pass serves both the sign fix and the axis
        # interpretation; flipping a PC negates its correlation column.
        corr_all = _sparse_corr_with_components(
            full_dist["sparse_probs"], full_dist["categories"], pc_df)
        target_cat = None
        for cat in ["yes", "Yes", "True", "true"]:
            if cat in corr_all.index:
                target_cat = cat
                break
        for col in pc_df.columns:
            if target_cat is not None:
                # If the vector aligned oppositely to "yes", flip it so "yes" goes UP.
                corr = corr_all.loc[target_cat, col]
                if pd.notna(corr) and corr < -1e-5:
                    pc_df[col] *= -1
                    corr_all[col] = -corr_all[col]
        top1_series = full_dist["top1"]
    else:
        probs = counts_df.div(counts_df.sum(axis=1), axis=0).fillna(0.0)
        for i, col in enumerate(pc_df.columns):
            target_cat = None
            for cat in ["yes", "Yes", "True", "true"]:
                if cat in probs.columns:
                    target_cat = cat
                    break

            if target_cat is not None:
                # If the vector aligned oppositely to "yes", flip it so "yes" goes UP.
                corr = probs[target_cat].corr(pc_df[col])
                if pd.notna(corr) and corr < -1e-5:
                    pc_df[col] *= -1
        top1_series = counts_df.T.idxmax()

    result_df = pd.concat([pc_df,pd.DataFrame(entropy_and_dominance),top1_series.to_frame("top1")],axis=1)

    if full_dist is not None:
        if len(pc_df) < 2:
            # Not enough groups for correlation — same shape the dense
            # interpretation path returns in this case.
            xx = {col: {"top_positive": [], "top_negative": []} for col in pc_df.columns}
        else:
            xx = _interpretation_from_corr(corr_all, pc_df.columns, top=5)
    else:
        xx = interpret_axes_with_categories(counts_df = counts_df, feat = pc_df, top = 5)

    # Pre-calculate variance percentages
    for idx, col in enumerate(pc_df.columns):
        if col in xx:
            xx[col]["explained_variance_pct"] = round(float(explained[idx]) * 100, 1)

    # Inject True 0-1 proportions for the dominant category of each component as its `_raw` absolute value
    # Only injected for components that explain exactly 100% of the variance to reduce tooltip bloat
    if full_dist is not None:
        def _prob_series(cat):
            # One category's full-denominator probability series from the
            # sparse matrix — the dense probs frame is never built here.
            j = full_dist["categories"].index(cat)
            col_vec = np.asarray(
                full_dist["sparse_probs"][:, j].todense()).ravel()
            return pd.Series(col_vec, index=counts_df.index)
    else:
        probs = counts_df.div(counts_df.sum(axis=1), axis=0).fillna(0.0)

        def _prob_series(cat):
            return probs[cat]
    raw_prob_cols = {}
    for col in pc_df.columns:
        if col in xx and xx.get(col, {}).get("top_positive_cat") and xx[col].get("explained_variance_pct") == 100.0:
            raw_prob_cols[f"{col}_raw"] = _prob_series(xx[col]["top_positive_cat"])
    
    if raw_prob_cols:
        result_df = pd.concat([result_df, pd.DataFrame(raw_prob_cols, index=result_df.index)], axis=1)

    for yy in xx:
        for zz in xx[yy]:
            if verbose:
                logger.info(f"{yy} {zz} {xx[yy][zz]}")


    return result_df, pc_df, xx






def calculate_scaled_pca_scores(
    study_name = None,
    study_recoded_dataset = None,
    minimum_group_size = None,
    target_explained_variance = 0.8,
    drop_rare_globally_below = 0.01,
    scale_it = True,
    load_from_cache = True,
    save_to_cache = True,
    verbose = False,
    ):

    # None -> the [correlations] config section (default 10), so every caller
    # (refresh workers, on-demand web path) honours the same admin-set value.
    if minimum_group_size is None:
        minimum_group_size = int(_cf().get("correlations", {}).get("minimum_group_size", 10))
    
    logger.info(
        f"Starting Principal Component Analysis. Now: {_dt.datetime.now()}...")

    # Phase timers (for Cloud Run vs local diagnostics). Each phase wall-clock
    # is accumulated below and emitted in a single summary line at the end.
    _t_start = _time.perf_counter()
    _t_load = 0.0
    _t_prep = 0.0
    _t_pca = 0.0
    _t_scale = 0.0
    _t_save = 0.0

    if study_name is None and study_recoded_dataset is None:
        logger.error("    [PCA] ERROR: This process cannot run without a study name or a recoded study dataset as input. Process failed.")
        return None


    _t_phase = _time.perf_counter()
    if load_from_cache and study_name is not None:

        if data_io.exists(
            storage_location="cache",
            filename=f"{study_name}_recoded.parquet",
            ):
            # Project to only the columns PCA actually consumes. The cache
            # `*_recoded.parquet` files contain 91 columns (collections joined
            # with scrapes + annotations), but PCA only needs the var_schema
            # factors/features/grouping_factors plus `annotated_ok` (filter).
            pca_factors, pca_features = get_factors_and_features_from_var_schema(verbose=False)
            pca_grouping = get_grouping_factors_from_var_schema(verbose=False)
            cols_for_pca = sorted(set(pca_factors + pca_features + pca_grouping
                                      + ['annotated_ok']))
            study_recoded_dataset = data_io.load_parquet_selective(
                storage_location="cache",
                filename=f"{study_name}_recoded.parquet",
                columns=cols_for_pca,
                verbose=verbose)

    if study_name is not None and study_recoded_dataset is None:
        logger.info("@@ No cached recoded study dataset found. I must create it. Please wait a moment...")
        study_recoded_dataset = create_study_recoded_dataset(
            study_name = study_name,
            save_to_cache=True,
            verbose = verbose
        )
        if study_recoded_dataset is None:
            raise ValueError("No study dataset found for study '{study_name}'")
        logger.info("@@ Back after created recoded dataset for this study. I will now resume the PCA analysis.")

    if study_recoded_dataset is None:
        logger.error("    [PCA] ERROR: This process cannot run without a study dataset. Process failed.")
        return None

    _t_load = _time.perf_counter() - _t_phase
    _t_phase = _time.perf_counter()

    if verbose:
        logger.info(f"    [PCA] Starting with a dataset of shape {study_recoded_dataset.shape}")


    # checking that the groupubg factors are properly defined and present in the dataset
    targeted_grouping_factors = get_grouping_factors_from_var_schema(some_events_df = None, verbose=verbose)
    grouping_factors = get_grouping_factors_from_var_schema(some_events_df = study_recoded_dataset, verbose=verbose)
    if targeted_grouping_factors != grouping_factors:
        logger.error(f"    [PCA] Targeted grouping factors {targeted_grouping_factors} differ from those available in the dataset {grouping_factors}. Terminating.")
        return None, None
    del targeted_grouping_factors

    # An all-NA grouping factor makes the group key meaningless, so it still
    # terminates. A merely CONSTANT one does not: a single-collection study
    # (every participant "Just Me" study is one) has one collection_id and many
    # local_dates, and grouping on the date alone is the intended unit. The
    # constant factor stays in grouping_factors so the scores frame keeps its
    # collection_id level for the downstream stats artifact. Only a dataset in
    # which EVERY grouping factor is constant has too little structure; the
    # minimum-group-count check further down catches the rest.
    varying_factors = []
    for gf in grouping_factors:
        n_unique = study_recoded_dataset[gf].dropna().nunique()
        if n_unique == 0:
            logger.error(f"    [PCA] Grouping factor {gf} is all NA. Terminating.")
            return None, None
        if n_unique > 1:
            varying_factors.append(gf)

    if not varying_factors:
        logger.error(f"    [PCA] Every grouping factor ({', '.join(grouping_factors)}) has a "
                     "single value, so there are no groups to compare. Terminating.")
        return None, None


    fyp_factors, fyp_features = get_factors_and_features_from_var_schema(some_events_df = study_recoded_dataset, verbose=verbose)

    # PCA always requires annotated rows, regardless of the [viz] require_annotated_items
    # flag. The PCA features are themselves annotation-derived recoded variables, so
    # un-annotated rows would be dropped at the dropna(subset=fyp_features) step below
    # anyway — keeping the filter explicit avoids confusing downstream behaviour.
    pre_len = len(study_recoded_dataset)
    if "annotated_ok" in study_recoded_dataset.columns:
        study_recoded_dataset = study_recoded_dataset[study_recoded_dataset["annotated_ok"].fillna(False)]
    else:
        # No annotations ingested yet — drop everything so downstream sees empty
        study_recoded_dataset = study_recoded_dataset.iloc[0:0]
    post_len = len(study_recoded_dataset)
    if verbose:
        logger.info(f"    [PCA] Only keeping events that are successfully annotated -> {pre_len - post_len:,} events dropped. Shape: {study_recoded_dataset.shape}")

    if post_len == 0:
        logger.error("    [PCA] No annotated events available for this study. Terminating.")
        return None, None


    not_na_columns = study_recoded_dataset[fyp_features + grouping_factors].notna().sum() / len(study_recoded_dataset)
    columns_to_be_dropped = not_na_columns[not_na_columns<=0.9].index
    study_recoded_dataset = study_recoded_dataset.drop(columns=columns_to_be_dropped)
    if verbose:
        logger.info(f"    [PCA] Dropping features and grouping factors with more than 10% missing values -> {len(columns_to_be_dropped)} columns dropped. Shape: {study_recoded_dataset.shape}")

    # I need to do this again in case some factors or features were dropped in the previous step
    fyp_factors, fyp_features = get_factors_and_features_from_var_schema(some_events_df = study_recoded_dataset, verbose=verbose)

    pre_len = len(study_recoded_dataset)
    study_recoded_dataset = study_recoded_dataset.dropna(subset = fyp_features + grouping_factors)
    post_len = len(study_recoded_dataset)
    if verbose:
        logger.info(f"    [PCA] Dropping rows with missing values in features and grouping factors -> {(pre_len - post_len):,} rows dropped. Shape: {study_recoded_dataset.shape}")
    del pre_len, post_len, columns_to_be_dropped


    # ----------------------------
    # Dropping groups that are too small
    # ----------------------------
    if verbose:
        logger.info(f"    [PCA] Dropping <{'|'.join(grouping_factors)}> groups that are smaller than {minimum_group_size} rows")

    group_sizes = study_recoded_dataset[grouping_factors].groupby(grouping_factors).agg(group_size = pd.NamedAgg(column=grouping_factors[0], aggfunc="count"))
    good_sized_groups = group_sizes[list((group_sizes>=minimum_group_size).to_dict()["group_size"].values())]

    if len(good_sized_groups) < 10:
        logger.error(f"    [PCA] ERROR: Less than 10 groups of {len(group_sizes):,} have at least {minimum_group_size} elements. I refuse to do PCA with soo few groups. Terminating.")
        return None, None
    elif len(good_sized_groups) < 100:
        logger.warning(f"    [PCA] WARNING: Only {len(good_sized_groups):,} groups of {len(group_sizes):,} have at least {minimum_group_size} elements. This is dangerously low. Please check your data.")
    
    too_small_groups = group_sizes[list((group_sizes<minimum_group_size).to_dict()["group_size"].values())]

    if len(too_small_groups) > 0:

        n_groups = len(group_sizes)
        if verbose:
            logger.info(
                f"    [PCA] {len(too_small_groups):,} groups of {n_groups:,} have fewer than {minimum_group_size}"
                f" elements and will be excluded from the analysis. {len(good_sized_groups):,} groups remain."
            )
            logger.info(f"    [PCA] This results in a loss of {too_small_groups.sum().values[0]:,} elements. {good_sized_groups.sum().values[0]:,} elements remain.")

        study_recoded_dataset = study_recoded_dataset.set_index(grouping_factors).loc[good_sized_groups.index].reset_index().copy()

        if verbose:
            logger.info(f"    [PCA] Confirming new shape: {study_recoded_dataset.shape}")
    else:
        if verbose:
            logger.info("    [PCA] No groups were below the threshold")

    _t_prep = _time.perf_counter() - _t_phase
    _t_phase = _time.perf_counter()

    # ----------------------------
    # PCA transformation
    # ----------------------------
    if verbose:
        logger.info("    [PCA] Consolidating events into aggregation groups and performing PCA transformation on categorical variables")

    events_pca_scores = []
    comp_interpretations = {}

    # batch all numerical features into a single groupby
    numerical_features = [c for c in study_recoded_dataset[fyp_features].columns
                          if c in study_recoded_dataset.select_dtypes(include=["number"]).columns]
    numerical_means_raw = None
    if numerical_features:
        num_block = study_recoded_dataset[numerical_features + grouping_factors]
        # Untransformed means feed the `_raw` tooltip copies (natural units).
        numerical_means_raw = num_block.groupby(grouping_factors).mean()
        # Contract-declared transforms (log1p for heavy-tailed counts) apply to
        # the row values before the mean; negatives are missing-value sentinels
        # (e.g. play_count -1 where a platform exposes no view count).
        transforms = contract_numeric_transforms()
        transformed = num_block.copy()
        for col in numerical_features:
            if transforms.get(col) == "log1p":
                vals = pd.to_numeric(transformed[col], errors="coerce").astype("double[pyarrow]")
                vals = vals.mask(vals < 0, pd.NA)
                transformed[col] = np.log1p(vals.astype("float64"))
        numerical_means = transformed.groupby(grouping_factors).mean()
        events_pca_scores.append(numerical_means)


    # transform categorical features to counts dataframes. Passing the rare
    # threshold lets high-cardinality columns build their dense frame for the
    # surviving categories only (see transform_category_column_to_counts_df).
    def _f1(cc):
        return transform_category_column_to_counts_df(
            study_recoded_dataset, the_column=cc, grouping_factors=grouping_factors,
            drop_rare_globally_below=drop_rare_globally_below)
    categorical_features = study_recoded_dataset[fyp_features].select_dtypes(exclude=["number"]).columns

    # Build each counts frame inside the loop rather than materializing them all
    # up front: these are dense group x category matrices, so holding every
    # feature's frame at once made peak memory the SUM of them instead of the
    # largest one.
    for i in range(len(categorical_features)):

        col_name = categorical_features[i]
        counts_df = _f1(col_name)
        print(f"    [PCA] {(i+1):02}/{len(categorical_features)}. {col_name}, {counts_df.shape}", end=": ", flush=True)

        # Yes/no(/unclear) variables bypass PCA: emit the day share of "yes"
        # instead of components/entropy. The scaled copy joins the frame like
        # any numeric; the _raw twin rides the existing raw-category plumbing
        # (scaled version dropped, unscaled re-injected after StandardScaler).
        if is_yes_no_counts(counts_df):
            share = yes_share_from_counts(counts_df)
            share_col = f"{col_name}{SHARE_OF_FEED_SUFFIX}"
            wer = pd.DataFrame({share_col: share, f"{share_col}_raw": share})

            if len(grouping_factors) > 1:
                wer.index = pd.MultiIndex.from_tuples(wer.index, names=grouping_factors)
            else:
                wer.index = wer.index.get_level_values(0)
                wer.index.name = grouping_factors[0]
            wer.index = convert_index_dtype_pyarrow(wer.index)

            comp_interpretations[share_col] = {
                "top_positive": "more 'yes' days",
                "top_negative": "fewer 'yes' days",
            }
            events_pca_scores += [wer.copy()]
            print(f"yes-share column ({share_col})", flush=True)
            continue

        wer, the_pc_df, comp_interpretation = transform_categories_to_components_and_diversity(
            counts_df=counts_df,
            gamma=0.8,
            max_components=15,
            target_explained_variance=target_explained_variance,
            drop_rare_globally_below=drop_rare_globally_below,
            verbose=False)

        wer.drop("top1", axis=1, inplace=True, errors="ignore")
        wer.columns = [col_name+"_"+col for col in wer.columns]

        if len(grouping_factors) > 1:
            wer.index = pd.MultiIndex.from_tuples(wer.index, names=grouping_factors)
        else:
            wer.index = wer.index.get_level_values(0)
            wer.index.name = grouping_factors[0]

        wer.index = convert_index_dtype_pyarrow(wer.index)
        
        for cvb in comp_interpretation:
            comp_interpretations[col_name+"_"+cvb] = comp_interpretation[cvb]

        events_pca_scores += [wer.copy()]

        
    events_pca_scores = pd.concat(events_pca_scores, axis=1)

    _t_pca = _time.perf_counter() - _t_phase
    _t_phase = _time.perf_counter()

    if verbose:
        logger.info(f"    [PCA] Shape of PCA scores table: {events_pca_scores.shape}")

    if not scale_it:
        if verbose:
            logger.info("    [PCA] Not scaling the scores and not saving them either")


    if verbose:
        logger.info("    [PCA] Scaling pca scores and concatenating factors into the scaled table")

    events_pca_scores_scaled = pd.DataFrame(
        StandardScaler().fit_transform(events_pca_scores),
        index=events_pca_scores.index,
        columns=events_pca_scores.columns)

    events_pca_scores_scaled.reset_index(inplace=True)
    

    # Ensure we don't select duplicate columns if grouping_factors overlap with the time columns
    cols_to_keep = list(set(fyp_factors + grouping_factors) & set(study_recoded_dataset.columns))

    # Shuffle rows to ensure random output order and avoid systematic bias (e.g. always picking the 'first' row)
    # when reducing the dataset to unique metadata combinations.
    time_columns_to_put_back = study_recoded_dataset[cols_to_keep].sample(frac=1, random_state=42).drop_duplicates()
    


    time_columns_to_put_back = time_columns_to_put_back.set_index(grouping_factors)
    pca_indexed = events_pca_scores_scaled.set_index(grouping_factors)

    # Extract raw numerical features and append them with '_raw' suffix — from
    # the untransformed aggregation, so tooltips stay in natural units even for
    # log1p-transformed features.
    if numerical_means_raw is not None:
        raw_num_df = numerical_means_raw.rename(
            columns={c: f"{c}_raw" for c in numerical_means_raw.columns})
    else:
        raw_num_df = pd.DataFrame(index=events_pca_scores.index)
    
    # Extract previously injected raw proportion columns from PCA categories
    raw_cat_cols = [c for c in events_pca_scores.columns if str(c).endswith("_raw")]
    raw_cat_df = events_pca_scores[raw_cat_cols]

    # Combine all raw unscaled columns
    raw_df = pd.concat([raw_num_df, raw_cat_df], axis=1)

    # Drop standard scaled versions of _raw category columns so we can securely inject the unscaled ones
    pca_indexed = pca_indexed.drop(columns=raw_cat_cols, errors="ignore")

    # How many videos each group averages over — the collection-day's
    # consumption intensity (declared as `videos_watched` in the derived
    # contract) and the sample-summary video count. Attached after scaling so
    # it never enters the PCA basis.
    group_size_df = study_recoded_dataset[grouping_factors].groupby(grouping_factors).agg(
        **{VIDEOS_WATCHED_COL: pd.NamedAgg(column=grouping_factors[0], aggfunc="count")})
    group_size_df.index = convert_index_dtype_pyarrow(group_size_df.index)

    events_pca_scores_scaled = pd.concat(
        [time_columns_to_put_back, pca_indexed, raw_df, group_size_df], axis=1).reset_index().copy()


    # TODO: avoid making direct references to column names
    #events_pca_scores_scaled[local_month"] = events_pca_scores_scaled[local_date"].map(lambda x:x.month)

    if verbose:
        logger.info(f"    [PCA] Shape of scaled PCA scores table: {events_pca_scores_scaled.shape}")

    for c in events_pca_scores_scaled.columns:
        if c not in comp_interpretations:
            comp_interpretations[c] = {'top_positive':'high', 'top_negative':'low'}


    if verbose:
        logger.info("    [PCA] Converting dtypes to pyarrow")
    events_pca_scores_scaled = convert_dtypes_to_pyarrow(events_pca_scores_scaled, verbose=verbose)

    _t_scale = _time.perf_counter() - _t_phase
    _t_phase = _time.perf_counter()

    if save_to_cache and study_name is not None:
        pca_filename = f"{study_name}_PCA.parquet"
        events_pca_scores_scaled.attrs['study_name'] = study_name
        data_io.save_parquet(
            df=events_pca_scores_scaled,
            storage_location="cache",
            filename=pca_filename,
            verbose=verbose,
            )
        if verbose:
            logger.info(f"    [PCA] Saved {events_pca_scores_scaled.shape[0]:,} scaled PCA scores in '{pca_filename}'.")


        comp_inter_filename = f"{study_name}_comp_interpretations.json"
        data_io.save_json(
            data=comp_interpretations,
            storage_location="cache",
            filename=comp_inter_filename,
            verbose=verbose,
            )
        if verbose:
            logger.info(f"    [PCA] Saved {len(comp_interpretations):,} component interpretations in '{comp_inter_filename}'.")

    _t_save = _time.perf_counter() - _t_phase
    _t_total = _time.perf_counter() - _t_start

    logger.info(
        f"    [PCA][TIMING] study={study_name} "
        f"load={_t_load:.2f}s prep={_t_prep:.2f}s pca={_t_pca:.2f}s "
        f"scale={_t_scale:.2f}s save={_t_save:.2f}s total={_t_total:.2f}s"
    )
    logger.info(f"...done. PCA completed at {_dt.datetime.now()}")



    return events_pca_scores_scaled, comp_interpretations

