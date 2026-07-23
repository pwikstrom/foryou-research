"""Per-arm backend/model/temperature overrides in the A/B eval harness.

Old-arm compatibility (arms without the new keys) is pinned by the untouched
``test_ab_eval.py``; this file covers the new plumbing: manifest recording,
the runner factory, per-call override application, and backend fail-fast.
"""

import json

import pytest

import fyp.ab_eval as ab_eval
import fyp.annotation_contract as ac
import fyp.data_io as data_io
from fyp.fyp_config import get_config






class _StubRunner:
    """Minimal runner double returning empty-but-valid rows."""

    def run(self, prompt_text, response_schema, item_ids, platform_map, progress_cb=None):
        return [{"item_id": str(i), "model": "stub", "parsed": None, "response": "",
                 "finish_reason": "DNF - stub", "usage": {}, "inference_duration": 0.0,
                 "error": "stub"} for i in item_ids]






def _cleanup_run(run_id: str):
    ab_eval.delete_run(run_id)






def test_manifest_records_backend_and_overrides():
    live_text = ac._read_baked_text()
    run_id = ab_eval.new_run_id()
    try:
        ab_eval.execute_run(
            run_id=run_id,
            arms=[{"name": "a", "source": "live", "text": live_text},
                  {"name": "b", "source": "live", "text": live_text,
                   "model": "gemini-other", "temperature": 0.9}],
            item_ids=["1"],
            started_by="tester",
            runner=_StubRunner(),
        )
        manifest = data_io.load_json(storage_location=ab_eval.LOCATION,
                                     filename=ab_eval._run_file(run_id, "manifest.json"))
        by_name = {a["name"]: a for a in manifest["arms"]}
        assert by_name["a"]["backend"] == "gemini"
        assert by_name["a"]["gen_overrides"] == {}
        assert by_name["b"]["gen_overrides"] == {"model": "gemini-other", "temperature": 0.9}
    finally:
        _cleanup_run(run_id)






def test_unavailable_backend_fails_before_any_call(monkeypatch):
    # Force the availability check to fail regardless of what this host has
    # installed (on a fully-provisioned Mac qwen_local is genuinely available).
    from fyp.annotation.backends import BackendAvailability
    from fyp.annotation.backends.qwen_local import QwenLocalBackend

    monkeypatch.setattr(QwenLocalBackend, "availability",
                        lambda self, deep=False: BackendAvailability(
                            ok=False, reason="forced unavailable (test)"))
    live_text = ac._read_baked_text()
    with pytest.raises(ValueError, match="forced unavailable"):
        ab_eval.execute_run(
            run_id=ab_eval.new_run_id(),
            arms=[{"name": "q", "source": "live", "text": live_text,
                   "backend": "qwen_local"}],
            item_ids=["1"],
            started_by="tester",
            runner=_StubRunner(),
        )






def test_unknown_backend_id_fails_before_any_call():
    live_text = ac._read_baked_text()
    with pytest.raises(ValueError, match="Unknown annotation backend"):
        ab_eval.execute_run(
            run_id=ab_eval.new_run_id(),
            arms=[{"name": "q", "source": "live", "text": live_text,
                   "backend": "nonexistent"}],
            item_ids=["1"],
            started_by="tester",
            runner=_StubRunner(),
        )






def test_runner_factory_threads_overrides():
    arm = {"backend": "gemini", "gen_overrides": {"temperature": 0.4}}
    runner = ab_eval._runner_for_arm(arm)
    assert isinstance(runner, ab_eval.SyncThreadedRunner)
    assert runner.gen_overrides == {"temperature": 0.4}

    arm_plain = {"backend": "gemini", "gen_overrides": {}}
    runner = ab_eval._runner_for_arm(arm_plain)
    assert runner.gen_overrides is None






class _ScriptedModels:
    """Records generate_content calls; returns a canned structured response."""

    def __init__(self):
        self.calls = []

    def generate_content(self, model=None, config=None, contents=None):
        self.calls.append({"model": model, "config": config})

        class _Candidate:
            finish_reason = "STOP"

        class _Resp:
            candidates = [_Candidate()]
            usage_metadata = None
            text = json.dumps({"ok": True})

        return _Resp()






def test_annotate_one_applies_gen_overrides(monkeypatch):
    machine = get_config()["machine"]["gemini"]
    scripted = _ScriptedModels()

    class _Client:
        models = scripted

    monkeypatch.setitem(machine, "client", _Client())
    monkeypatch.setattr(ab_eval, "_build_contents", lambda item_id, platform: ["contents"])

    row = ab_eval.annotate_one("123", None, "PROMPT", {"type": "object"},
                               gen_overrides={"model": "override-model", "temperature": 1.3})
    assert row["error"] == ""
    assert row["model"] == "override-model"
    call = scripted.calls[0]
    assert call["model"] == "override-model"
    assert call["config"].temperature == 1.3
    # Non-overridden params come from the production config.
    assert call["config"].max_output_tokens == machine["max_output_tokens"]






class _StubBackend:
    """Backend double returning production-shaped raw rows."""

    name = "stub"

    def __init__(self):
        self.calls = []

    def annotate_one(self, item_id, platform=None, gen_overrides=None,
                     prompt_text=None, response_schema=None):
        self.calls.append({"item_id": item_id, "prompt_text": prompt_text,
                           "response_schema": response_schema,
                           "gen_overrides": gen_overrides})
        return {"item_id": item_id, "source_platform": platform or "tiktok",
                "model": "stub-model", "structured": True,
                "response": json.dumps({"x": item_id}), "error": "",
                "finish_reason": "STOP", "usage": {"total_tokens": 5},
                "inference_duration": 0.01}






def test_backend_sequential_runner_row_shape_and_schema_preference():
    backend = _StubBackend()
    runner = ab_eval.BackendSequentialRunner(
        backend, gen_overrides={"temperature": 0.2}, schema_json={"type": "object"})
    rows = runner.run("PROMPT", "genai-schema-object", ["1", "2"], {"1": "tiktok"})
    assert [r["item_id"] for r in rows] == ["1", "2"]
    for row in rows:
        assert row["parsed"] is not None
        assert row["error"] == ""
        assert row["model"] == "stub-model"
    # The portable JSON schema wins over the genai-typed one run() received.
    assert backend.calls[0]["response_schema"] == {"type": "object"}
    assert backend.calls[0]["prompt_text"] == "PROMPT"
    assert backend.calls[0]["gen_overrides"] == {"temperature": 0.2}






def test_backend_sequential_runner_cancellation():
    backend = _StubBackend()
    runner = ab_eval.BackendSequentialRunner(backend, cancel_cb=lambda: True)
    with pytest.raises(ab_eval.RunCancelled):
        runner.run("PROMPT", None, ["1"], {})
    assert backend.calls == []  # cancelled before the first item






def test_annotate_one_defaults_unchanged_without_overrides(monkeypatch):
    machine = get_config()["machine"]["gemini"]
    scripted = _ScriptedModels()

    class _Client:
        models = scripted

    monkeypatch.setitem(machine, "client", _Client())
    monkeypatch.setattr(ab_eval, "_build_contents", lambda item_id, platform: ["contents"])

    row = ab_eval.annotate_one("123", None, "PROMPT", {"type": "object"})
    assert row["model"] == machine["model"]
    assert scripted.calls[0]["model"] == machine["model"]
    assert scripted.calls[0]["config"].temperature == machine["temperature"]





def test_arm_price_resolves_selection_and_model_override(monkeypatch):
    gemini_cf = get_config()["machine"]["gemini"]
    monkeypatch.setitem(gemini_cf, "pricing", {"input": 0.5, "output": 3.0})
    monkeypatch.setitem(gemini_cf, "variants",
                        {"gx": {"model": "gemini-x",
                                "pricing": {"input": 0.3, "output": 2.5}}})

    assert ab_eval._arm_price({"backend": "gemini"}) == {"input": 0.5, "output": 3.0}
    assert ab_eval._arm_price({"backend": "gx"}) == {"input": 0.3, "output": 2.5}
    # A same-model per-arm override keeps the price; a swap makes it unknown.
    assert ab_eval._arm_price({"backend": "gx",
                               "gen_overrides": {"model": "gemini-x"}}) is not None
    assert ab_eval._arm_price({"backend": "gx",
                               "gen_overrides": {"model": "something-else"}}) is None
    assert ab_eval._arm_price({"backend": "unknown_selection"}) is None






def test_arm_cost_computes_dollars_and_flags_unpriced():
    rows = [
        # 1M in + (0.1M out + 0.1M thinking) -> $1 + $2 = $3
        {"model": "modelA", "usage": {"prompt_tokens": 1_000_000,
                                      "candidates_tokens": 100_000,
                                      "thoughts_tokens": 100_000,
                                      "total_tokens": 1_200_000},
         "inference_duration": 1.0, "error": ""},
    ]
    cost = ab_eval._arm_cost(rows, price={"input": 1.0, "output": 10.0})
    assert cost["cost_usd"] == 3.0
    assert cost["unpriced_rows"] == 0
    assert cost["prompt_tokens"] == 1_000_000  # token totals unchanged

    # No price -> cost is None, not 0 (distinguish "free" from "unknown").
    unpriced = ab_eval._arm_cost(rows, price=None)
    assert unpriced["cost_usd"] is None
    assert unpriced["unpriced_rows"] == 1
