"""Legacy skip-list entries carry their own ledger outcome.

The old flat ``discarded_collection_files.json`` recorded a filename and
nothing else. Seeding those entries as ``discarded_at_load`` with raw_rows 0
made the ingestion history report a reason ("too few rows") and a count the
file never held — on a real install, for hundreds of files at once.
"""

from fyp.ingest.base import (
    LEDGER_SKIP_OUTCOMES,
    LEGACY_MIGRATION_NOTE,
    ForYouCollection,
)


def _upgrade(files):
    """Run the real in-place upgrade over a bare ledger dict."""
    shim = type(
        "_Shim", (),
        {"_upgrade_legacy_ledger_entries": ForYouCollection._upgrade_legacy_ledger_entries},
    )()
    shim.ledger = {"files": files}
    shim._upgrade_legacy_ledger_entries()
    return files


def test_migrated_entries_are_restamped():
    files = _upgrade({
        "a.zip": {
            "outcome": "discarded_at_load", "raw_rows": 0, "kept_rows": 0,
            "notes": LEGACY_MIGRATION_NOTE,
        },
    })
    assert files["a.zip"]["outcome"] == "skipped_legacy"


def test_migrated_entries_lose_their_invented_zero_counts():
    files = _upgrade({
        "a.zip": {
            "outcome": "discarded_at_load", "raw_rows": 0, "kept_rows": 0,
            "notes": LEGACY_MIGRATION_NOTE,
        },
    })
    assert files["a.zip"]["raw_rows"] is None
    assert files["a.zip"]["kept_rows"] is None


def test_a_real_too_few_rows_discard_is_left_alone():
    files = _upgrade({
        "b.zip": {"outcome": "discarded_at_load", "raw_rows": 3, "kept_rows": 0},
    })
    assert files["b.zip"] == {
        "outcome": "discarded_at_load", "raw_rows": 3, "kept_rows": 0,
    }


def test_an_ingested_file_is_left_alone():
    files = _upgrade({
        "c.zip": {"outcome": "added_as_new", "raw_rows": 100, "kept_rows": 90},
    })
    assert files["c.zip"]["outcome"] == "added_as_new"


def test_upgrade_is_idempotent():
    entry = {
        "outcome": "discarded_at_load", "raw_rows": 0, "kept_rows": 0,
        "notes": LEGACY_MIGRATION_NOTE,
    }
    first = dict(_upgrade({"a.zip": dict(entry)})["a.zip"])
    twice = _upgrade(_upgrade({"a.zip": dict(entry)}))["a.zip"]
    assert first == twice


def test_upgrade_tolerates_a_malformed_entry():
    files = _upgrade({"a.zip": None, "b.zip": "junk"})
    assert files == {"a.zip": None, "b.zip": "junk"}


def test_legacy_outcome_still_means_do_not_reload():
    # The whole point of the entry is that the file stays skipped; only the
    # label it is reported under changes.
    assert "skipped_legacy" in LEDGER_SKIP_OUTCOMES
