"""The Machine Annotations overlay column: model labels + NA-safe masking.

Regression for 2026-07-21: annotated_ok is bool[pyarrow] and can hold NA;
the model-label mask must fill NA before numpy coercion (NA raised
"boolean value of NA is ambiguous" and 500'd every Explore/Viewer request).
"""

import pandas as pd

from web_interface.services import study_data


def _frame(annotated_ok_values, versions):
    return pd.DataFrame({
        "item_id": pd.array([f"id{i}" for i in range(len(annotated_ok_values))],
                            dtype="string[pyarrow]"),
        "annotated_ok": pd.array(annotated_ok_values, dtype="bool[pyarrow]"),
        "annotation_version": pd.array(versions, dtype="string[pyarrow]"),
    })






def test_overlay_handles_na_annotated_ok(monkeypatch):
    monkeypatch.setattr(study_data, "_annotation_model_labels",
                        lambda: {"av_abc": "test-model-1"})
    df = _frame([True, False, None], ["av_abc", None, None])

    out, col_types = study_data.enrich_with_user_tags(df, {}, "user@example.com")

    assert col_types["Machine Annotations"] == "category"
    assert list(out["Machine Annotations"]) == [
        "test-model-1", "Cannot Machine Annotate", "Not Attempted"]






def test_overlay_unknown_version_falls_back_to_generic(monkeypatch):
    monkeypatch.setattr(study_data, "_annotation_model_labels",
                        lambda: {"av_other": "some-model"})
    df = _frame([True], ["av_unknown"])

    out, _ = study_data.enrich_with_user_tags(df, {}, "user@example.com")

    assert list(out["Machine Annotations"]) == ["Machine Annotated"]
