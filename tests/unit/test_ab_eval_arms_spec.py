"""Validation of the explicit test-arm list (`arms_spec`) on the run route.

The same contract may appear as several arms under distinct labels (e.g. once
per backend); labels are the report/manifest keys so they must be unique.
"""

from web_interface.routes.management.ab_eval import _clean_arms_spec






def test_valid_spec_with_duplicate_contract_distinct_labels():
    cleaned, err = _clean_arms_spec([
        {"source": "live", "label": "live"},
        {"source": "live", "label": "live~2", "backend": "qwen_local"},
        {"source": "candidate", "name": "my-cand", "label": "my-cand", "backend": ""},
    ])
    assert err is None
    assert [a["label"] for a in cleaned] == ["live", "live~2", "my-cand"]
    assert cleaned[1]["backend"] == "qwen_local"
    assert "backend" not in cleaned[2]  # "" = gemini default, omitted
    assert cleaned[2]["name"] == "my-cand"






def test_rejects_bad_specs():
    assert _clean_arms_spec([])[1] is not None
    assert _clean_arms_spec("nope")[1] is not None
    assert _clean_arms_spec([{"source": "other", "label": "x"}])[1] is not None
    assert _clean_arms_spec([{"source": "live", "label": ""}])[1] is not None
    assert _clean_arms_spec([{"source": "candidate", "name": "Bad Name!", "label": "x"}])[1] is not None
    assert _clean_arms_spec([{"source": "live", "label": "a"},
                             {"source": "live", "label": "a"}])[1] is not None
    assert _clean_arms_spec([{"source": "live", "label": "a", "backend": "nope"}])[1] is not None
