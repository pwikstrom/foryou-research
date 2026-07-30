"""`run_ingest_refresh._build_per_file_summary` merges the intake drop stats."""

from types import SimpleNamespace

import pandas as pd

from web_interface.run_ingest_refresh import _build_per_file_summary


def _main_collection(final_rows: dict[str, str]) -> SimpleNamespace:
    """Fake main collection whose .data holds one row per (raw_file, cid)."""
    rows = [
        {"raw_file": rf, "collection_id": cid, "item_id": str(i)}
        for i, (rf, cid) in enumerate(final_rows.items())
    ]
    return SimpleNamespace(data=pd.DataFrame(rows))






def test_summary_merges_drop_stats_and_min_row_counts():
    main = _main_collection({"good.zip": "c1"})

    summary = _build_per_file_summary(
        main,
        raw_counts={"good.zip": {"rows": 100, "platform": "tiktok", "source": "ddp"}},
        processed_counts={"good.zip": {"rows": 90, "platform": "tiktok", "source": "ddp"}},
        discarded_at_load={"tiny.zip"},
        existing_raw_files=set(),
        file_stats={
            "good.zip": {"raw_rows": 100, "dropped": {"not_parseable": 10}},
            "tiny.zip": {"raw_rows": 4, "dropped": {}},
        },
    )
    by_name = {e["filename"]: e for e in summary}

    good = by_name["good.zip"]
    assert good["outcome"] == "added_as_new"
    assert good["dropped"] == {"not_parseable": 10}
    assert good["deduped_rows"] == 89  # 90 processed - 1 surviving row in the fake frame

    tiny = by_name["tiny.zip"]
    assert tiny["outcome"] == "discarded_at_load"
    # The true raw count survives (was 0 before the intake stats existed)
    assert tiny["raw_rows"] == 4






def test_summary_without_file_stats_still_works():
    """Backwards compatibility: callers may omit file_stats entirely."""
    main = _main_collection({"good.zip": "c1"})
    summary = _build_per_file_summary(
        main,
        raw_counts={"good.zip": {"rows": 10, "platform": "tiktok", "source": "ddp"}},
        processed_counts={"good.zip": {"rows": 10, "platform": "tiktok", "source": "ddp"}},
        discarded_at_load=set(),
        existing_raw_files=set(),
    )
    assert summary[0]["dropped"] == {}
    assert summary[0]["raw_rows"] == 10
