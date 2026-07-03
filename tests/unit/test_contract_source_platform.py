"""Pin the scrape contract's source_platform field semantics.

The field must be part of the scrape dtype set (so every scraped row carries
its platform) and part of the hash digest (sv_ provenance), but must NOT
appear in contract_column_metadata — the ACTIVITY contract owns the
source_platform var_schema row, and double ownership would make the two
contracts fight over role/scale/display_name.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fyp import registry_metadata
from fyp import scrape_contract as sc


def main() -> int:
    contract = sc.load_contract()

    errors = sc.validate_contract(contract)
    assert not errors, f"contract must validate, got: {errors}"

    dtypes = sc.field_dtypes(contract)
    assert dtypes.get("source_platform") == "string[pyarrow]", dtypes.get("source_platform")

    meta = sc.contract_column_metadata(contract)
    assert "source_platform" not in meta, (
        "source_platform must not be metadata-owned by the scrape contract — "
        "the activity contract owns the var_schema row"
    )

    digest = sc.contract_field_digest(contract)
    assert "source_platform" in str(digest), "field must participate in the sv_ hash"

    # The registry snapshot path must handle the metadata-less field silently
    snapshot = registry_metadata.snapshot_field_metadata(sc)
    assert "source_platform" not in snapshot
    assert len(snapshot) > 0, "snapshot should still carry the metadata-owning fields"

    # var_schema synthesis: exactly one source_platform row, owned by activity
    from fyp.fyp_config import fyp_cf
    vs = fyp_cf["var_schema"]
    rows = vs[vs["variable_name"] == "source_platform"]
    assert len(rows) == 1, f"expected exactly one var_schema row, got {len(rows)}"
    assert rows.iloc[0]["source"] == "activity", rows.iloc[0]["source"]

    print("OK — scrape-contract source_platform semantics pinned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
