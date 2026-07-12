from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class SimilarityConfig:
    sample_rows: int = 50_000               # per-file row sample for speed; None/0 => full
    random_state: int = 42

    # Anomaly thresholding
    z_thresh: float = 4.0                   # robust z-score threshold for per-feature flags
    file_score_thresh: float = 12.0         # aggregate score threshold to flag file as anomaly

    # Numeric distribution checks
    numeric_quantiles: Tuple[float, ...] = (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)

    # Text/object checks
    topk_value_counts: int = 50
    min_topk_overlap: float = 0.4           # overlap fraction for top-k categories
    text_len_quantiles: Tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9)

    # Special handling
    empty_string_as_null: bool = True
    treat_whitespace_as_empty: bool = True
    consider_zero_rate: bool = True

    # Parquet conversion check
    parquet_check: bool = True
    parquet_engines: Tuple[str, ...] = ("pyarrow", "fastparquet")
    parquet_compression: str = "snappy"     # ignored by some engines if unsupported


def scan_dataframe_parquets_for_anomalies(
    parquet_paths: List[str],
    cfg: SimilarityConfig = SimilarityConfig(),
    *,
    read_kwargs: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any], pd.DataFrame]:
    """
    Scan many parquet pandas DataFrames and flag schema/drift anomalies.
    Also checks whether each dataframe can be written to Parquet without errors
    (writes to a temporary file and deletes it).

    Returns:
        summary_df: one row per file, with schema deviations + parquet check + drift scores
        details: dict with baseline stats + per-file feature-level flags/drift numbers
        normal_schema_df: DataFrame describing the "normal" schema + baseline metric centers/scales
    """
    read_kwargs = read_kwargs or {}

    # -----------------------------
    # Helpers
    # -----------------------------
    def _file_mtime(path: str) -> pd.Timestamp | float:
        try:
            return pd.to_datetime(os.path.getmtime(path), unit="s")
        except Exception:
            return np.nan

    def _safe_read(path: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        try:
            df = pd.read_parquet(path, **read_kwargs)
            if not isinstance(df, pd.DataFrame):
                return None, f"Not a DataFrame (got {type(df)})"
            return df, None
        except Exception as e:
            return None, f"Read error: {e}"

    def _maybe_sample(df: pd.DataFrame) -> pd.DataFrame:
        if not cfg.sample_rows or cfg.sample_rows <= 0:
            return df
        if len(df) <= cfg.sample_rows:
            return df
        return df.sample(n=cfg.sample_rows, random_state=cfg.random_state)

    def _normalize_empty_strings(s: pd.Series) -> pd.Series:
        if not cfg.empty_string_as_null:
            return s
        if not (pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)):
            return s

        def _clean(x):
            if x is None or (isinstance(x, float) and np.isnan(x)):
                return x
            if isinstance(x, str):
                y = x.strip() if cfg.treat_whitespace_as_empty else x
                return np.nan if y == "" else x
            return x

        return s.map(_clean)

    def _robust_center_scale(x: np.ndarray) -> Tuple[float, float]:
        x = x[np.isfinite(x)]
        if x.size == 0:
            return np.nan, np.nan
        med = float(np.median(x))
        mad = float(np.median(np.abs(x - med)))
        scale = mad * 1.4826
        if not np.isfinite(scale) or scale == 0:
            q25, q75 = np.percentile(x, [25, 75])
            iqr = q75 - q25
            scale = float(iqr / 1.349) if iqr > 0 else 1.0
        return med, scale

    def _robust_z(value: float, med: float, scale: float) -> float:
        if not np.isfinite(value) or not np.isfinite(med) or not np.isfinite(scale) or scale == 0:
            return 0.0
        return float((value - med) / scale)

    def _col_signature(df: pd.DataFrame) -> Dict[str, str]:
        return {c: str(df[c].dtype) for c in df.columns}

    def _numeric_stats(s: pd.Series) -> Dict[str, float]:
        s = pd.to_numeric(s, errors="coerce")
        arr = s.to_numpy(dtype=float, copy=False)

        out: Dict[str, float] = {}
        out["null_rate"] = float(np.mean(~np.isfinite(arr))) if arr.size else np.nan

        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            out.update(
                mean=np.nan, std=np.nan, min=np.nan, max=np.nan,
                **({"zero_rate": np.nan} if cfg.consider_zero_rate else {}),
                **{f"q_{q}": np.nan for q in cfg.numeric_quantiles},
            )
            return out

        out["mean"] = float(np.mean(finite))
        out["std"] = float(np.std(finite, ddof=0))
        out["min"] = float(np.min(finite))
        out["max"] = float(np.max(finite))

        if cfg.consider_zero_rate:
            out["zero_rate"] = float(np.mean(finite == 0))

        for q in cfg.numeric_quantiles:
            out[f"q_{q}"] = float(np.quantile(finite, q))
        return out

    def _text_object_stats(s: pd.Series) -> Dict[str, Any]:
        s = _normalize_empty_strings(s)
        n = len(s)
        if n == 0:
            return {"null_rate": np.nan, "empty_rate": np.nan, "topk": [],
                    **{f"len_q_{q}": np.nan for q in cfg.text_len_quantiles}}

        out: Dict[str, Any] = {}
        is_null = s.isna()
        out["null_rate"] = float(is_null.mean())

        if cfg.empty_string_as_null:
            out["empty_rate"] = 0.0
        else:
            def _is_empty(x):
                if not isinstance(x, str):
                    return False
                y = x.strip() if cfg.treat_whitespace_as_empty else x
                return y == ""
            out["empty_rate"] = float(s.map(_is_empty).mean())

        vc = s.value_counts(dropna=True).head(cfg.topk_value_counts)
        out["topk"] = [(k, float(v) / max(1, n)) for k, v in vc.items()]

        nonnull = s.dropna()
        if nonnull.empty:
            for q in cfg.text_len_quantiles:
                out[f"len_q_{q}"] = np.nan
            return out

        def _len(x):
            if isinstance(x, str):
                return len(x.strip()) if cfg.treat_whitespace_as_empty else len(x)
            return np.nan

        lens = nonnull.map(_len).to_numpy(dtype=float)
        lens = lens[np.isfinite(lens)]
        if lens.size == 0:
            for q in cfg.text_len_quantiles:
                out[f"len_q_{q}"] = np.nan
            return out

        for q in cfg.text_len_quantiles:
            out[f"len_q_{q}"] = float(np.quantile(lens, q))
        return out

    def _topk_overlap(a: List[Tuple[Any, float]], b: List[Tuple[Any, float]]) -> float:
        set_a = {x for x, _ in a}
        set_b = {x for x, _ in b}
        if not set_a and not set_b:
            return 1.0
        if not set_a or not set_b:
            return 0.0
        return float(len(set_a & set_b) / min(len(set_a), len(set_b)))

    def _schema_deviations(
        df_cols: Tuple[str, ...],
        df_dtypes: Dict[str, str],
        normal_cols: Tuple[str, ...],
        normal_dtype_sig: Tuple[Tuple[str, str], ...],
    ) -> Dict[str, Any]:
        s_df = set(df_cols)
        s_norm = set(normal_cols)
        missing = sorted(list(s_norm - s_df))
        extra = sorted(list(s_df - s_norm))

        norm_dtype_map = dict(normal_dtype_sig)
        dtype_mismatch: Dict[str, Dict[str, str]] = {}
        for c in (s_df & s_norm):
            got = df_dtypes.get(c, "<missing>")
            exp = norm_dtype_map.get(c, "<missing>")
            if got != exp:
                dtype_mismatch[c] = {"expected": exp, "got": got}

        out: Dict[str, Any] = {}
        if missing:
            out["missing"] = missing
        if extra:
            out["extra"] = extra
        if dtype_mismatch:
            out["dtype_mismatch"] = dtype_mismatch
        if df_cols != normal_cols:
            out["order_mismatch"] = True

        return out

    def _infer_kind(dtype_str: str) -> str:
        numeric_markers = ("int", "float", "Int", "Float", "bool", "boolean")
        return "numeric" if any(m in dtype_str for m in numeric_markers) else "text"

    def _check_parquet_roundtrip(df: pd.DataFrame) -> Tuple[bool, str | float, str | float]:
        """
        Attempt to write to a temp parquet file using one of the configured engines.
        Returns: (ok, error_msg_or_nan, engine_or_nan)
        """
        if not cfg.parquet_check:
            return True, np.nan, np.nan

        # Use a temp file that we delete after attempting.
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".parquet")
            os.close(fd)  # pandas will open it itself

            last_err: Optional[str] = None
            for engine in cfg.parquet_engines:
                try:
                    df.to_parquet(
                        tmp_path,
                        engine=engine,
                        index=False,
                        compression=cfg.parquet_compression,
                    )
                    return True, np.nan, engine
                except ImportError as e:
                    last_err = f"Engine '{engine}' not available: {e}"
                except Exception as e:
                    last_err = f"Engine '{engine}' failed: {type(e).__name__}: {e}"

            return False, (last_err if last_err is not None else "No parquet engines attempted"), np.nan
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    # -----------------------------
    # Pass 1: load, parquet-check, sample, compute per-file stats
    # -----------------------------
    per_file: Dict[str, Any] = {}
    schema_rows: List[Dict[str, Any]] = []

    for path in pkl_paths:
        rec: Dict[str, Any] = {
            "path": path,
            "modified_time": _file_mtime(path),
        }

        df, err = _safe_read(path)
        rec["ok"] = err is None
        rec["error"] = err

        if err is not None:
            rec["parquet_ok"] = False
            rec["parquet_error"] = "Skipped (read failed)"
            rec["parquet_engine"] = np.nan
            schema_rows.append(rec)
            per_file[path] = {"error": err}
            continue

        # Parquet conversion check on the full df (not sampled) because dtype/object issues can hide in sampling.
        parquet_ok, parquet_error, parquet_engine = _check_parquet_roundtrip(df)
        rec["parquet_ok"] = bool(parquet_ok)
        rec["parquet_error"] = parquet_error
        rec["parquet_engine"] = parquet_engine

        # Continue with sampling for distribution checks
        df_s = _maybe_sample(df)

        rec["n_rows"] = int(df_s.shape[0])
        rec["n_cols"] = int(df_s.shape[1])
        rec["columns"] = tuple(df_s.columns.tolist())
        rec["dtypes"] = _col_signature(df_s)
        schema_rows.append(rec)

        stats: Dict[str, Any] = {
            "schema": {"shape": df_s.shape, "columns": rec["columns"], "dtypes": rec["dtypes"]},
            "columns": {},
        }

        for c in df_s.columns:
            s = df_s[c]
            if pd.api.types.is_numeric_dtype(s):
                stats["columns"][c] = {"kind": "numeric", "stats": _numeric_stats(s)}
            else:
                stats["columns"][c] = {"kind": "text", "stats": _text_object_stats(s)}

        per_file[path] = stats

    summary_df = pd.DataFrame(schema_rows)

    ok_paths = summary_df.loc[summary_df["ok"] == True, "path"].tolist()
    if len(ok_paths) < 2:
        summary_df["schema_deviations"] = None
        summary_df["comparable"] = False
        summary_df["file_score"] = np.nan
        summary_df["n_flags"] = np.nan
        summary_df["drift_anomaly"] = np.nan
        summary_df["anomaly"] = (summary_df["ok"] == False) | (summary_df["parquet_ok"] == False)
        details = {"reason": "Not enough readable DataFrames to compare.", "per_file": per_file}
        normal_schema_df = pd.DataFrame(columns=["column", "dtype_expected", "kind", "baseline_metrics"])
        return summary_df, details, normal_schema_df

    # -----------------------------
    # Define "normal schema" (mode columns + mode dtype signature)
    # -----------------------------
    ok_df = summary_df[summary_df["ok"] == True].copy()

    col_mode: Tuple[str, ...] = ok_df["columns"].value_counts().idxmax()

    def dtype_sig(dtypes: Dict[str, str], cols: Tuple[str, ...]) -> Tuple[Tuple[str, str], ...]:
        return tuple((c, dtypes.get(c, "<missing>")) for c in cols)

    ok_df["dtype_sig"] = ok_df.apply(lambda r: dtype_sig(r["dtypes"], r["columns"]), axis=1)

    subset = ok_df[ok_df["columns"] == col_mode]
    if not subset.empty:
        dtype_mode = subset["dtype_sig"].value_counts().idxmax()
    else:
        dtype_mode = ok_df["dtype_sig"].value_counts().idxmax()

    ncols_mode = int(ok_df["n_cols"].value_counts().idxmax())

    def _compute_schema_fields(row) -> Tuple[Optional[Dict[str, Any]], bool]:
        if row.get("ok") is not True:
            return None, False
        dev = _schema_deviations(
            df_cols=row["columns"],
            df_dtypes=row["dtypes"],
            normal_cols=col_mode,
            normal_dtype_sig=dtype_mode,
        )
        comparable = (
            (row["columns"] == col_mode)
            and (dtype_sig(row["dtypes"], row["columns"]) == dtype_mode)
            and (int(row["n_cols"]) == ncols_mode)
        )
        return dev if dev else {}, comparable

    tmp = summary_df.apply(lambda r: _compute_schema_fields(r), axis=1, result_type="expand")
    summary_df["schema_deviations"] = tmp[0]
    summary_df["comparable"] = tmp[1].fillna(False)

    comparable_paths = summary_df.loc[summary_df["comparable"] == True, "path"].tolist()
    if len(comparable_paths) < 2:
        summary_df["file_score"] = np.nan
        summary_df["n_flags"] = np.nan
        summary_df["drift_anomaly"] = np.nan
        # anomaly: unreadable OR not comparable OR parquet fails
        summary_df["anomaly"] = (summary_df["ok"] == False) | (summary_df["comparable"] == False) | (summary_df["parquet_ok"] == False)
        details = {
            "reason": "Too few DataFrames share the normal schema to compare distributions.",
            "per_file": per_file,
            "normal_schema": {"columns": col_mode, "dtype_sig": dtype_mode, "n_cols": ncols_mode},
        }
        normal_schema_df = pd.DataFrame(
            [{"column": c, "dtype_expected": dict(dtype_mode).get(c, "<missing>"),
              "kind": _infer_kind(dict(dtype_mode).get(c, "")), "baseline_metrics": None}
             for c in col_mode]
        )
        return summary_df, details, normal_schema_df

    # -----------------------------
    # Build baseline metrics (robust) + topk medoid for text
    # -----------------------------
    baseline: Dict[str, Any] = {"columns": {}}
    columns_expected = list(col_mode)

    for c in columns_expected:
        first_kind = per_file[comparable_paths[0]]["columns"][c]["kind"]
        baseline["columns"][c] = {"kind": first_kind, "metrics": {}}

        if first_kind == "numeric":
            metric_names = list(per_file[comparable_paths[0]]["columns"][c]["stats"].keys())
            for m in metric_names:
                vals = [per_file[p]["columns"][c]["stats"].get(m, np.nan) for p in comparable_paths]
                arr = np.array(vals, dtype=float)
                med, scale = _robust_center_scale(arr)
                baseline["columns"][c]["metrics"][m] = {"median": med, "scale": scale}
        else:
            numeric_m = [k for k in per_file[comparable_paths[0]]["columns"][c]["stats"].keys() if k != "topk"]
            for m in numeric_m:
                vals = [per_file[p]["columns"][c]["stats"].get(m, np.nan) for p in comparable_paths]
                arr = np.array(vals, dtype=float)
                med, scale = _robust_center_scale(arr)
                baseline["columns"][c]["metrics"][m] = {"median": med, "scale": scale}

            topks = [per_file[p]["columns"][c]["stats"].get("topk", []) for p in comparable_paths]
            if topks:
                overlaps = []
                for i, a in enumerate(topks):
                    ov = []
                    for j, b in enumerate(topks):
                        if i == j:
                            continue
                        ov.append(_topk_overlap(a, b))
                    overlaps.append(float(np.mean(ov)) if ov else 1.0)
                baseline["columns"][c]["topk_ref"] = topks[int(np.argmax(overlaps))]
            else:
                baseline["columns"][c]["topk_ref"] = []

    # Normal schema output DataFrame
    norm_dtype_map = dict(dtype_mode)
    normal_schema_df = pd.DataFrame(
        [
            {
                "column": c,
                "dtype_expected": norm_dtype_map.get(c, "<missing>"),
                "kind": baseline["columns"][c]["kind"],
                "baseline_metrics": baseline["columns"][c].get("metrics", {}),
            }
            for c in columns_expected
        ]
    )

    # -----------------------------
    # Score comparable files
    # -----------------------------
    details: Dict[str, Any] = {
        "baseline": baseline,
        "per_file": per_file,
        "flags": {},
        "normal_schema": {"columns": col_mode, "dtype_sig": dtype_mode, "n_cols": ncols_mode},
    }

    drift_rows: List[Dict[str, Any]] = []
    for path in comparable_paths:
        score = 0.0
        n_flags = 0
        col_flags: Dict[str, Any] = {}

        for c in columns_expected:
            kind = baseline["columns"][c]["kind"]
            stats = per_file[path]["columns"][c]["stats"]

            metric_flags: Dict[str, Any] = {}

            if kind == "numeric":
                for m, bm in baseline["columns"][c]["metrics"].items():
                    z = _robust_z(float(stats.get(m, np.nan)), bm["median"], bm["scale"])
                    if abs(z) >= cfg.z_thresh:
                        metric_flags[m] = z
                        score += min(abs(z), 20.0)
                        n_flags += 1
            else:
                for m, bm in baseline["columns"][c]["metrics"].items():
                    z = _robust_z(float(stats.get(m, np.nan)), bm["median"], bm["scale"])
                    if abs(z) >= cfg.z_thresh:
                        metric_flags[m] = z
                        score += min(abs(z), 20.0)
                        n_flags += 1

                ref_topk = baseline["columns"][c].get("topk_ref", [])
                ov = _topk_overlap(stats.get("topk", []), ref_topk)
                if ov < cfg.min_topk_overlap:
                    metric_flags["topk_overlap"] = ov
                    score += (cfg.z_thresh + (cfg.min_topk_overlap - ov) * 10.0)
                    n_flags += 1

            if metric_flags:
                col_flags[c] = metric_flags

        drift_rows.append(
            {
                "path": path,
                "file_score": float(score),
                "n_flags": int(n_flags),
                "drift_anomaly": bool(score >= cfg.file_score_thresh),
            }
        )
        details["flags"][path] = col_flags

    drift_df = pd.DataFrame(drift_rows)
    summary_df = summary_df.merge(drift_df, on="path", how="left")

    # Final anomaly logic:
    # - unreadable => anomaly
    # - parquet conversion fails => anomaly
    # - readable but not comparable => anomaly (schema anomaly)
    # - comparable and drift score over threshold => anomaly
    summary_df["drift_anomaly"] = summary_df["drift_anomaly"].fillna(False)

    summary_df["anomaly"] = (
        (summary_df["ok"] == False)
        | (summary_df["parquet_ok"] == False)
        | (summary_df["comparable"] == False)
        | (summary_df["drift_anomaly"] == True)
    )

    # Remove bulky columns/dtypes from summary; keep deviations only
    drop_cols = [c for c in ["columns", "dtypes", "dtype_sig"] if c in summary_df.columns]
    summary_df = summary_df.drop(columns=drop_cols, errors="ignore")

    # Sort: worst first. Drift anomalies among comparable float to top.
    summary_df = summary_df.sort_values(
        by=["anomaly", "parquet_ok", "comparable", "file_score", "n_flags", "modified_time"],
        ascending=[False, True, False, False, False, False],
        na_position="last",
    ).reset_index(drop=True)

    return summary_df, details, normal_schema_df
