"""qwen_support hardware/dependency checks: outcomes and actionable fixes."""

import os

import pytest

from fyp.annotation.backends import qwen_support

_MODEL = "mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit"






def _by_name(checks: list[dict]) -> dict:
    return {c["name"]: c for c in checks}






def test_check_rows_have_contract_shape():
    checks = qwen_support.check_all(_MODEL)
    assert checks, "expected at least one check"
    for check in checks:
        assert {"name", "ok", "detail", "fix"} <= set(check)
        if not check["ok"]:
            assert check["fix"], f"failing check '{check['name']}' must carry a fix"






def test_platform_check_fails_on_non_mac(monkeypatch):
    monkeypatch.setattr(qwen_support.sys, "platform", "linux")
    checks = _by_name(qwen_support.check_all(_MODEL))
    assert checks["Apple Silicon Mac"]["ok"] is False
    assert "Apple Silicon" in checks["Apple Silicon Mac"]["fix"]






def test_cloud_run_check(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "fyp-task-runner")
    checks = _by_name(qwen_support.check_all(_MODEL))
    assert checks["Local machine (not Cloud Run)"]["ok"] is False
    monkeypatch.delenv("K_SERVICE")
    checks = _by_name(qwen_support.check_all(_MODEL))
    assert checks["Local machine (not Cloud Run)"]["ok"] is True






def test_low_ram_fails(monkeypatch):
    monkeypatch.setattr(qwen_support, "_total_ram_gb", lambda: 16.0)
    checks = _by_name(qwen_support.check_all(_MODEL))
    assert checks["Memory"]["ok"] is False
    assert "32" in checks["Memory"]["fix"]






def test_missing_ffmpeg_has_brew_fix(monkeypatch):
    monkeypatch.setattr(qwen_support.shutil, "which", lambda name: None)
    checks = _by_name(qwen_support.check_all(_MODEL))
    assert checks["ffmpeg"]["ok"] is False
    assert "brew install ffmpeg" in checks["ffmpeg"]["fix"]






def test_model_snapshot_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    assert qwen_support.model_snapshot_present(_MODEL) is False

    snap = tmp_path / f"models--{_MODEL.replace('/', '--')}" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "model-00001.safetensors").write_bytes(b"x")
    assert qwen_support.model_snapshot_present(_MODEL) is True

    checks = _by_name(qwen_support.check_all(_MODEL))
    assert checks["Model downloaded"]["ok"] is True






def test_missing_model_has_download_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    checks = _by_name(qwen_support.check_all(_MODEL))
    assert checks["Model downloaded"]["ok"] is False
    assert _MODEL in checks["Model downloaded"]["fix"]






def test_availability_reports_first_failure(monkeypatch):
    monkeypatch.setattr(qwen_support.sys, "platform", "linux")
    result = qwen_support.availability(_MODEL)
    assert result.ok is False
    assert "Apple Silicon" in result.reason
    assert result.checks  # full check list still attached for the UI panel






@pytest.mark.skipif(os.environ.get("K_SERVICE") is not None, reason="not on Cloud Run")
def test_hf_cache_root_env_precedence(monkeypatch):
    monkeypatch.setenv("HF_HOME", "/tmp/hfhome")
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    assert qwen_support.hf_cache_root() == "/tmp/hfhome/hub"
    monkeypatch.setenv("HF_HUB_CACHE", "/tmp/hfcache")
    assert qwen_support.hf_cache_root() == "/tmp/hfcache"
