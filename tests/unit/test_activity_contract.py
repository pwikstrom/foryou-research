"""Unit tests for the activity contract loader.

Cost-free, no network: exercises the declarative contract
(``config/activity_contract.toml``), the required-column / required-core /
platform / derived accessors, and the var_schema overlay payload.
"""

from fyp import activity_contract as ac

_EXPECTED_REQUIRED_COLUMNS = {
    "item_id", "activity_type", "utc_timestamp", "collection_id", "data_source",
    "extra_data", "tz_offset", "raw_file", "source_platform", "ts_added_to_dataset",
    "play_duration",
}
_EXPECTED_CORE = {"activity_type", "utc_timestamp", "collection_id", "data_source", "tz_offset"}
_EXPECTED_DERIVED = {
    "session_id", "local_date", "local_day_segment", "local_hour", "local_timestamp",
    "local_week", "local_weekday", "activity_contract_version",
}




def test_contract_loads_and_validates() -> None:
    """The shipped contract parses and validates with no errors."""
    contract = ac.load_contract()
    assert ac.validate_contract(contract) == []
    print("test_contract_loads_and_validates PASSED")




def test_required_columns() -> None:
    """required_columns returns the ingested base columns (the REQUIRED_COLUMNS analog)."""
    contract = ac.load_contract()
    req = ac.required_columns(contract)
    assert set(req) == _EXPECTED_REQUIRED_COLUMNS, set(req) ^ _EXPECTED_REQUIRED_COLUMNS
    assert req["utc_timestamp"] == "timestamp[ns][pyarrow]"
    assert req["tz_offset"] == "int64[pyarrow]"
    assert req["play_duration"] == "int64[pyarrow]"
    # derived fields are NOT ingested required columns
    for absent in ("session_id", "local_hour", "activity_contract_version"):
        assert absent not in req, absent
    print("test_required_columns PASSED")




def test_required_core_fields() -> None:
    """The hard-drop gate uses exactly the 5 structural fields."""
    contract = ac.load_contract()
    assert set(ac.required_core_fields(contract)) == _EXPECTED_CORE
    # item_id and extra_data are nullable by design and must NOT be required-core
    assert "item_id" not in ac.required_core_fields(contract)
    assert "extra_data" not in ac.required_core_fields(contract)
    print("test_required_core_fields PASSED")




def test_platform_columns() -> None:
    """No platform-scoped fields remain — play_duration was promoted to base."""
    contract = ac.load_contract()
    assert ac.platform_columns(contract, "tiktok") == {}
    assert ac.platform_columns(contract, None) == {}
    assert ac.platforms(contract) == []
    print("test_platform_columns PASSED")




def test_derived_fields() -> None:
    """Derived fields are session_id, the local_* features, and the provenance stamp."""
    contract = ac.load_contract()
    assert ac.derived_fields(contract) == _EXPECTED_DERIVED
    print("test_derived_fields PASSED")




def test_contract_column_metadata() -> None:
    """The overlay payload owns item_id and carries the expected roles/scales."""
    contract = ac.load_contract()
    meta = ac.contract_column_metadata(contract)
    assert "item_id" in meta                                  # activity contract owns item_id
    assert meta["collection_id"]["role"] == "grouping"
    # local_hour and local_day_segment are deliberately role-free: hour-of-day
    # is circular, and day segment varies within a collection-day group.
    assert meta["local_hour"]["role"] is None
    assert meta["local_day_segment"]["role"] is None
    assert meta["local_week"]["role"] == "descriptor"
    assert meta["activity_contract_version"]["role"] == "skip"
    assert meta["utc_timestamp"]["scale"] == "datetime"
    print("test_contract_column_metadata PASSED")




def test_field_digest_stable() -> None:
    """The field digest is a plain dict keyed by field name."""
    contract = ac.load_contract()
    digest = ac.contract_field_digest(contract)
    assert "item_id" in digest["fields"]
    assert digest["fields"]["play_duration"]["scope"] == "base"
    print("test_field_digest_stable PASSED")




if __name__ == "__main__":
    test_contract_loads_and_validates()
    test_required_columns()
    test_required_core_fields()
    test_platform_columns()
    test_derived_fields()
    test_contract_column_metadata()
    test_field_digest_stable()
    print("All tests passed.")
