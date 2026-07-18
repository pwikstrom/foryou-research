"""Hardware/dependency checks for the local MiniCPM annotation backend.

Import-safe everywhere: this module never imports mlx/mlx_vlm, so the checks
can run on any host (Cloud Run, Linux CI, Windows) and simply report that the
backend is unsupported there. Each check returns an actionable ``fix`` string
surfaced in the admin requirements panel and ``scripts/setup.py --check-only``.

v1 targets Apple Silicon + MLX only, like ``qwen_support`` (whose generic
cache/RAM/disk helpers this module reuses). At 9B the MiniCPM model runs on
much smaller Macs than the 30B Qwen model — see the thresholds below.
"""

import importlib.util
import os
import platform
import shutil
import sys

from fyp.annotation.backends.base import BackendAvailability
from fyp.annotation.backends.qwen_support import (
    _free_disk_gb,
    _total_ram_gb,
    hf_cache_root,
    model_snapshot_present,
)
from fyp.fyp_config import get_config

# Peak unified-memory observed in the 20-video pilot for the 4-bit 9B model.
_OBSERVED_PEAK_GB = 8
_RAM_FAIL_GB = 16
_RAM_WARN_GB = 24
_MODEL_DISK_GB = 8






def default_model_id() -> str:
    """The configured local model id (``[machine.minicpm_local] model_id``)."""
    minicpm_cf = get_config()["machine"].get("minicpm_local", {}) or {}
    return minicpm_cf.get("model_id", "mlx-community/MiniCPM-o-4_5-4bit")






def check_all(model_id: str | None = None) -> list[dict]:
    """Run every readiness check for the local MiniCPM backend.

    Args:
        model_id: HF model id to probe for; defaults to the configured one.

    Returns:
        Check rows ``{name, ok, detail, fix}`` in display order. ``fix`` is an
        actionable command/instruction, empty when the check passes.
    """
    model_id = model_id or default_model_id()
    checks: list[dict] = []

    apple_silicon = sys.platform == "darwin" and platform.machine() == "arm64"
    checks.append({
        "name": "Apple Silicon Mac",
        "ok": apple_silicon,
        "detail": f"{sys.platform}/{platform.machine()}",
        "fix": "" if apple_silicon else
               "Local MiniCPM annotation runs on Apple Silicon (MLX) only in "
               "this version — use the Gemini backend on this machine.",
    })

    # Same signal as web_interface.task_status.is_cloud_run (inlined — the fyp
    # core must not import the web layer).
    on_cloud_run = bool(os.environ.get("K_SERVICE"))
    checks.append({
        "name": "Local machine (not Cloud Run)",
        "ok": not on_cloud_run,
        "detail": "running on Cloud Run" if on_cloud_run else "local host",
        "fix": "" if not on_cloud_run else
               "A local model cannot run on Cloud Run — run the annotator on "
               "the host machine, or switch the backend to Gemini.",
    })

    ram_gb = _total_ram_gb()
    if ram_gb is None:
        checks.append({"name": "Memory", "ok": True, "detail": "unknown (could not probe)", "fix": ""})
    else:
        ram_ok = ram_gb >= _RAM_FAIL_GB
        detail = (f"{ram_gb:.0f} GB total (observed peak {_OBSERVED_PEAK_GB} GB; "
                  f"{_RAM_WARN_GB}+ GB recommended)")
        checks.append({
            "name": "Memory",
            "ok": ram_ok,
            "detail": detail + ("" if ram_gb >= _RAM_WARN_GB or not ram_ok else " — tight, close other apps"),
            "fix": "" if ram_ok else
                   f"At least {_RAM_FAIL_GB} GB of unified memory is required "
                   f"for the 9B model ({_OBSERVED_PEAK_GB} GB peak observed).",
        })

    cache_root = hf_cache_root()
    have_model = model_snapshot_present(model_id)
    if have_model:
        checks.append({"name": "Model downloaded", "ok": True,
                       "detail": f"{model_id} in {cache_root}", "fix": ""})
    else:
        free_gb = _free_disk_gb(cache_root)
        disk_ok = free_gb is None or free_gb >= _MODEL_DISK_GB
        checks.append({
            "name": "Disk space for model",
            "ok": disk_ok,
            "detail": f"{free_gb:.0f} GB free at {cache_root}" if free_gb is not None else "unknown",
            "fix": "" if disk_ok else
                   f"Free at least {_MODEL_DISK_GB} GB (the model download is ~6 GB).",
        })
        checks.append({
            "name": "Model downloaded",
            "ok": False,
            "detail": f"{model_id} not found in {cache_root}",
            "fix": f"hf download {model_id}   (~6 GB, one-time)",
        })

    ffmpeg_ok = shutil.which("ffmpeg") is not None
    checks.append({
        "name": "ffmpeg",
        "ok": ffmpeg_ok,
        "detail": shutil.which("ffmpeg") or "not on PATH",
        "fix": "" if ffmpeg_ok else "brew install ffmpeg",
    })

    mlx_ok = importlib.util.find_spec("mlx_vlm") is not None
    checks.append({
        "name": "mlx-vlm installed",
        "ok": mlx_ok,
        "detail": "importable" if mlx_ok else "not installed in this environment",
        "fix": "" if mlx_ok else 'pip install -e ".[local_minicpm]"',
    })

    return checks






def availability(model_id: str | None = None) -> BackendAvailability:
    """Aggregate the checks into a single availability result.

    Args:
        model_id: HF model id to probe for; defaults to the configured one.

    Returns:
        ``ok`` iff every check passes; ``reason`` is the first failing check's
        detail + fix.
    """
    checks = check_all(model_id)
    for check in checks:
        if not check["ok"]:
            reason = f"Local MiniCPM backend unavailable — {check['name']}: {check['detail']}."
            if check["fix"]:
                reason += f" Fix: {check['fix']}"
            return BackendAvailability(ok=False, reason=reason, checks=checks)
    return BackendAvailability(ok=True, reason="", checks=checks)
