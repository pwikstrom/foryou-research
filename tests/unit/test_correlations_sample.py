"""Correlations unit-of-analysis payload: the study IS the sample.

The tab has no filter/sample panel by design — exclusions and event windows
belong in study definitions, where they are versioned and documented. These
tests pin the whole-study contract: the metadata payload ships no filter
machinery, and the unit banner carries the study-wide video count.
"""

import pandas as pd

from web_interface.services import correlations_service as svc






def _frame():
    """Three collections × ten days, with a per-group video count."""
    rows = []
    for collection in ("c1", "c2", "c3"):
        for day in range(1, 11):
            rows.append({
                "collection_id": collection,
                "local_date": f"2026-03-{day:02d}",
                "local_weekday": "Mon" if day % 2 else "Tue",
                "score": float(day),
                svc.VIDEOS_WATCHED_COL: 10 + day,
            })
    return pd.DataFrame(rows)






def test_total_videos_prefers_videos_watched_with_legacy_fallback():
    df = _frame()
    assert svc.total_videos(df) == sum(10 + d for d in range(1, 11)) * 3

    legacy = df.rename(columns={svc.VIDEOS_WATCHED_COL: svc.GROUP_SIZE_COL})
    assert svc.total_videos(legacy) == svc.total_videos(df)

    neither = df.drop(columns=[svc.VIDEOS_WATCHED_COL])
    assert svc.total_videos(neither) is None






def test_metadata_payload_ships_no_filter_machinery(monkeypatch):
    df = _frame()
    monkeypatch.setattr(svc, "get_factors_and_features_from_var_schema",
                        lambda **kw: (["collection_id", "local_weekday"], []))
    monkeypatch.setattr(svc, "get_grouping_factors_from_var_schema",
                        lambda **kw: ["collection_id", "local_date"])
    monkeypatch.setattr(svc, "load_interpretations", lambda study: {})
    monkeypatch.setattr(svc, "load_display_id_map", lambda: {})
    monkeypatch.setattr(svc, "load_schema_metadata", lambda m: {})

    payload = svc.build_metadata_payload(df, "mystudy")

    for retired_key in ("factor_values", "factor_ranges", "truncated_factors",
                        "filter_immune_views", "sample", "split_cols", "split_levels"):
        assert retired_key not in payload, retired_key
    assert payload["unit"]["n_groups"] == len(df)
    assert payload["unit"]["videos_total"] == svc.total_videos(df)






def test_scatter_and_matrix_cover_the_whole_study(monkeypatch):
    df = _frame()
    monkeypatch.setattr(svc, "get_factors_and_features_from_var_schema",
                        lambda **kw: (["collection_id"], []))
    monkeypatch.setattr(svc, "load_interpretations", lambda study: {})
    monkeypatch.setattr(svc, "load_display_id_map", lambda: {})

    scatter = svc.build_scatter_payload(df, "score", svc.VIDEOS_WATCHED_COL, "collection_id")
    assert scatter["total_count"] == len(df)
    assert "sample" not in scatter

    matrix, err = svc.build_matrix_payload(df, "mystudy")
    assert err is None
    assert matrix["count"] == len(df)
    assert "sample" not in matrix
