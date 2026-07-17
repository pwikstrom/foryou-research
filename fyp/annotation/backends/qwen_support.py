"""Hardware/dependency checks for the local Qwen annotation backend.

Import-safe everywhere: this module never imports mlx/mlx_vlm, so the checks
can run on any host (Cloud Run, Linux CI, Windows) and simply report that the
backend is unsupported there. Each check returns an actionable ``fix`` string
surfaced in the admin requirements panel and ``scripts/setup.py --check-only``.

v1 targets Apple Silicon + MLX only (the pilot-validated stack). Other
platforms would need a different runtime (Ollama / llama.cpp) — see
docs/installation.md#enabling-local-qwen-annotation.
"""

import importlib.util
import os
import platform
import shutil
import sys

from fyp.annotation.backends.base import BackendAvailability
from fyp.fyp_config import get_config

# Peak unified-memory observed in the 20-video pilot for the 4-bit 30B-A3B.
_OBSERVED_PEAK_GB = 23
_RAM_FAIL_GB = 32
_RAM_WARN_GB = 48
_MODEL_DISK_GB = 20






def default_model_id() -> str:
    """The configured local model id (``[machine.qwen_local] model_id``)."""
    qwen_cf = get_config()["machine"].get("qwen_local", {}) or {}
    return qwen_cf.get("model_id", "mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit")






def hf_cache_root() -> str:
    """The Hugging Face hub cache directory (honours HF_HUB_CACHE / HF_HOME)."""
    if os.environ.get("HF_HUB_CACHE"):
        return os.environ["HF_HUB_CACHE"]
    if os.environ.get("HF_HOME"):
        return os.path.join(os.environ["HF_HOME"], "hub")
    return os.path.expanduser("~/.cache/huggingface/hub")






def model_snapshot_present(model_id: str) -> bool:
    """Whether the model's weights are already in the local HF cache."""
    snap_dir = os.path.join(hf_cache_root(), f"models--{model_id.replace('/', '--')}", "snapshots")
    if not os.path.isdir(snap_dir):
        return False
    for entry in os.listdir(snap_dir):
        snapshot = os.path.join(snap_dir, entry)
        if os.path.isdir(snapshot) and any(
                fname.endswith(".safetensors") for fname in os.listdir(snapshot)):
            return True
    return False






def _total_ram_gb() -> float | None:
    """Total physical memory in GB, or None when unknowable."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        return None






def _free_disk_gb(path: str) -> float | None:
    """Free disk space at ``path`` (or its nearest existing parent) in GB."""
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free / 1e9
    except OSError:
        return None






def check_all(model_id: str | None = None) -> list[dict]:
    """Run every readiness check for the local Qwen backend.

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
               "Local Qwen annotation runs on Apple Silicon (MLX) only in this "
               "version — use the Gemini backend on this machine.",
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
                   f"for the 30B model ({_OBSERVED_PEAK_GB} GB peak observed).",
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
                   f"Free at least {_MODEL_DISK_GB} GB (the model download is ~18 GB).",
        })
        checks.append({
            "name": "Model downloaded",
            "ok": False,
            "detail": f"{model_id} not found in {cache_root}",
            "fix": f"hf download {model_id}   (~18 GB, one-time)",
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
        "fix": "" if mlx_ok else 'pip install -e ".[local_qwen]"',
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
            reason = f"Local Qwen backend unavailable — {check['name']}: {check['detail']}."
            if check["fix"]:
                reason += f" Fix: {check['fix']}"
            return BackendAvailability(ok=False, reason=reason, checks=checks)
    return BackendAvailability(ok=True, reason="", checks=checks)
