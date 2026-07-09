import sys
import time
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_retokenise_hashtags(
    reporter: TaskStatusReporter, task_args: dict | None = None
) -> None:
    """Re-apply the current stoplist to already-stored hashtags.

    Re-runs the hashtag extractor (``recode_tokenise``) over the preserved
    caption (``desc_raw``) of every source scrape parquet, rewriting the
    ``desc_hashtags`` column with the *current* admin stoplist
    (``fyp/irrelevant_words.py``). Only files whose hashtags actually change
    are rewritten. This is a "clean-only" pass: it does NOT consolidate — a
    forced Consolidate & Refresh is still needed to propagate the change into
    the study/dashboard caches (the UI states this).

    Returns None (no downstream chain).
    """
    import fyp.data_io as data_io
    from fyp import types
    from fyp.organize_datasets import SCRAPES_LABEL
    from fyp.recode_variables import recode_tokenise

    task_args = task_args or {}
    _t_start = time.perf_counter()

    reporter.update_progress(0, "Listing scrape files...")

    scrape_files = [
        fn for fn in data_io.listdir(storage_location="scrape")
        if fn.startswith(SCRAPES_LABEL) and fn.endswith(".parquet")
    ]
    total = len(scrape_files)
    reporter.log(f"Found {total} scrape file(s) to check.")

    if total == 0:
        reporter.emit_data({
            "files_scanned": 0,
            "files_changed": 0,
            "rows_changed": 0,
        })
        reporter.log("No scrape files found — nothing to do.")
        return None

    files_changed = 0
    rows_changed = 0

    for i, fn in enumerate(scrape_files):
        pct = int(round((i / total) * 100))
        reporter.update_progress(pct, f"Re-cleaning {fn} ({i + 1}/{total})...")

        df = data_io.load_parquet(storage_location="scrape", filename=fn)
        if "desc_raw" not in df.columns or "desc_hashtags" not in df.columns:
            continue

        # Re-tokenise from the preserved caption using the CURRENT stoplist.
        # recode_tokenise returns a Series of {"hashtags": [...]} dicts; NA/
        # non-str captions yield []. Rows with a missing desc_raw are left as-is.
        new_hashtags = recode_tokenise(df["desc_raw"]).map(lambda d: d["hashtags"])
        has_raw = df["desc_raw"].notna()

        old = df["desc_hashtags"]
        changed_mask = has_raw & old.map(_as_list).ne(new_hashtags.map(_as_list))
        n_changed = int(changed_mask.sum())
        if n_changed == 0:
            continue

        df.loc[changed_mask, "desc_hashtags"] = new_hashtags[changed_mask]
        # Preserve the list<string>[pyarrow] storage dtype (the recode/save path
        # does the same before writing).
        df["desc_hashtags"] = types.convert_dtypes_to_pyarrow(
            df[["desc_hashtags"]]
        )["desc_hashtags"]

        data_io.save_parquet(df=df, storage_location="scrape", filename=fn)
        files_changed += 1
        rows_changed += n_changed
        reporter.log(f"  {fn}: rewrote {n_changed:,} row(s).")

    _t_total = time.perf_counter() - _t_start
    reporter.emit_data({
        "files_scanned": total,
        "files_changed": files_changed,
        "rows_changed": rows_changed,
    })
    reporter.log(
        f"[TIMING] retokenise_hashtags total={_t_total:.2f}s "
        f"files_scanned={total} files_changed={files_changed} "
        f"rows_changed={rows_changed}"
    )
    reporter.log(
        "Done. Run a Force Reconsolidate (Data Management -> Enrichment) to "
        "apply these changes to studies and dashboards."
    )
    return None




def _as_list(value) -> list:
    """Normalise a stored hashtag cell to a plain list for comparison.

    Handles native lists, numpy/pyarrow arrays, and NA (-> [])."""
    if value is None:
        return []
    try:
        # pandas NA / float nan scalars
        import pandas as pd

        if not hasattr(value, "__len__") and pd.isna(value):
            return []
    except (TypeError, ValueError):
        pass
    return list(value)




if __name__ == "__main__":
    from web_interface.task_status import LocalStatusReporter

    reporter = LocalStatusReporter("retokenise_hashtags")
    try:
        run_retokenise_hashtags(reporter=reporter, task_args={})
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
