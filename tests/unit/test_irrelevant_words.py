#!/usr/bin/env python3
"""Tests for the admin-editable hashtag stoplist (``fyp.irrelevant_words``).

Covers squeeze/normalize edge cases, the squeeze + prefix-wildcard matcher,
save-time dedupe and validation, etag conflicts, config seeding, and the
``recode_tokenise`` integration.

Usage:
    python tests/unit/test_irrelevant_words.py
    pytest tests/unit/test_irrelevant_words.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fyp import irrelevant_words as iw




class _StubDataIO:
    """In-memory JSON store; never touches real storage."""

    def __init__(self, payload=None):
        self.payload = payload
        self.saves = 0

    def exists(self, storage_location=None, filename=None):
        return self.payload is not None

    def load_json(self, storage_location=None, filename=None):
        return self.payload

    def save_json(self, data=None, storage_location=None, filename=None):
        self.payload = data
        self.saves += 1




def _with_stub(stub, fn):
    """Run ``fn`` with ``iw._data_io`` replaced by ``stub``."""
    original = iw._data_io
    iw._data_io = lambda: stub
    try:
        return fn()
    finally:
        iw._data_io = original




def test_squeeze():
    """Runs of one code point collapse; multi-codepoint sequences pass through."""
    assert iw.squeeze("fyyyyp") == "fyp"
    assert iw.squeeze("fyppppppppppppppppppppppp") == "fyp"
    assert iw.squeeze("all") == "al"
    assert iw.squeeze("fyp") == "fyp"
    # シ + ゚ are different code points — not a run, untouched.
    assert iw.squeeze("fypシ゚") == "fypシ゚"
    assert iw.squeeze("fypシシ") == "fypシ"
    assert iw.squeeze("-year-old") == "-year-old"
    assert iw.squeeze("") == ""
    print("PASS: squeeze")




def test_normalize_entry():
    """Lowercase/strip; empty, bare-*, and too-short wildcards normalize away."""
    assert iw.normalize_entry("  FYP ") == "fyp"
    assert iw.normalize_entry("fyp*") == "fyp*"
    assert iw.normalize_entry("") == ""
    assert iw.normalize_entry("   ") == ""
    assert iw.normalize_entry("*") == ""
    # Squeezed prefix "f" is shorter than MIN_WILDCARD_PREFIX.
    assert iw.normalize_entry("f*") == ""
    assert iw.normalize_entry("ff*") == ""
    assert iw.normalize_entry("fy*") == "fy*"
    print("PASS: normalize_entry")




def test_matcher_exact_and_wildcard():
    """One entry catches its elongations; * entries match squeezed prefixes."""
    match = iw.build_matcher(["fyp", "all", "foryou", "viral*", "fypシ"])
    # Exact via squeeze.
    assert match("fyp")
    assert match("fyyyyp")
    assert match("fypp")
    assert match("fyppppppppppppppppppppppp")
    assert match("foryouu")
    assert match("alllll")
    assert match("al")  # "all" squeezes to "al" — same matching class
    assert match("fypシ")
    # Prefix wildcard.
    assert match("viral")
    assert match("viralvideo")
    assert match("viiiralvideos")
    # Non-matches survive.
    assert not match("alps")
    assert not match("fypage")  # no fyp* entry in this list
    assert not match("dance")
    print("PASS: matcher")




def test_matcher_no_wildcards():
    """An all-exact list must not prefix-match anything (empty-tuple guard)."""
    match = iw.build_matcher(["fyp"])
    assert not match("fypage")
    assert match("fyp")
    print("PASS: matcher without wildcards")




def test_dedupe_words():
    """Entries that squeeze identically collapse to the first one, sorted output."""
    out = iw.dedupe_words(["fyp", "fypp", "fyppp", "FYP", " viral ", "viral", "fyp*", "", "*", "f*"])
    assert out == ["fyp", "fyp*", "viral"]
    print("PASS: dedupe_words")




def test_save_and_load_roundtrip():
    """save_words persists a cleaned list; load_words returns it."""
    stub = _StubDataIO()

    def run():
        result = iw.save_words(["FYP", "fypp", "viral*"], updated_by="tester")
        assert result["words"] == ["fyp", "viral*"]
        assert stub.payload["updated_by"] == "tester"
        assert iw.load_words() == ["fyp", "viral*"]
        return result

    result = _with_stub(stub, run)
    assert result["etag"] != "missing"
    print("PASS: save/load roundtrip")




def test_save_rejects_invalid_entries():
    """Malformed payloads raise ValueError before anything is written."""
    stub = _StubDataIO()

    def run():
        for bad in (["ok", ""], ["*"], ["f*"], "not-a-list", ["ok", 3]):
            try:
                iw.save_words(bad)  # type: ignore[arg-type]
                raise AssertionError(f"save_words accepted {bad!r}")
            except ValueError:
                pass
        assert stub.saves == 0

    _with_stub(stub, run)
    print("PASS: save validation")




def test_etag_conflict():
    """A stale etag refuses the save; the matching etag allows it."""
    stub = _StubDataIO()

    def run():
        first = iw.save_words(["fyp"])
        try:
            iw.save_words(["fyp", "viral"], expected_etag="stale")
            raise AssertionError("stale etag accepted")
        except iw.IrrelevantWordsConflict:
            pass
        assert iw.load_words() == ["fyp"]
        second = iw.save_words(["fyp", "viral"], expected_etag=first["etag"])
        assert second["words"] == ["fyp", "viral"]

    _with_stub(stub, run)
    print("PASS: etag conflict")




def test_load_words_seeds_from_config():
    """A missing store is seeded (deduped) from the config list, once."""
    stub = _StubDataIO()
    original_cf = iw._cf
    iw._cf = lambda: {"labels": {"IRRELEVANT_WORDS": ["fyp", "fypp", "viral"]}}
    try:
        def run():
            words = iw.load_words()
            assert words == ["fyp", "viral"]
            assert stub.payload["updated_by"] == "config-seed"
            # Second load reads the store, no re-seed.
            saves_before = stub.saves
            assert iw.load_words() == ["fyp", "viral"]
            assert stub.saves == saves_before

        _with_stub(stub, run)
    finally:
        iw._cf = original_cf
    print("PASS: config seeding")




def test_recode_tokenise_uses_store():
    """Hashtag extraction drops squeeze/wildcard matches, keeps the rest + emoji."""
    from fyp import recode_variables as rv

    original = iw.load_words
    iw.load_words = lambda: ["fyp", "foryou*", "all"]
    try:
        out = rv.recode_tokenise("#fyyyyp #foryoupage #alps #dance ignored #al #❤️")
    finally:
        iw.load_words = original
    # fyyyyp → fyp (squeeze), foryoupage → foryou* (wildcard), al ~ all (squeeze).
    assert out == {"hashtags": ["alps", "dance", "❤️"]}
    print("PASS: recode_tokenise integration")




def test_hash_unaffected_by_stoplist():
    """The stoplist is not part of the study hash — edits must not change it."""
    from fyp.recode_variables import compute_var_schema_hash

    original = iw.load_words
    iw.load_words = lambda: ["fyp"]
    try:
        h1 = compute_var_schema_hash()
    finally:
        iw.load_words = original
    iw.load_words = lambda: ["fyp", "somethingelse", "viral*"]
    try:
        h2 = compute_var_schema_hash()
    finally:
        iw.load_words = original
    assert h1 == h2
    print("PASS: schema hash unaffected")




if __name__ == "__main__":
    test_squeeze()
    test_normalize_entry()
    test_matcher_exact_and_wildcard()
    test_matcher_no_wildcards()
    test_dedupe_words()
    test_save_and_load_roundtrip()
    test_save_rejects_invalid_entries()
    test_etag_conflict()
    test_load_words_seeds_from_config()
    test_recode_tokenise_uses_store()
    test_hash_unaffected_by_stoplist()
    print("All irrelevant-words tests passed.")
