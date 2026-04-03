



def compute_effect_sizes(anova_table, factor):
    """
    Compute eta-squared (η²) and omega-squared (ω²) for a one-way ANOVA.

    Parameters
    ----------
    anova_table : pandas.DataFrame
        ANOVA table from statsmodels, containing:
        - sum_sq
        - df
        - PR(>F)
    factor : str
        The row label (index) corresponding to your factor,
        e.g. 'C(local_week)'.

    Returns
    -------
    dict
        {'eta2': value, 'omega2': value}
    """

    ss_effect = anova_table.loc[factor, "sum_sq"]
    df_effect = anova_table.loc[factor, "df"]

    ss_resid = anova_table.loc["Residual", "sum_sq"]
    df_resid = anova_table.loc["Residual", "df"]

    # Eta squared
    eta2 = ss_effect / (ss_effect + ss_resid)

    # Mean squared residual
    ms_resid = ss_resid / df_resid

    # Omega squared
    omega2 = (ss_effect - df_effect * ms_resid) / (ss_effect + ss_resid + ms_resid)

    return {"eta2": eta2, "omega2": omega2}



def run_anova(
    events_pca_scores_scaled,
    selected_factor
):

    import statsmodels.api as sm
    import statsmodels.formula.api as smf

    from pandas import Series, concat, DataFrame

    anova_results = {}
    lots_of_anova_tables = []

    # TODO: make this dynamic. There is a risk with startswith 'G_'
    raise "Hey Patrik, you need to fix the reference to 'G_' in this code - otherwise it won't work"
    component_cols = [col for col in events_pca_scores_scaled.columns if False and col.startswith("G_")]



    for factor in [selected_factor]:#grouping_factors:
        anova_results[factor] = {}
        for comp in component_cols:
            formula = f"{comp} ~ C({factor})"
            model = smf.ols(formula, data=events_pca_scores_scaled).fit()
            anova_table = sm.stats.anova_lm(model, typ=2)

            compute_effect_sizes(anova_table, f"C({factor})")

            unstacked_anova_table = concat([
                anova_table.T.unstack(0),
                DataFrame(Series(compute_effect_sizes(anova_table, f"C({factor})")), columns=["Effect_sizes"]).stack().swaplevel()
                ])

            lots_of_anova_tables += [unstacked_anova_table.copy()]
            lots_of_anova_tables[-1]["component"] = comp

            anova_results[factor][comp] = anova_table

    all_anova_tables = DataFrame(lots_of_anova_tables)
    #all_anova_tables = all_anova_tables.drop([
    #    #(f"C({selected_factor})","df"),
    #    ("Residual","df"),
    #    ("Residual","F"),
    #    ("Residual","PR(>F)")], axis=1).sort_values(((f"C({selected_factor})","PR(>F)")))

    print(f"Rows: {len(all_anova_tables):,} -- Cols: {len(all_anova_tables.columns):,}")

    return all_anova_tables




def run_permanova(
    events_pca_scores_scaled,
    selected_factor,
    fyp_features
):
    # Running PERMANOVA across all fyp_features


    from numpy import fill_diagonal
    from skbio.stats.distance import DistanceMatrix, permanova
    from sklearn.metrics import pairwise_distances

    cols_based_on_fyp_features = []
    for k1 in events_pca_scores_scaled.columns:
        for k2 in fyp_features:
            if k2 in k1 and not "dominance" in k1 and not "entropy" in k1:
                cols_based_on_fyp_features += [k1]
    print(f"Number of columns based on the features in the pca scores table: {len(cols_based_on_fyp_features)}")


    feature_matrix = events_pca_scores_scaled[cols_based_on_fyp_features].to_numpy()

    D = pairwise_distances(feature_matrix, metric='euclidean')

    # there are some weird issues with symmetry and 'hollow-ness' that I can fix this way
    D = (D + D.T) / 2
    fill_diagonal(D, 0.0)

    dm = DistanceMatrix(D)

    # iterate over the factors
    for factor_col in [selected_factor]:#grouping_factors:
        factor = events_pca_scores_scaled[factor_col].tolist()
        result = permanova(dm, factor, permutations=999)

        print(factor_col)
        #print(result)
        #print()
    
    return result




def run_many_permanova(
    events_pca_scores_scaled,
    selected_factor,
    fyp_features
):
    # running PERMANOVA on each feature (averaging the results for the feature components)


    from numpy import fill_diagonal
    from pandas import DataFrame
    from skbio.stats.distance import DistanceMatrix, permanova
    from sklearn.metrics import pairwise_distances

    many_results = {}
    for one_feature in fyp_features:

        selected_columns = [c for c in events_pca_scores_scaled.columns if c==one_feature or (c.startswith(one_feature) and c[-1].isnumeric())]

        if len(selected_columns)==0:
            continue

        # setting up the distance matrix
        feature_matrix = events_pca_scores_scaled[selected_columns].to_numpy()
        D = pairwise_distances(feature_matrix, metric='euclidean')

        # need to fix some weird issues with matrix symmetry and hollowness
        D = (D + D.T) / 2
        fill_diagonal(D, 0.0)

        dm = DistanceMatrix(D)


        # iterate over the factors
        for factor_col in [selected_factor]:# grouping_factors:

            print(f"{factor_col}-->{one_feature}")

            factor = events_pca_scores_scaled[factor_col].tolist()
            result = permanova(dm, factor, permutations=999)

            many_results[f"{factor_col}-->{one_feature}"] = result


    permanova_on_fyp_features = DataFrame(many_results).T.drop(columns=["method name","test statistic name", "sample size", "number of permutations", "number of groups"]).sort_values("p-value")
    significant_associations = permanova_on_fyp_features[permanova_on_fyp_features["p-value"]<0.01]
    significant_fyp_features = list(significant_associations.index.map(lambda x:x.split("-->")[-1]))

    return permanova_on_fyp_features

    #significant_fyp_features