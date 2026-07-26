"""Run manifests record each arm's underlying candidate name.

The arm label is the report/manifest key and diverges from the candidate name
when one candidate runs as several arms (``foo``, ``foo~2``); the Graduate
button needs the real candidate name, so ``execute_run`` persists it per arm
(empty for live arms and pre-existing callers that don't pass it).
"""

import fyp.ab_eval as ab_eval
import fyp.annotation_contract as ac
import fyp.data_io as data_io






class _StubRunner:
    """Minimal runner double returning empty-but-valid rows."""

    def run(self, prompt_text, response_schema, item_ids, platform_map, progress_cb=None):
        return [{"item_id": str(i), "model": "stub", "parsed": None, "response": "",
                 "finish_reason": "DNF - stub", "usage": {}, "inference_duration": 0.0,
                 "error": "stub"} for i in item_ids]






def test_manifest_records_candidate_name():
    live_text = ac._read_baked_text()
    run_id = ab_eval.new_run_id()
    try:
        ab_eval.execute_run(
            run_id=run_id,
            arms=[{"name": "live", "source": "live", "text": live_text},
                  {"name": "my-cand", "source": "candidate", "text": live_text,
                   "candidate": "my-cand"},
                  {"name": "my-cand~2", "source": "candidate", "text": live_text,
                   "candidate": "my-cand"}],
            item_ids=["1"],
            started_by="tester",
            runner=_StubRunner(),
        )
        manifest = data_io.load_json(storage_location=ab_eval.LOCATION,
                                     filename=ab_eval._run_file(run_id, "manifest.json"))
        by_name = {a["name"]: a for a in manifest["arms"]}
        assert by_name["live"]["candidate"] == ""
        assert by_name["my-cand"]["candidate"] == "my-cand"
        assert by_name["my-cand~2"]["candidate"] == "my-cand"
    finally:
        ab_eval.delete_run(run_id)






def test_legacy_arms_without_candidate_still_run():
    live_text = ac._read_baked_text()
    run_id = ab_eval.new_run_id()
    try:
        ab_eval.execute_run(
            run_id=run_id,
            arms=[{"name": "old-cand", "source": "candidate", "text": live_text}],
            item_ids=["1"],
            started_by="tester",
            runner=_StubRunner(),
        )
        manifest = data_io.load_json(storage_location=ab_eval.LOCATION,
                                     filename=ab_eval._run_file(run_id, "manifest.json"))
        assert manifest["arms"][0]["candidate"] == ""
    finally:
        ab_eval.delete_run(run_id)
