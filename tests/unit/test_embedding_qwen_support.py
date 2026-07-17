"""Local-embedding dependency checks stay import-safe and actionable."""

from fyp.analysis.embedding_backends import qwen_support






def test_checks_run_without_optional_deps():
    """check_all must work on any host — it never imports torch/ST."""
    checks = qwen_support.check_all("Qwen/Qwen3-Embedding-0.6B")
    assert checks, "expected at least one check row"
    assert all({"name", "ok", "detail", "fix"} <= set(c) for c in checks)






def test_failing_checks_carry_fix_strings():
    checks = qwen_support.check_all("definitely/not-a-downloaded-model")
    by_name = {c["name"]: c for c in checks}
    model_check = by_name["Model downloaded"]
    assert model_check["ok"] is False
    assert "hf download" in model_check["fix"]






def test_cloud_run_forces_unavailable(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "fyp-task-runner")
    result = qwen_support.availability("Qwen/Qwen3-Embedding-0.6B")
    assert result.ok is False
    assert "Cloud Run" in result.reason






def test_install_fix_mentions_local_embeddings_extra(monkeypatch):
    """The actionable fix must point at the right pyproject extra."""
    import importlib.util as ilu

    real_find_spec = ilu.find_spec
    monkeypatch.setattr(
        qwen_support.importlib.util, "find_spec",
        lambda name: None if name == "sentence_transformers" else real_find_spec(name))
    checks = qwen_support.check_all("Qwen/Qwen3-Embedding-0.6B")
    st_check = next(c for c in checks if c["name"] == "sentence-transformers installed")
    assert st_check["ok"] is False
    assert "local_embeddings" in st_check["fix"]






def test_default_model_id_reads_config():
    assert qwen_support.default_model_id() == "Qwen/Qwen3-Embedding-0.6B"
