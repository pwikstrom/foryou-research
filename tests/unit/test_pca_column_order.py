"""The factor columns of a PCA frame come out in the same order in every process.

``_scale_and_assemble`` used to pick the factor columns with a set
intersection, so the descriptor columns after the grouping keys in each
``{study}_PCA.parquet`` were ordered by the process's string hash seed
(PYTHONHASHSEED): two refreshes of the same study wrote the same values in a
different column order. The selection now keeps var-schema order.

The hash-order test shells out because the hash seed is fixed at interpreter
start.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from fyp.analysis.pca import _factor_columns_to_keep

ROOT = Path(__file__).resolve().parents[2]


def test_factor_columns_keep_factor_order_without_duplicates():
    factors = ["collection_id", "is_weekend", "local_date", "local_week", "source_platform"]
    grouping = ["collection_id", "local_date"]
    columns = ["duration", "source_platform", "local_date", "collection_id", "local_week"]

    assert _factor_columns_to_keep(factors, grouping, columns) == [
        "collection_id",
        "local_date",
        "local_week",
        "source_platform",
    ]


def test_grouping_factor_missing_from_factors_is_appended():
    assert _factor_columns_to_keep(["b", "a"], ["a", "c"], ["a", "b", "c"]) == ["b", "a", "c"]


# Builds a small recoded frame with group-constant descriptor columns, runs the
# real PCA assembly and prints the resulting column order.
_PROBE = """
import json
import numpy as np
import pandas as pd
from fyp.analysis.pca import calculate_scaled_pca_scores

rng = np.random.default_rng(3)
colls, dates = [], []
for coll in ("coll-00", "coll-01"):
    for date in pd.date_range("2026-01-01", periods=14):
        colls += [coll] * 20
        dates += [date] * 20
dates = pd.Series(dates)
n = len(colls)
s = lambda v: pd.array(list(v), dtype="string[pyarrow]")
df = pd.DataFrame({
    "collection_id": s(colls),
    "local_date": s(dates.dt.strftime("%Y-%m-%d")),
    "local_week": s(dates.dt.strftime("%G-W%V")),
    "local_weekday": s(dates.dt.day_name()),
    "is_weekend": s(np.where(dates.dt.dayofweek >= 5, "yes", "no")),
    "source_platform": s(["tiktok"] * n),
    "duration": pd.array(rng.uniform(5, 90, n), dtype="double[pyarrow]"),
    "content_category": s(rng.choice(["comedy", "news", "music", "sport"], n)),
    "annotated_ok": pd.array([True] * n, dtype="bool[pyarrow]"),
})
scores, _ = calculate_scaled_pca_scores(
    study_recoded_dataset=df, load_from_cache=False, save_to_cache=False, verbose=False
)
print("===COLUMNS===" + json.dumps([str(c) for c in scores.columns]))
"""


def _columns_under_hash_seed(seed: int) -> list:
    env = dict(os.environ, PYTHONHASHSEED=str(seed), PYTHONPATH=str(ROOT))
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, cwd=ROOT, env=env
    )
    assert out.returncode == 0, out.stderr[-2000:]
    _, sep, payload = out.stdout.partition("===COLUMNS===")
    assert sep, f"marker missing from stdout:\n{out.stdout[-2000:]}"
    return json.loads(payload.splitlines()[0])


def test_pca_column_order_does_not_depend_on_hash_seed():
    orders = {seed: _columns_under_hash_seed(seed) for seed in (1, 2, 3)}

    first = orders[1]
    for seed, cols in orders.items():
        assert cols == first, f"PYTHONHASHSEED={seed} gave {cols[:8]}, seed 1 gave {first[:8]}"

    # Grouping keys first (they come back out of the index), then the other
    # factors in var-schema order.
    assert first[:6] == [
        "collection_id",
        "local_date",
        "is_weekend",
        "local_week",
        "local_weekday",
        "source_platform",
    ]
