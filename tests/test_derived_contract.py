"""Unit tests for the derived contract loader.

Cost-free, no network: exercises ``config/derived_contract.toml`` and the
metadata / digest accessors.
"""

from fyp import derived_contract as dc

_EXPECTED = {"days_since_created", "completion_rate", "scraped_fail", "niche", "niche_name"}




def test_contract_loads_and_validates() -> None:
    """The shipped contract parses and validates with no errors."""
    contract = dc.load_contract()
    assert dc.validate_contract(contract) == []
    print("test_contract_loads_and_validates PASSED")




def test_owns_the_calc_and_niche_columns() -> None:
    """The derived contract owns the 5 merge-time columns, not plays_per_day."""
    contract = dc.load_contract()
    meta = dc.contract_column_metadata(contract)
    assert set(meta) == _EXPECTED, set(meta) ^ _EXPECTED
    assert "plays_per_day" not in meta                       # scrape-owned
    assert meta["completion_rate"]["role"] == "feature"
    assert meta["niche_name"]["role"] == "feature"
    assert meta["niche"]["role"] == "skip" and meta["niche"]["scale"] == "raw"
    assert meta["scraped_fail"]["role"] == "skip"
    print("test_owns_the_calc_and_niche_columns PASSED")




def test_derived_fields_and_digest() -> None:
    """derived_fields and the digest cover all five columns."""
    contract = dc.load_contract()
    assert dc.derived_fields(contract) == _EXPECTED
    assert set(dc.contract_field_digest(contract)["fields"]) == _EXPECTED
    print("test_derived_fields_and_digest PASSED")




if __name__ == "__main__":
    test_contract_loads_and_validates()
    test_owns_the_calc_and_niche_columns()
    test_derived_fields_and_digest()
    print("All tests passed.")
