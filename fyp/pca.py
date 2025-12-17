

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

    def _js_distance(p: np.ndarray, q: np.ndarray, eps: float = 1e-12) -> float:
        p = p + eps; q = q + eps
        p /= p.sum(); q /= q.sum()
        m = 0.5 * (p + q)
        kl_pm = np.sum(p * (np.log2(p) - np.log2(m)))
        kl_qm = np.sum(q * (np.log2(q) - np.log2(m)))
        return np.sqrt(max(0.0, 0.5 * (kl_pm + kl_qm)))  # in [0,1]

    def _hellinger(p: np.ndarray, q: np.ndarray, eps: float = 1e-12) -> float:
        p = p + eps; q = q + eps
        p /= p.sum(); q /= q.sum()
        return np.linalg.norm(np.sqrt(p) - np.sqrt(q)) / np.sqrt(2)  # in [0,1]

    def _total_variation(p: np.ndarray, q: np.ndarray, eps: float = 0.0) -> float:
        p = p + eps; q = q + eps
        p /= p.sum(); q /= q.sum()
        return 0.5 * np.abs(p - q).sum()  # in [0,1]

    def _bray_curtis(x: np.ndarray, y: np.ndarray) -> float:
        num = np.abs(x - y).sum()
        den = (x + y).sum()
        return 0.0 if den == 0 else num / den  # in [0,1]

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
    D = np.zeros((G, G), dtype=float)
    for i in range(G):
        for j in range(i + 1, G):
            if metric == "jensen-shannon":
                d = _js_distance(P[i], P[j])
            elif metric == "hellinger":
                d = _hellinger(P[i], P[j])
            elif metric == "total-variation":
                d = _total_variation(P[i], P[j])
            elif metric == "bray-curtis":
                d = _bray_curtis(counts_smooth[i], counts_smooth[j])
            elif metric == "chi2":
                d = _chi2_distance(counts_smooth[i], counts_smooth[j])
            else:
                raise ValueError("Unsupported metric")
            D[i, j] = D[j, i] = d

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
    counts_df,
    feat,
    top=5
) -> dict:
    """
    counts_df: rows=groups, cols=categories, values=counts
    feat: DataFrame with columns cat_PC1..k, index=matching group labels
    Returns dict {axis: [(category, corr), ...]}
    """

    from numpy import nan as np_nan, inf as np_inf, corrcoef
    from collections import Counter
    
    probs = counts_df.div(counts_df.sum(axis=1), axis=0).fillna(0.0)
    probs = probs.loc[feat.index]  # align

    out = {}
    for col in feat.columns:
        corrs = probs.apply(lambda s: corrcoef(s.values, feat[col].values)[0,1], axis=0)
        corrs = corrs.replace([np_inf, -np_inf], np_nan).dropna()
        # top positive and negative
        top_pos = corrs.sort_values(ascending=False).head(top).items()
        top_pos = " | ".join([f"{cat.replace("  and  "," & ")}({cor:.2f})" for cat,cor in top_pos])
        top_neg = corrs.sort_values(ascending=True).head(top).items()
        top_neg = " | ".join([f"{cat.replace("  and  "," & ")}({cor:.2f})" for cat,cor in top_neg])
        out[col] = {"top_positive": top_pos, "top_negative": top_neg}
    return out




def transform_category_column_to_counts_df(
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

    def _to_count_series(group: Group) -> Series:
        if isinstance(group, dict):
            return Series(group, dtype=float)
        return Series(group, dtype="object").value_counts().astype(float)

    def _align_counts(groups: List[Group]) -> DataFrame:
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
    mds = MDS(n_components=15, dissimilarity='precomputed', random_state=0, n_init=1)
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

    if verbose:
        xx = interpret_axes_with_categories(counts_df, pc_df)
        for yy in xx:
            for zz in xx[yy]:
                print(yy,zz,xx[yy][zz])


    # group_labels must have same order as counts_df.index
    # e.g., if your counts_df rows correspond to experimental groups, pass that as a Series
    if "group" in counts_df.columns:
        group_labels = counts_df["group"].values
    else:
        # or pass it separately via an argument if you prefer
        group_labels = counts_df.index  # placeholder if groups=rows
        # raise ValueError("Need a group label column or separate argument")

    return result_df, pc_df






def calculate_scaled_pca_scores(
    cf = None,
    study_name = None,
    some_events_df = None,
    selected_factors = None,
    minimum_group_size = 10,
    target_explained_variance = 0.8,
    drop_rare_globally_below = 0.01,
    scale_it = True,
    verbose = False

):
    from pandas import NamedAgg, MultiIndex, DataFrame, concat, read_pickle
    from os.path import join, getctime, exists
    from datetime import datetime
    from sklearn.preprocessing import StandardScaler
    from fyp.recode_variables import get_factors_and_features_from_var_scheme
    from fyp.fyp_main import init_config

    if study_name is None:
        raise ValueError("study_name must be specified")

    if cf is None:
        cf = init_config()

    selected_factors = cf["var_scheme"][cf["var_scheme"]["role"]=='group_factor'].variable_name.to_list()



    print(f"Performing Principal Component Analysis based on {" | ".join(selected_factors)}. Study: '{study_name}'")
    print(f"Now: {datetime.now()}")


    if some_events_df is None:
        recoded_path = join(cf['paths']['exports'],f"{study_name}_RECODED.pkl")
        if not exists(recoded_path):
            raise FileNotFoundError(f"Recoded events file not found at: {recoded_path}")
        nice_time = datetime.fromtimestamp(getctime(recoded_path)).strftime('%Y-%m-%d %H:%M:%S')
        print(f"Loading recoded events file in export folder, created at: {nice_time}", end=" ", flush=True)
        some_events_df = read_pickle(recoded_path)
        print(f"  |  Shape: {some_events_df.shape}")


    fyp_factors, fyp_features = get_factors_and_features_from_var_scheme(cf = cf, some_events_df = some_events_df, verbose=verbose)
    
    if verbose:
        print(f"Step 1: Dropping {"-".join(selected_factors)}-groups that are smaller than {minimum_group_size} rows")

    group_sizes = some_events_df[selected_factors].groupby(selected_factors).agg(group_size = NamedAgg(column=selected_factors[0], aggfunc="count"))

    good_sized_groups = group_sizes[list((group_sizes>=minimum_group_size).to_dict()["group_size"].values())]

    too_small_groups = group_sizes[list((group_sizes<minimum_group_size).to_dict()["group_size"].values())]

    if len(too_small_groups) > 0:

        n_groups = len(group_sizes)
        if verbose:
            print(
                f"{len(too_small_groups):,} groups of {n_groups:,} have fewer than {minimum_group_size}"
                f" elements and will be excluded from the analysis. {len(good_sized_groups):,} groups remain."
            )
            print(f"This results in a loss of {too_small_groups.sum().values[0]:,} elements. {good_sized_groups.sum().values[0]:,} elements remain.\n")

        some_events_df = some_events_df.set_index(selected_factors).loc[good_sized_groups.index].reset_index().copy()

        if verbose:
            print(f"  --  Confirming new length of DF: {len(some_events_df):,}")
    else:
        if verbose:
            print("no groups were below the threshold")


    if verbose:
        print()
        print("Step 2: consolidating events into groups and performing PCA transformation on categorical variables")

    events_pca_scores = []
    
    for c in some_events_df[fyp_features].columns:
        if c in some_events_df.select_dtypes(object).columns:
            
            counts_df = transform_category_column_to_counts_df(some_events_df, the_column=c, the_selected_factors=selected_factors)

            if verbose:
                print(c,counts_df.shape, end=": ", flush=True)
            wer, the_pc_df = transform_categories_to_components_and_diversity(
                counts_df,
                metric="hellinger",#"jensen-shannon",
                gamma=0.8,
                max_components=15,
                target_explained_variance=target_explained_variance,
                drop_rare_globally_below=drop_rare_globally_below,
                verbose=False)
            wer.drop("top1", axis=1, inplace=True, errors="ignore")
            wer.columns = [c+"_"+col for col in wer.columns]


            if len(selected_factors) > 1:
                wer.index = MultiIndex.from_tuples(wer.index, names=selected_factors)
            else:
                wer.index = wer.index.get_level_values(0)
                wer.index.name = selected_factors[0]
            
        else:
            the_pc_df = None
            wer = DataFrame(some_events_df[[c] + selected_factors].groupby(selected_factors).mean())
        
        events_pca_scores += [wer.copy()]

    events_pca_scores = concat(events_pca_scores, axis=1)
    if verbose:
        print()
        print(f"Rows: {len(events_pca_scores):,} -- Cols: {len(events_pca_scores.columns):,}")


    if not scale_it:
        print("Not scaling the scores and not saving them either")
        return events_pca_scores

    if verbose:
        print()
        print(f"Step 3: Scaling pca scores and concatenating factors into the scaled table")
    events_pca_scores_scaled = DataFrame(
        StandardScaler().fit_transform(events_pca_scores), 
        index=events_pca_scores.index, 
        columns=events_pca_scores.columns)
    events_pca_scores_scaled.reset_index(inplace=True)

    time_columns_to_put_back = some_events_df[["D_donation_id","T_local_weekday","T_local_date","T_local_week"]].sample(frac=1, random_state=42).drop_duplicates().set_index(selected_factors)
    events_pca_scores_scaled = concat([time_columns_to_put_back,events_pca_scores_scaled.set_index(selected_factors)], axis=1).reset_index().copy()

    events_pca_scores_scaled["T_local_month"] = events_pca_scores_scaled["T_local_date"].map(lambda x:x[:7])

    if verbose:
        print(f"Rows: {len(events_pca_scores_scaled):,} -- Cols: {len(events_pca_scores_scaled.columns):,}")


    pca_filename = f"{study_name}_PCA.pkl"
    export_sub_folder_name = cf["paths"]["exports"].replace(cf["paths"]["main"],"")

    events_pca_scores_scaled.to_pickle(join(cf['paths']['exports'],pca_filename))
    print(f"Exported {len(events_pca_scores_scaled):,} scaled PCA scores in {join(export_sub_folder_name,pca_filename)}.")
    print(f"Now: {datetime.now()}")
    print("--"*60)



    return events_pca_scores_scaled

