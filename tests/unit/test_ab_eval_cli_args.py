"""Local subprocess mode must not drop ab_eval task_args.

Regression for 2026-07-21: arms_spec (and eval_set / name / started_by) had no
CLI translation, so a locally-launched test run reached the worker with no
arms at all and failed with "no contracts selected".
"""

import json

from web_interface.process_manager import _task_args_to_cli






def test_ab_eval_args_round_trip_through_cli():
    spec = [{"source": "live", "label": "live"},
            {"source": "candidate", "name": "c1", "label": "c1", "backend": "qwen_local"}]
    argv = _task_args_to_cli("ab_eval", {
        "run_id": "r1", "arms_spec": spec, "eval_set": "myset",
        "name": "my run", "started_by": "admin@admin.net",
    })
    pairs = dict(zip(argv[::2], argv[1::2], strict=False))
    assert pairs["--run-id"] == "r1"
    assert json.loads(pairs["--arms-spec"]) == spec
    assert pairs["--eval-set"] == "myset"
    assert pairs["--name"] == "my run"
    assert pairs["--started-by"] == "admin@admin.net"






def test_generic_keys_not_emitted_for_other_workers():
    argv = _task_args_to_cli("queue_annotator", {
        "batch_size": 5, "name": "not-a-flag", "eval_set": "nope",
    })
    assert "--name" not in argv
    assert "--eval-set" not in argv
    assert argv == ["--batch-size", "5"]
