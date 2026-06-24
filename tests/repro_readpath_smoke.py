"""Read-only smoke: new code reading OLD per-study caches (deploy-without-recode).

Confirms the explorer + PCA read paths do not crash when the current (new)
codebase loads pre-migration recoded caches. No data is written.
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fyp import fyp_config

fyp_config.initialize()

from web_interface import data_service


STUDIES = [
    "chenglong",
    "paper_three",
    "ABC Verify 2026",
    "new_prompt_test",
    "dmrc_summer_mini",
]


def main() -> int:
    failures = 0
    for study in STUDIES:
        print(f"\n=== {study} ===")
        try:
            df, col_types = data_service.get_explorer_data(study, context="explorer")
            if df is None:
                print("  explorer: None (study not found / empty) -- skipping")
                continue
            print(f"  explorer: {len(df):,} rows x {df.shape[1]} cols")
            present = [c for c in ("main_activity", "speech_vs_music",
                                   "political_score", "sensitivity_score",
                                   "faces_age_estimate") if c in df.columns]
            print(f"  sample annotation cols present: {present}")
            # scene_sentiments should still exist in OLD caches (harmless under new code)
            scene_cols = [c for c in df.columns if "scene_sentiment" in c.lower()]
            print(f"  scene_sentiment cols in OLD cache: {scene_cols}")
        except Exception:
            failures += 1
            print("  EXPLORER FAILED:")
            traceback.print_exc()

        try:
            pca = data_service.get_pca_df(study)
            n = 0 if pca is None else len(pca)
            print(f"  pca: {n:,} rows")
        except Exception:
            failures += 1
            print("  PCA FAILED:")
            traceback.print_exc()

    print(f"\nTOTAL FAILURES: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
