"""Dependency checks for the local Qwen embedding backend.

Import-safe everywhere: this module never imports torch/sentence-transformers,
so the checks can run on any host (Cloud Run, Linux CI, Windows) and simply
report what is missing. Each check returns an actionable ``fix`` string
surfaced in the admin requirements panel.

Unlike the 30B annotation model, the embedding model (Qwen3-Embedding-0.6B by
default) is small (~1.2 GB) and sentence-transformers runs on MPS, CUDA or
plain CPU — so there is no Apple-Silicon or memory hard gate, only a
"slow on CPU" advisory.
"""

import importlib.util
import os

from fyp.annotation.backends.base import BackendAvailability
from fyp.annotation.backends.qwen_support import hf_cache_root, model_snapshot_present
from fyp.fyp_config import get_config

_MODEL_DISK_GB = 3






def default_model_id() -> str:
    """The configured local embedding model id (``[embedding.qwen_local] model_id``)."""
    embed_cf = get_config().get("embedding", {}).get("qwen_local", {}) or {}
    return embed_cf.get("model_id", "Qwen/Qwen3-Embedding-0.6B")






def _free_disk_gb(path: str) -> float | None:
    """Free disk space at ``path`` (or its nearest existing parent) in GB."""
    import shutil

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






def _device_detail() -> str:
    """Best-effort accelerator description for the checks panel.

    Only probes torch when it is already importable (never triggers an
    install-side import error on hosts without the extra).
    """
    if importlib.util.find_spec("torch") is None:
        return "unknown (torch not installed)"
    try:
        import torch

        if torch.backends.mps.is_available():
            return "MPS (Apple GPU)"
        if torch.cuda.is_available():
            return "CUDA"
        return "CPU (works, but slow for large backlogs)"
    except Exception:
        return "unknown"






def check_all(model_id: str | None = None) -> list[dict]:
    """Run every readiness check for the local Qwen embedding backend.

    Args:
        model_id: HF model id to probe for; defaults to the configured one.

    Returns:
        Check rows ``{name, ok, detail, fix}`` in display order. ``fix`` is an
        actionable command/instruction, empty when the check passes.
    """
    model_id = model_id or default_model_id()
    checks: list[dict] = []

    # Same signal as web_interface.task_status.is_cloud_run (inlined — the fyp
    # core must not import the web layer).
    on_cloud_run = bool(os.environ.get("K_SERVICE"))
    checks.append({
        "name": "Local machine (not Cloud Run)",
        "ok": not on_cloud_run,
        "detail": "running on Cloud Run" if on_cloud_run else "local host",
        "fix": "" if not on_cloud_run else
               "A local model cannot run on Cloud Run — run the embeddings "
               "refresh on the host machine, or switch the backend to Gemini.",
    })

    st_ok = importlib.util.find_spec("sentence_transformers") is not None
    checks.append({
        "name": "sentence-transformers installed",
        "ok": st_ok,
        "detail": "importable" if st_ok else "not installed in this environment",
        "fix": "" if st_ok else 'pip install -e ".[local_embeddings]"',
    })

    checks.append({
        "name": "Compute device",
        "ok": True,
        "detail": _device_detail(),
        "fix": "",
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
                   f"Free at least {_MODEL_DISK_GB} GB (the model download is ~1.2 GB).",
        })
        checks.append({
            "name": "Model downloaded",
            "ok": False,
            "detail": f"{model_id} not found in {cache_root}",
            "fix": f"hf download {model_id}   (~1.2 GB, one-time)",
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
            reason = f"Local Qwen embedding backend unavailable — {check['name']}: {check['detail']}."
            if check["fix"]:
                reason += f" Fix: {check['fix']}"
            return BackendAvailability(ok=False, reason=reason, checks=checks)
    return BackendAvailability(ok=True, reason="", checks=checks)
