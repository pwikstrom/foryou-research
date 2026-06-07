"""Shared refinement + comparison helpers for the free-text vs structured A/B.

Both arms run through the IDENTICAL recode downstream
(``consolidate_rare_columns`` → transcript de-dup → ``rename_columns`` →
``recode_events_df`` → ``clean_up_machine_annotations`` → flags → pyarrow).
The only thing that differs between arms is the flatten step that produces the
per-response field dicts. That isolates "free-text vs structured generation" as
the single variable.

Comparison is field-type aware (driven by ``var_schema`` scale), because the
right metric differs by kind:
  * numeric  -> correlation + mean abs diff + coverage
  * enum     -> exact-match agreement + coverage
  * list     -> mean Jaccard overlap + exact-set agreement + coverage
  * freetext -> coverage only (two independent generations of an essay never
                match verbatim, so exact agreement is meaningless there)
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd
from _harness import _normalize_cell  # tests/golden/_harness.py

import fyp.machine_annotation as ma
from fyp.fyp_config import fyp_cf
from fyp.recode_variables import recode_events_df, rename_columns
from fyp.types import convert_dtypes_to_pyarrow

_NORM_SENTINELS = {"unable to detect", "-", "other category", "not coded", "", "<NA>", "no"}

_NUMERIC_SCALES = {"ratio", "interval", "ordinal"}
_ENUM_SCALES = {"categorical", "dichotomous", "factor"}
_LIST_SCALES = {"collection"}


def refine_from_flat_dicts(records: list[dict], quiet: bool = True) -> pd.DataFrame:
    """Run the shared recode downstream on already-flattened per-response dicts."""
    df = pd.DataFrame(records)
    sink = io.StringIO()
    ctx = contextlib.redirect_stdout(sink) if quiet else contextlib.nullcontext()
    with ctx:
        df = ma.consolidate_rare_columns_from_gemini_output(df)
        if "transcript" in df.columns:
            df = ma.remove_repetitions_from_transcripts(df)
        df = rename_columns(df)
        df = recode_events_df(study_dataset=df, drop_single_value_cols=False)
        df = ma.clean_up_machine_annotations(some_events=df)
        if "type_of_story" in df.columns:
            df["annotated_ok"] = ~df["type_of_story"].isna()
            df["annotated_fail"] = df["type_of_story"].isna()
        df = convert_dtypes_to_pyarrow(df)
    return df


def _scale_map() -> dict[str, str]:
    vs = fyp_cf["var_schema"]
    return {
        str(n): str(s)
        for n, s in zip(vs["variable_name"], vs["scale"], strict=False)
    }


def _classify(col: str, sa: pd.Series, sb: pd.Series, scales: dict[str, str]) -> str:
    """Classify a column as numeric / enum / list / freetext."""
    if pd.api.types.is_numeric_dtype(sa) and pd.api.types.is_numeric_dtype(sb):
        return "numeric"
    scale = scales.get(col, "")
    if scale in _NUMERIC_SCALES:
        return "numeric"
    if scale in _LIST_SCALES:
        return "list"
    has_list = sa.map(lambda x: isinstance(x, (list, np.ndarray))).any() or sb.map(
        lambda x: isinstance(x, (list, np.ndarray))
    ).any()
    if has_list:
        return "list"
    if scale in _ENUM_SCALES:
        return "enum"
    avg_len = sa.dropna().astype(str).str.len().mean()
    return "enum" if (pd.notna(avg_len) and avg_len < 25) else "freetext"


def _coverage(norm_series: pd.Series) -> float:
    return float((~norm_series.isin(_NORM_SENTINELS)).mean())


def _to_set(value) -> set[str]:
    if isinstance(value, (list, np.ndarray)):
        return {_normalize_cell(v) for v in value} - _NORM_SENTINELS
    norm = _normalize_cell(value)
    return ({norm} - _NORM_SENTINELS) if norm else set()


def compare_arms(df_a: pd.DataFrame, df_b: pd.DataFrame) -> dict:
    """Field-type-aware comparison of two recoded arms aligned on ``item_id``."""
    a = df_a.drop_duplicates("item_id").copy()
    b = df_b.drop_duplicates("item_id").copy()
    a["item_id"] = a["item_id"].astype(str)
    b["item_id"] = b["item_id"].astype(str)
    a = a.set_index("item_id")
    b = b.set_index("item_id")
    common = sorted(set(a.index) & set(b.index))
    a = a.loc[common]
    b = b.loc[common]

    scales = _scale_map()
    skip = {"annotated_ok", "annotated_fail"}
    cols = sorted((set(a.columns) & set(b.columns)) - skip)
    report: dict = {"n_items": len(common), "columns": {}}

    enum_agree, list_jaccard, num_corr, ft_cov_delta = [], [], [], []
    for c in cols:
        sa, sb = a[c], b[c]
        kind = _classify(c, sa, sb, scales)

        if kind == "numeric":
            xa = pd.to_numeric(sa, errors="coerce")
            xb = pd.to_numeric(sb, errors="coerce")
            both = xa.notna() & xb.notna()
            corr = (
                float(xa[both].corr(xb[both]))
                if both.sum() >= 3 and xa[both].std() > 0 and xb[both].std() > 0
                else None
            )
            mad = float((xa[both] - xb[both]).abs().mean()) if both.any() else None
            report["columns"][c] = {
                "kind": "numeric", "correlation": corr, "mean_abs_diff": mad,
                "coverage_a": float(xa.notna().mean()), "coverage_b": float(xb.notna().mean()),
            }
            if corr is not None:
                num_corr.append(corr)

        elif kind == "list":
            sets_a = sa.map(_to_set)
            sets_b = sb.map(_to_set)
            jac, exact = [], []
            for ia, ib in zip(sets_a, sets_b, strict=False):
                union = ia | ib
                jac.append(1.0 if not union else len(ia & ib) / len(union))
                exact.append(1.0 if ia == ib else 0.0)
            report["columns"][c] = {
                "kind": "list", "mean_jaccard": float(np.mean(jac)),
                "exact_set_agreement": float(np.mean(exact)),
                "coverage_a": float(sets_a.map(bool).mean()),
                "coverage_b": float(sets_b.map(bool).mean()),
            }
            list_jaccard.append(float(np.mean(jac)))

        elif kind == "enum":
            na = sa.map(_normalize_cell)
            nb = sb.map(_normalize_cell)
            agreement = float((na.values == nb.values).mean())
            report["columns"][c] = {
                "kind": "enum", "agreement": agreement,
                "coverage_a": _coverage(na), "coverage_b": _coverage(nb),
            }
            enum_agree.append(agreement)

        else:  # freetext
            na = sa.map(_normalize_cell)
            nb = sb.map(_normalize_cell)
            cov_a, cov_b = _coverage(na), _coverage(nb)
            report["columns"][c] = {
                "kind": "freetext", "coverage_a": cov_a, "coverage_b": cov_b,
            }
            ft_cov_delta.append(cov_b - cov_a)

    report["summary"] = {
        "n_columns": len(cols),
        "mean_enum_agreement": float(np.mean(enum_agree)) if enum_agree else None,
        "mean_list_jaccard": float(np.mean(list_jaccard)) if list_jaccard else None,
        "mean_numeric_correlation": float(np.mean(num_corr)) if num_corr else None,
        "mean_freetext_coverage_delta_b_minus_a": (
            float(np.mean(ft_cov_delta)) if ft_cov_delta else None
        ),
        "annotated_ok_rate_a": (
            float(df_a["annotated_ok"].fillna(False).mean())
            if "annotated_ok" in df_a.columns else None
        ),
        "annotated_ok_rate_b": (
            float(df_b["annotated_ok"].fillna(False).mean())
            if "annotated_ok" in df_b.columns else None
        ),
    }
    return report


def distribution_table(df_a: pd.DataFrame, df_b: pd.DataFrame, column: str, top: int = 8) -> dict:
    """Side-by-side top value counts for one column across both arms."""
    out: dict = {"column": column, "arm_a": {}, "arm_b": {}}
    for label, df in (("arm_a", df_a), ("arm_b", df_b)):
        if column in df.columns:
            is_list = df[column].map(lambda x: isinstance(x, (list, np.ndarray))).any()
            series = df[column].explode() if is_list else df[column]
            counts = series.map(_normalize_cell).value_counts().head(top)
            out[label] = {str(k): int(v) for k, v in counts.items()}
    return out
