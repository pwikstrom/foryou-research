

from typing import Iterable, Hashable, Tuple, Dict, List, Sequence, Union, Literal, Optional


Group = Union[Dict[str, int], Sequence[str]]
Metric = Literal["jensen-shannon", "hellinger", "total-variation", "bray-curtis", "chi2"]
Mode = Literal["distance", "similarity"]
Weighting = Literal["none", "idf"]



def pairwise_matrix_for_categorical_groups(
        counts_df,
        metric: Metric = "jensen-shannon",
        mode: Mode = "similarity",
        labels: Optional[List[str]] = None,
        smoothing: float = 1e-9,
        weighting: Weighting = "none",
        gamma: Optional[float] = None,
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

    import numpy as np
    from pandas import DataFrame


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

    def _apply_tempering(P: np.ndarray, gamma: Optional[float]) -> np.ndarray:
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
    from scipy.spatial.distance import pdist, squareform

    if metric == "jensen-shannon":
        # Scipy's jensenshannon is sqrt(JS_divergence). base=2 puts it in [0,1]
        D_condensed = pdist(P, metric='jensenshannon', base=2.0)
        D = squareform(D_condensed)
    
    elif metric == "hellinger":
        # Hellinger is 1/sqrt(2) * Euclidean distance of sqrt(probs)
        D_condensed = pdist(np.sqrt(P), metric='euclidean')
        D = squareform(D_condensed) / np.sqrt(2)
    
    elif metric == "total-variation":
        # TV is 0.5 * L1 distance
        D_condensed = pdist(P, metric='cityblock')
        D = squareform(D_condensed) * 0.5
    
    elif metric == "bray-curtis":
        # Built-in braycurtis
        D_condensed = pdist(counts_smooth, metric='braycurtis')
        D = squareform(D_condensed)
    
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
        return DataFrame(D, index=counts_df.index, columns=counts_df.index)

    # similarity
    if metric == "chi2":
        S = 1.0 / (1.0 + D)
    else:
        S = 1.0 - D
    np.fill_diagonal(S, 1.0)
    return DataFrame(S, index=counts_df.index, columns=counts_df.index)

    



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

    from numpy import sort as np_sort, clip as np_clip, log2 as np_log2


    if top_n < 1:
        raise ValueError("top_n must be at least 1")


    # convert to probabilities (row-normalized)
    probs = counts_df.div(counts_df.sum(axis=1), axis=0).fillna(0.0)

    # sum the top_n proportions for each row
    dom = probs.apply(lambda row: np_sort(row.values)[-top_n:].sum(), axis=1)

    entropy = -(probs * np_log2(np_clip(probs, 1e-12, 1))).sum(axis=1)

    return {"dominance":dom, "entropy":entropy}




def interpret_axes_with_categories(
    cf = None,
    counts_df = None,
    feat = None,
    top=5
) -> dict:
    """
    counts_df: rows=groups, cols=categories, values=counts
    feat: DataFrame with columns cat_PC1..k, index=matching group labels
    Returns dict {axis: [(category, corr), ...]}
    """


    from fyp.fyp_main import initialize

    if cf is None:
        cf = initialize()


    from numpy import nan as np_nan, inf as np_inf, corrcoef
    from collections import Counter
    
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
    P_scaled = P_centered.divide(P_std.replace(0, np_nan), axis=1)

    F_centered = F - F.mean(axis=0)
    F_std = F.std(axis=0)
    F_scaled = F_centered.divide(F_std.replace(0, np_nan), axis=1)

    # 3. Matrix Multiplication: (Categories x Groups) @ (Groups x Components) -> (Categories x Components)
    N = len(common_index)
    # Result is a DataFrame where index=categories, columns=components, values=correlation
    corr_matrix = (P_scaled.T @ F_scaled) / (N - 1)

    # 4. Extract top correlations per component
    for col in feat.columns:
        # Get correlations for this PC, drop NaNs (from constant columns)
        corrs = corr_matrix[col].dropna()
        
        # Top Positive
        top_pos = corrs.sort_values(ascending=False).head(top).items()
        top_pos = [(cat, cor) for cat, cor in top_pos if cor > 0.2 and cat != cf["misc"]["OTHER_THINGS"]]
        top_pos_str = "More likely: " + " | ".join([f"{cat.replace('  and  ', ' & ')}" for cat, cor in top_pos])

        # Top Negative
        top_neg = corrs.sort_values(ascending=True).head(top).items()
        top_neg = [(cat, cor) for cat, cor in top_neg if cor < -0.2 and cat != cf["misc"]["OTHER_THINGS"]]
        top_neg_str = "More likely: " + " | ".join([f"{cat.replace('  and  ', ' & ')}" for cat, cor in top_neg])

        out[col] = {"top_positive": top_pos_str, "top_negative": top_neg_str}
        
    return out



def interpret_pca_axes(
    cf = None,
    c = None,
    scaled_pca_scores = None, 
    events_df_recoded = None):

    from fyp.fyp_main import initialize

    if cf is None:
        cf = initialize()

    group_factors = cf["var_schema"][cf["var_schema"].role=="group_factor"].variable_name.tolist()

    # this looks awkward but it makes the selection realy clear
    components_associated_w_this_feature = []
    for kk in scaled_pca_scores.columns:
        if c in kk:
            if kk[-1].isnumeric():
                components_associated_w_this_feature += [kk]

    selected_pca_scores = scaled_pca_scores.set_index(group_factors)[components_associated_w_this_feature]

    cool_counts = transform_category_column_to_counts_df(
        events_df_recoded, 
        the_column=c, 
        the_selected_factors=group_factors)

    xx = interpret_axes_with_categories(cf = cf, counts_df = cool_counts, feat = selected_pca_scores, top=3)
    for yy in xx:
        for zz in xx[yy]:
            print(yy,zz,xx[yy][zz])
    
    return xx



def transform_category_column_to_counts_df_old(
    some_events,
    the_column = None,
    the_selected_factors: List = None,
):
    if the_column is None:
        raise ValueError("No column provided") 
    if the_selected_factors is None:
        raise ValueError("No selected factors provided")

    from pandas import Series, DataFrame, MultiIndex
    from collections import Counter

    def _to_count_series(group) -> Series:
        if isinstance(group, dict):
            return Series(group, dtype=float)
        return Series(group, dtype="object").value_counts().astype(float)

    def _align_counts(groups: List) -> DataFrame:
        ser_list = [_to_count_series(g) for g in groups]
        all_idx = sorted(set().union(*[s.index for s in ser_list]))
        counts = DataFrame({i: s.reindex(all_idx, fill_value=0.0) for i, s in enumerate(ser_list)}).T
        return counts  # shape (n_groups, n_categories)

    def _shorten_strings(
        s_list,
        min_length = 20):

        for s in s_list:
            if type(s)!=str:
                return s_list

        target_length = min_length
        original_max_length = max([len(s) for s in s_list])
        new_list = [s[:target_length] for s in s_list]

        while len(set(new_list)) != len(new_list):
            target_length += 5
            new_list = [s[:target_length] for s in s_list]
            #print(target_length)

        new_max_length = max([len(s) for s in new_list])
        return new_list

    group_labels = []
    groups = []

    # Iterative approach (proven faster for this specific data structure than explode)
    for i,g in some_events[[the_column] + the_selected_factors].groupby(the_selected_factors):
        new_list = []
        for k in g[the_column].to_list():
            if type(k) in [list, set]:
                for kk in k:
                    if not kk in ["DDP","BASELINE"]:
                        new_list += [kk]
            elif type(k)==dict:
                raise TypeError('I cannot deal with dicts')
            else:
                if not k in ["DDP","BASELINE"]:
                    new_list += [k]
        group_labels += [i]
        groups += [dict(Counter(new_list))]

    counts_df = _align_counts(groups)
    if group_labels is not None:
        if len(group_labels) != len(groups):
            raise ValueError("group labels length must match number of groups")
        counts_df.index = group_labels

    counts_df.index = MultiIndex.from_tuples(counts_df.index.tolist())
    counts_df.columns = _shorten_strings(counts_df.columns)

    return counts_df
        


def transform_category_column_to_counts_df(
    some_events,
    the_column = None,
    the_selected_factors: List = None,
):
    if the_column is None:
        raise ValueError("No column provided") 
    if the_selected_factors is None:
        raise ValueError("No selected factors provided")

    from pandas import crosstab, DataFrame

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
    df = some_events[[the_column] + the_selected_factors].copy()
    
    # Explode list-like elements. Scalars remain scalars.
    df_exploded = df.explode(the_column)

    # 2. Filter (Vectorized)
    # Remove nulls and unwanted keywords
    if df_exploded[the_column].empty:
        # Handle case where column is empty or all null
         return DataFrame(index=some_events.set_index(the_selected_factors).index.unique())

    mask = (df_exploded[the_column].notna()) & \
           (~df_exploded[the_column].isin(["DDP", "BASELINE"]))
    df_filtered = df_exploded[mask]

    if df_filtered.empty:
         return DataFrame(index=some_events.set_index(the_selected_factors).index.unique())

    # 3. Crosstab / Pivot
    # groupby factors + category column -> size -> unstack
    # Using crosstab is generally cleaner for frequency counts
    counts_df = crosstab(
        index=[df_filtered[c] for c in the_selected_factors],
        columns=df_filtered[the_column]
    )
    
    # Crosstab returns ints, convert to float as per original return type expectations
    counts_df = counts_df.astype(float)

    # 4. Shorten column names (keep existing logic helper)
    counts_df.columns = _shorten_strings(counts_df.columns)
    
    return counts_df








def transform_categories_to_components_and_diversity(
    counts_df,
    metric="jensen-shannon",
    smoothing=1e-9,
    weighting="idf",
    gamma=0.8,
    drop_rare_globally_below=0.001,
    max_components=5,
    target_explained_variance=0.8,
    verbose=False
):

    from sklearn.manifold import MDS
    from sklearn.decomposition import PCA
    from pandas import concat, DataFrame
    from datetime import datetime

    """
    groups: list of dicts {category: count} or sequences of labels
    metric: "jensen-shannon", "hellinger", "total-variation", "bray-curtis", "chi2"
    mode: "distance" or "similarity"
    smoothing: small Dirichlet mass added to every category to avoid zero issues
    weighting: "none" or "idf" to reduce dominance of ubiquitous head categories
    gamma: optional probability tempering in (0,1], e.g., 0.8 to soften the head
    drop_rare_globally_below: drop categories whose global relative mass is below this threshold
    """
    entropy_and_dominance = calc_entropy_and_dominance(counts_df, 1)

    D = pairwise_matrix_for_categorical_groups(
        counts_df,
        metric=metric,
        mode="distance",
        smoothing=smoothing,
        weighting=weighting,
        gamma=gamma,
        drop_rare_globally_below=drop_rare_globally_below,
    )

    # MDS to embed the distance matrix into a coordinate space
    mds = MDS(
        n_components=15, # consider reducing to 10 for performance
        metric='precomputed',
        random_state=0,
        n_init=1,
        init='random',
    )
    coords = mds.fit_transform(D)

    # PCA to see how many axes matter
    pca = PCA()
    pca_coords = pca.fit_transform(coords)

    # check how much variance each component explains
    explained = pca.explained_variance_ratio_

    explained_cumsum = explained.cumsum()
    for i in range(len(explained_cumsum), 0, -1):
        if (explained_cumsum[i-1] < target_explained_variance):
            required_components = i+1
            n_components = min(max_components,i+1)
            break
        required_components = 1
        n_components = 1
        

    if verbose:
        print(f"Explained variance per component: {', '.join([f'{p:.3f}' for p in explained])}")
        print(f"Cumulative explained variance: {', '.join([f'{p:.3f}' for p in explained.cumsum()])}")

    print(f"{n_components} components explain {sum(explained[:n_components]):.2%} of the variance", end="", flush=True)
    if required_components != n_components:
        print(f"  |  {required_components} required to be able to explain {target_explained_variance:.0%} of the variance")
    else:
        print()

    pc_df = DataFrame(pca_coords, index=counts_df.index).iloc[:,:n_components]

    pc_df.columns = [f"C{c}" for c in pc_df.columns]

    result_df = concat([pc_df,DataFrame(entropy_and_dominance),DataFrame(counts_df.T.idxmax(), columns=["top1"])],axis=1)

    xx = interpret_axes_with_categories(counts_df = counts_df, feat = pc_df, top = 5)
    for yy in xx:
        for zz in xx[yy]:
            if verbose:
                print(yy,zz,xx[yy][zz])


    # group_labels must have same order as counts_df.index
    # e.g., if your counts_df rows correspond to experimental groups, pass that as a Series
    if "group" in counts_df.columns:
        group_labels = counts_df["group"].values
    else:
        # or pass it separately via an argument if you prefer
        group_labels = counts_df.index  # placeholder if groups=rows
        # raise ValueError("Need a group label column or separate argument")


    return result_df, pc_df, xx






def calculate_scaled_pca_scores(
    cf = None,
    study_name = None,
    study_recoded_dataset = None,
    minimum_group_size = 10,
    target_explained_variance = 0.8,
    drop_rare_globally_below = 0.01,
    scale_it = True,
    load_from_cache = True,
    save_to_cache = True,
    verbose = False,
    ):

    #from json import dump as json_dump
    from concurrent.futures import ThreadPoolExecutor
    from pandas import NamedAgg, MultiIndex, DataFrame, concat, read_parquet as pd_read_parquet
    from os.path import join as os_join, exists as os_exists
    from datetime import datetime
    from sklearn.preprocessing import StandardScaler
    from fyp.recode_variables import get_factors_and_features_from_var_schema, recode_events_df
    from fyp.fyp_main import initialize, convert_dtypes_to_pyarrow, is_list_like_col
    import fyp.data_io as data_io
    from json import dump as json_dump

    if study_name is None:
        raise ValueError("study_name must be specified")

    if cf is None:
        cf = initialize()

    selected_factors = cf["var_schema"][cf["var_schema"]["role"]=='group_factor'].variable_name.to_list()

    print(
        f"Performing Principal Component Analysis based on {' | '.join(selected_factors)}. Study: '{study_name}'"
        f" | {datetime.now()}")


    if study_name is None and study_recoded_dataset is None:
        print("    ERROR: This process cannot run without a study name or a recoded study dataset as input. Process failed.")
        return None

    if cf is None:
        cf = initialize()

    if load_from_cache and study_name is not None:
        recoded_cache_path = os_join(cf['paths']['temp'], f"CACHE_{study_name}_recoded.parquet")
        if os_exists(recoded_cache_path):
            if verbose:
                print("    Loading recoded events from cache", end=" ", flush=True)
            some_events_df = pd_read_parquet(recoded_cache_path, engine="pyarrow", dtype_backend="pyarrow")
            if verbose:
                print(f"  |  Shape: {some_events_df.shape}")
        else:
            print("@@ No cached recoded study dataset found. I must run the recoding process to create it. Please wait a moment...")
            some_events_df = recode_events_df(
                cf = cf,
                study_name = study_name,
                load_from_cache = True,
                save_to_cache = True,
                verbose = verbose
            )
            print("@@ Back after finalising the recoding process. I will now resume the PCA analysis.")

    if some_events_df is None:
        print("    ERROR: This process cannot run without a study dataset as input or in cache. Process failed.")
        return None



    fyp_factors, fyp_features = get_factors_and_features_from_var_schema(cf = cf, some_events_df = some_events_df, verbose=verbose)
    
    if verbose:
        print(f"    Dropping '{'-'.join(selected_factors)}' groups that are smaller than {minimum_group_size} rows")

    group_sizes = some_events_df[selected_factors].groupby(selected_factors).agg(group_size = NamedAgg(column=selected_factors[0], aggfunc="count"))

    good_sized_groups = group_sizes[list((group_sizes>=minimum_group_size).to_dict()["group_size"].values())]

    too_small_groups = group_sizes[list((group_sizes<minimum_group_size).to_dict()["group_size"].values())]

    if len(too_small_groups) > 0:

        n_groups = len(group_sizes)
        if verbose:
            print(
                f"    {len(too_small_groups):,} groups of {n_groups:,} have fewer than {minimum_group_size}"
                f" elements and will be excluded from the analysis. {len(good_sized_groups):,} groups remain."
            )
            print(f"    This results in a loss of {too_small_groups.sum().values[0]:,} elements. {good_sized_groups.sum().values[0]:,} elements remain.")

        some_events_df = some_events_df.set_index(selected_factors).loc[good_sized_groups.index].reset_index().copy()

        if verbose:
            print(f"    Confirming new shape: {some_events_df.shape}")
    else:
        if verbose:
            print("    No groups were below the threshold")


    if verbose:
        print("    Consolidating events into groups and performing PCA transformation on categorical variables")

    events_pca_scores = []
    

    comp_interpretations = {}

    # first, run through the numerical features. This doesn't take much time. Most of my features are categorical anyway...
    for i,c in enumerate(some_events_df[fyp_features].columns):
        if c in some_events_df.select_dtypes(include=["number"]).columns:
            the_pc_df = None
            one_comp_interpretation = {}
            wer = DataFrame(some_events_df[[c] + selected_factors].groupby(selected_factors).mean())

            for cvb in one_comp_interpretation:
                comp_interpretations[c+"_"+cvb] = one_comp_interpretation[cvb]

            events_pca_scores += [wer.copy()]



    # transform categorical features to a list of counts dataframes
    def _f1(cc):
        return transform_category_column_to_counts_df(some_events_df, the_column=cc, the_selected_factors=selected_factors)
    categorical_features = some_events_df[fyp_features].select_dtypes(exclude=["number"]).columns
    counts_list = list(map(_f1, categorical_features))


    for i in range(len(counts_list)):

        counts_df = counts_list[i]
        col_name = categorical_features[i]
        print(f"    {(i+1):02}/{len(counts_list)}. {col_name}, {counts_df.shape}", end=": ", flush=True)
        wer, the_pc_df, comp_interpretation = transform_categories_to_components_and_diversity(
            counts_df,
            metric="hellinger",#"jensen-shannon",
            gamma=0.8,
            max_components=15,
            target_explained_variance=target_explained_variance,
            drop_rare_globally_below=drop_rare_globally_below,
            verbose=False)
        wer.drop("top1", axis=1, inplace=True, errors="ignore")
        wer.columns = [col_name+"_"+col for col in wer.columns]


        if len(selected_factors) > 1:
            wer.index = MultiIndex.from_tuples(wer.index, names=selected_factors)
        else:
            wer.index = wer.index.get_level_values(0)
            wer.index.name = selected_factors[0]

        
        for cvb in comp_interpretation:
            comp_interpretations[col_name+"_"+cvb] = comp_interpretation[cvb]

        events_pca_scores += [wer.copy()]


    events_pca_scores = concat(events_pca_scores, axis=1)

    if verbose:
        print(f"    Shape of PCA scores table: {events_pca_scores.shape}")


    if not scale_it:
        if verbose:
            print("    Not scaling the scores and not saving them either")
        return events_pca_scores

    if verbose:
        print("    Scaling pca scores and concatenating factors into the scaled table")
    events_pca_scores_scaled = DataFrame(
        StandardScaler().fit_transform(events_pca_scores), 
        index=events_pca_scores.index, 
        columns=events_pca_scores.columns)
    events_pca_scores_scaled.reset_index(inplace=True)

    # TODO: avoid making direct references to column names
    time_columns_to_put_back = some_events_df[["D_donation_id","T_local_weekday","T_local_date","T_local_week"]].sample(frac=1, random_state=42).drop_duplicates().set_index(selected_factors)
    events_pca_scores_scaled = concat([time_columns_to_put_back,events_pca_scores_scaled.set_index(selected_factors)], axis=1).reset_index().copy()

    # TODO: avoid making direct references to column names
    events_pca_scores_scaled["T_local_month"] = events_pca_scores_scaled["T_local_date"].map(lambda x:x[:7])

    if verbose:
        print(f"    Shape of scaled PCA scores table: {events_pca_scores_scaled.shape}")

    for c in events_pca_scores_scaled.columns:
        if not c in comp_interpretations.keys():
            comp_interpretations[c] = {'top_positive':'high', 'top_negative':'low'}

    if verbose:
        print("    Converting dtypes to pyarrow")
    events_pca_scores_scaled = convert_dtypes_to_pyarrow(events_pca_scores_scaled, verbose=verbose)


    if save_to_cache and study_name is not None:
        pca_filename = f"CACHE_{study_name}_PCA.parquet"
        comp_inter_filename = f"CACHE_{study_name}_comp_interpretations.json"

        events_pca_scores_scaled.attrs['study_name'] = study_name
        events_pca_scores_scaled.to_parquet(os_join(cf['paths']['temp'], pca_filename), engine="pyarrow")
        if verbose:
            print(f"    Saved {events_pca_scores_scaled.shape[0]:,} scaled PCA scores in '{pca_filename}'.")

        with open(os_join(cf['paths']['temp'],comp_inter_filename), 'w') as f:
            json_dump(comp_interpretations, f, indent=4)
        if verbose:
            print(f"    Saved {len(comp_interpretations):,} component interpretations in '{comp_inter_filename}'.")

    print(f"    Now: {datetime.now()}")
    #print("--"*60)

            
    return events_pca_scores_scaled, comp_interpretations

