"""Smoke test: confirm PCA's selective-load path produces the same shape and
columns as the previous full-load path.

We don't run the full PCA — we just exercise calculate_scaled_pca_scores up to
the point of loading and let it run end-to-end against a real cached study to
prove no critical column was dropped from the projection.
"""
import sys
from os.path import abspath, dirname, join

sys.path.insert(0, abspath(join(dirname(__file__), '..')))

from fyp import fyp_config
fyp_config.initialize()

from fyp import data_io, pca
from fyp.recode_variables import (
    get_factors_and_features_from_var_schema,
    get_grouping_factors_from_var_schema,
)


STUDY = 'chenglong'  # smallest cached study (~26 MB)


def main():
    print(f"\n[1] Verifying selective-load column set for study '{STUDY}'")
    factors, features = get_factors_and_features_from_var_schema(verbose=False)
    grouping = get_grouping_factors_from_var_schema(verbose=False)
    cols_for_pca = sorted(set(factors + features + grouping
                              + ['annotated_ok', 'dd_event_id']))
    print(f"      var_schema requests {len(cols_for_pca)} columns for PCA")

    df_proj = data_io.load_parquet_selective(
        storage_location='cache',
        filename=f'{STUDY}_recoded.parquet',
        columns=cols_for_pca,
        verbose=True,
    )
    df_full = data_io.load_parquet(
        storage_location='cache',
        filename=f'{STUDY}_recoded.parquet',
        verbose=False,
    )

    proj_cols = set(df_proj.columns)
    full_cols = set(df_full.columns)
    on_disk_pca_cols = full_cols.intersection(cols_for_pca)

    print(f"      full read shape:       {df_full.shape}")
    print(f"      projected read shape:  {df_proj.shape}")
    print(f"      requested cols:        {len(cols_for_pca)}")
    print(f"      cols on disk that PCA wants: {len(on_disk_pca_cols)}")
    print(f"      projected col count:   {len(proj_cols)}")

    missing_from_proj = on_disk_pca_cols - proj_cols
    extra_in_proj = proj_cols - on_disk_pca_cols
    if missing_from_proj:
        print(f"      [FAIL] PCA cols on disk that the projection MISSED: {sorted(missing_from_proj)}")
        sys.exit(1)
    if extra_in_proj:
        print(f"      [FAIL] projection returned cols not requested: {sorted(extra_in_proj)}")
        sys.exit(1)
    if len(df_proj) != len(df_full):
        print(f"      [FAIL] row count mismatch: {len(df_proj)} vs {len(df_full)}")
        sys.exit(1)
    print(f"      [OK] projection returns exactly the {len(proj_cols)} on-disk PCA cols, all rows preserved")

    print(f"\n[2] Running calculate_scaled_pca_scores on '{STUDY}' (load_from_cache=True)")
    result = pca.calculate_scaled_pca_scores(
        study_name=STUDY,
        load_from_cache=True,
        verbose=True,
    )
    if result is None or (isinstance(result, tuple) and result[0] is None):
        print("      [FAIL] PCA returned None — selective load may have dropped a required column")
        sys.exit(1)
    print(f"      [OK] PCA completed: result type={type(result).__name__}")
    if isinstance(result, tuple):
        for i, r in enumerate(result):
            if hasattr(r, 'shape'):
                print(f"         result[{i}] shape={r.shape}")

    print("\n[OK] PCA selective-load smoke test passed.")


if __name__ == '__main__':
    main()
