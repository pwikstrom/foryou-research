#!/usr/bin/env python3
"""Tests for the retroactive hashtag-cleanup worker (run_retokenise_hashtags).

Drives the worker against a stubbed data_io holding an in-memory scrape parquet
and a monkeypatched stoplist, asserting it re-tokenises desc_hashtags from
desc_raw, rewrites only changed files, and preserves the list<string> dtype.

Usage:
    PYTHONPATH=. python tests/unit/test_retokenise_hashtags.py
    pytest tests/unit/test_retokenise_hashtags.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import pyarrow as pa

from fyp import irrelevant_words as iw
from fyp.recode_variables import recode_tokenise
from web_interface import run_retokenise_hashtags as worker

_LIST_STR = pd.ArrowDtype(pa.list_(pa.string()))




class _CapturingReporter:
    def __init__(self):
        self.data = None
        self.logs = []

    def update_progress(self, *a, **k):
        pass

    def emit_data(self, payload):
        self.data = payload

    def log(self, msg):
        self.logs.append(msg)

    def complete(self, data=None):
        pass

    def fail(self, error):
        pass

    def check_cancelled(self):
        return False




class _StubDataIO:
    """In-memory scrape store: {filename: DataFrame}. Records saves."""

    def __init__(self, files):
        self.files = files
        self.saved = {}

    def listdir(self, storage_location=None, verbose=False):
        return list(self.files.keys())

    def load_parquet(self, storage_location=None, filename=None, **k):
        return self.files[filename].copy()

    def save_parquet(self, df=None, storage_location=None, filename=None, **k):
        self.saved[filename] = df.copy()
        self.files[filename] = df.copy()




def _run_with(files, stoplist):
    """Run the worker over `files` with a monkeypatched stoplist + data_io."""
    stub = _StubDataIO(files)
    orig_io = worker.__dict__.get("_data_io")
    orig_load_words = iw.load_words
    iw.load_words = lambda: stoplist
    # The worker does `import fyp.data_io as data_io` internally; patch the module.
    import fyp.data_io as real_io
    saved = {"listdir": real_io.listdir, "load_parquet": real_io.load_parquet, "save_parquet": real_io.save_parquet}
    real_io.listdir = stub.listdir
    real_io.load_parquet = stub.load_parquet
    real_io.save_parquet = stub.save_parquet
    reporter = _CapturingReporter()
    try:
        worker.run_retokenise_hashtags(reporter, {})
    finally:
        iw.load_words = orig_load_words
        real_io.listdir = saved["listdir"]
        real_io.load_parquet = saved["load_parquet"]
        real_io.save_parquet = saved["save_parquet"]
    return stub, reporter




def _frame(item_ids, descs, hashtags):
    return pd.DataFrame({
        "item_id": pd.Series(item_ids, dtype="string[pyarrow]"),
        "desc_raw": pd.Series(descs, dtype="string[pyarrow]"),
        "desc_hashtags": pd.Series(hashtags, dtype=_LIST_STR),
    })




def test_rewrites_stale_hashtags():
    """A stale desc_hashtags is re-tokenised from desc_raw with the current list."""
    # Old exact-match list missed 'fyyyyp'; new squeeze list catches it via 'fyp'.
    df = _frame(
        ["1", "2"],
        ["#fyyyyp #dance", "#dance #cat"],
        [["fyyyyp", "dance"], ["dance", "cat"]],
    )
    stub, reporter = _run_with({"scrapes_100.parquet": df}, ["fyp"])

    assert "scrapes_100.parquet" in stub.saved, "changed file must be written"
    out = stub.saved["scrapes_100.parquet"]
    assert list(out["desc_hashtags"].iloc[0]) == ["dance"]      # fyyyyp dropped
    assert list(out["desc_hashtags"].iloc[1]) == ["dance", "cat"]  # unchanged row
    # Matches exactly what a fresh recode_tokenise would produce.
    expected = recode_tokenise(df["desc_raw"]).map(lambda d: d["hashtags"])
    assert list(out["desc_hashtags"].iloc[0]) == expected.iloc[0]
    assert reporter.data["files_changed"] == 1
    assert reporter.data["rows_changed"] == 1
    print("PASS: rewrites stale hashtags")




def test_noop_file_not_rewritten():
    """A file whose hashtags already match the stoplist is left untouched."""
    df = _frame(["1"], ["#dance"], [["dance"]])
    stub, reporter = _run_with({"scrapes_200.parquet": df}, ["fyp"])
    assert stub.saved == {}, "unchanged file must not be written"
    assert reporter.data["files_changed"] == 0
    assert reporter.data["rows_changed"] == 0
    print("PASS: no-op file not rewritten")




def test_dtype_preserved():
    """The rewritten desc_hashtags keeps the list<string>[pyarrow] dtype."""
    df = _frame(["1"], ["#fyppp #dance"], [["fyppp", "dance"]])
    stub, _ = _run_with({"scrapes_300.parquet": df}, ["fyp"])
    out = stub.saved["scrapes_300.parquet"]
    assert out["desc_hashtags"].dtype == _LIST_STR, out["desc_hashtags"].dtype
    assert list(out["desc_hashtags"].iloc[0]) == ["dance"]
    print("PASS: dtype preserved")




def test_missing_columns_skipped():
    """A scrape file without desc_raw/desc_hashtags is skipped, not an error."""
    df = pd.DataFrame({"item_id": pd.Series(["1"], dtype="string[pyarrow]")})
    stub, reporter = _run_with({"scrapes_400.parquet": df}, ["fyp"])
    assert stub.saved == {}
    assert reporter.data["files_scanned"] == 1
    assert reporter.data["files_changed"] == 0
    print("PASS: missing-columns file skipped")




def test_na_desc_raw_left_untouched():
    """A row with a missing desc_raw keeps its stored hashtags."""
    df = _frame(["1", "2"], [pd.NA, "#fyyyyp"], [["kept", "asis"], ["fyyyyp"]])
    stub, reporter = _run_with({"scrapes_500.parquet": df}, ["fyp"])
    out = stub.saved["scrapes_500.parquet"]
    assert list(out["desc_hashtags"].iloc[0]) == ["kept", "asis"]  # NA row untouched
    assert list(out["desc_hashtags"].iloc[1]) == []                # fyyyyp dropped
    assert reporter.data["rows_changed"] == 1
    print("PASS: NA desc_raw left untouched")




if __name__ == "__main__":
    test_rewrites_stale_hashtags()
    test_noop_file_not_rewritten()
    test_dtype_preserved()
    test_missing_columns_skipped()
    test_na_desc_raw_left_untouched()
    print("All retokenise-hashtags worker tests passed.")
