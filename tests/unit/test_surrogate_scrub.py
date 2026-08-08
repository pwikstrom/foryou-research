"""Lone-surrogate scrubbing (2026-08-08 consolidation outage).

Gemini batch output for one item contained the malformed escape
``\\uD83E\\uFA79`` (intended: ``\\uD83E\\uDE79`` = U+1FA79). ``json.loads``
keeps the lone high surrogate, and once recoding split the value into a
list-of-strings column the old scalar-only ``fix_surrogates`` net missed it,
crashing the refined-parquet write and with it the whole consolidation.
"""

import io
import json

import pandas as pd
import pytest

from fyp.types import (
    contains_surrogates,
    convert_dtypes_to_pyarrow,
    fix_surrogates,
    scrub_surrogates_nested,
)


LONE = json.loads('"\\u2764\\uFE0F\\u200D\\uD83E\\uFA79"')  # ❤️‍ + lone \ud83e + 啕






def test_json_loads_keeps_lone_surrogate():
    assert contains_surrogates(LONE)
    with pytest.raises(UnicodeEncodeError):
        LONE.encode("utf-8")






def test_contains_surrogates_negative_cases():
    assert not contains_surrogates("plain ❤️ text 🩹")
    assert not contains_surrogates(None)
    assert not contains_surrogates(123)
    assert not contains_surrogates(["\ud83e"])  # non-str: False by contract






def test_scrub_scalar_replaces_lone_surrogate():
    fixed = scrub_surrogates_nested(LONE)
    assert not contains_surrogates(fixed)
    fixed.encode("utf-8")  # must not raise
    assert fixed.startswith("❤️‍")






def test_scrub_recombines_adjacent_surrogate_pair():
    # A high+low pair stored as two codepoints becomes the intended emoji.
    paired = json.loads('"\\uD83E\\uDE79"')
    assert scrub_surrogates_nested(paired) == "🩹"






def test_scrub_recurses_into_nested_structures():
    nested = {
        "text_overlays": [LONE, "T&S"],
        "faces": [{"note": LONE, "age": 34}],
        "as_tuple": (LONE,),
        "untouched": 42,
    }
    fixed = scrub_surrogates_nested(nested)
    assert not contains_surrogates(fixed["text_overlays"][0])
    assert fixed["text_overlays"][1] == "T&S"
    assert not contains_surrogates(fixed["faces"][0]["note"])
    assert fixed["faces"][0]["age"] == 34
    assert isinstance(fixed["as_tuple"], tuple)
    assert not contains_surrogates(fixed["as_tuple"][0])
    assert fixed["untouched"] == 42
    # Clean values pass through unchanged (and str identity is preserved).
    clean = "no surrogates here"
    assert scrub_surrogates_nested(clean) is clean






def test_fix_surrogates_scalar_only_contract_unchanged():
    # The scalar helper still ignores non-str values (documented behavior the
    # nested scrubber compensates for).
    bad_list = [LONE]
    assert fix_surrogates(bad_list) is bad_list






def test_convert_dtypes_scrubs_list_of_string_column():
    # Regression: a list-of-strings object column carrying a lone surrogate
    # must survive convert_dtypes_to_pyarrow + a parquet write.
    df = pd.DataFrame(
        {
            "item_id": ["a", "b"],
            "text_overlays": [[LONE, "T&S"], ["ok"]],
        }
    )
    out = convert_dtypes_to_pyarrow(df)
    buf = io.BytesIO()
    out.to_parquet(buf)  # raised UnicodeEncodeError before the fix
    assert buf.getbuffer().nbytes > 0
