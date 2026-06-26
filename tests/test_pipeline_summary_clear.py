"""Ad-hoc test for _write_pipeline_summary_cloud impact-clearing + partial flags.

Full success → consolidation_impact is cleared; partial/aborted → impact kept
and last_pipeline_partial / last_pipeline_failed_at recorded.
"""

import web_interface.routes.process_routes as pr


def _setup(impact):
    pr.load_process_stats = lambda: None  # no-op (don't reload from GCS)
    pr.save_process_stats = lambda: None
    pr.process_stats = {
        "consolidate_enrichment": {
            "consolidation_impact": impact,
            "last_run_end_time": "2026-06-26T01:00:00+00:00",
        }
    }


def run():
    impact = {
        "affected_study_names": ["StudyA"],
        "affected_collection_ids": ["c1"],
        "timestamp": "2026-06-26T00:59:00+00:00",
    }

    # Full success → impact cleared, partial=False.
    _setup(impact)
    pr._write_pipeline_summary_cloud(partial=False, failed_at=None)
    entry = pr.process_stats["consolidate_enrichment"]
    assert "consolidation_impact" not in entry, entry
    assert entry["last_pipeline_partial"] is False, entry
    assert entry["last_pipeline_failed_at"] is None, entry
    assert entry.get("last_pipeline_summary"), entry

    # Partial → impact kept, flags recorded.
    _setup(impact)
    pr._write_pipeline_summary_cloud(partial=True, failed_at="video_map_refresh")
    entry = pr.process_stats["consolidate_enrichment"]
    assert entry.get("consolidation_impact") == impact, entry
    assert entry["last_pipeline_partial"] is True, entry
    assert entry["last_pipeline_failed_at"] == "video_map_refresh", entry
    assert "aborted at 'video_map_refresh'" in entry["last_pipeline_summary"], entry["last_pipeline_summary"]

    print("ALL PIPELINE SUMMARY-CLEAR TESTS PASSED")


if __name__ == "__main__":
    run()
