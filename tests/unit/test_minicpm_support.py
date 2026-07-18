"""minicpm_support hardware/dependency checks: outcomes and actionable fixes."""

from fyp.annotation.backends import minicpm_support

_MODEL = "mlx-community/MiniCPM-o-4_5-4bit"






def _by_name(checks: list[dict]) -> dict:
    return {c["name"]: c for c in checks}






def test_check_rows_have_contract_shape():
    checks = minicpm_support.check_all(_MODEL)
    assert checks, "expected at least one check"
    for check in checks:
        assert {"name", "ok", "detail", "fix"} <= set(check)
        if not check["ok"]:
            assert check["fix"], f"failing check '{check['name']}' must carry a fix"






def test_platform_check_fails_on_non_mac(monkeypatch):
    monkeypatch.setattr(minicpm_support.sys, "platform", "linux")
    checks = _by_name(minicpm_support.check_all(_MODEL))
    assert checks["Apple Silicon Mac"]["ok"] is False
    assert "Apple Silicon" in checks["Apple Silicon Mac"]["fix"]






def test_low_ram_threshold_is_minicpm_sized(monkeypatch):
    # 16 GB passes for the 9B model (would fail the 30B Qwen thresholds) ...
    monkeypatch.setattr(minicpm_support, "_total_ram_gb", lambda: 16.0)
    checks = _by_name(minicpm_support.check_all(_MODEL))
    assert checks["Memory"]["ok"] is True
    # ... while 8 GB fails with the MiniCPM figure in the fix.
    monkeypatch.setattr(minicpm_support, "_total_ram_gb", lambda: 8.0)
    checks = _by_name(minicpm_support.check_all(_MODEL))
    assert checks["Memory"]["ok"] is False
    assert "16" in checks["Memory"]["fix"]






def test_missing_model_has_download_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    checks = _by_name(minicpm_support.check_all(_MODEL))
    assert checks["Model downloaded"]["ok"] is False
    assert _MODEL in checks["Model downloaded"]["fix"]






def test_mlx_fix_names_minicpm_extra(monkeypatch):
    monkeypatch.setattr(minicpm_support.importlib.util, "find_spec", lambda name: None)
    checks = _by_name(minicpm_support.check_all(_MODEL))
    assert checks["mlx-vlm installed"]["ok"] is False
    assert "local_minicpm" in checks["mlx-vlm installed"]["fix"]






def test_availability_reports_first_failure(monkeypatch):
    monkeypatch.setattr(minicpm_support.sys, "platform", "linux")
    result = minicpm_support.availability(_MODEL)
    assert result.ok is False
    assert "MiniCPM" in result.reason and "Apple Silicon" in result.reason
    assert result.checks  # full check list still attached for the UI panel
