"""Hosted Qwen text-embedding backend (Alibaba Model Studio / DashScope).

Calls the OpenAI-compatible ``/embeddings`` endpoint on DashScope's
international region with the Qwen3-Embedding-based ``text-embedding-v4``
model (Matryoshka ``dimensions`` supported). Same credential pattern as the
``qwen_api`` annotation backend: the API key comes from the
``DASHSCOPE_API_KEY`` environment variable; config lives in
``[embedding.qwen_api]``.

DashScope caps ``text-embedding-v4`` at 10 input strings per request, so
batches are small; a modest thread pool keeps throughput reasonable. A batch
that fails all retries yields zero-vectors for its rows (the caller drops
all-zero rows before the shard write so those items retry next run).
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests

from fyp.analysis.embedding_backends.base import BackendAvailability, EmbeddingBackend
from fyp.annotation.backends.qwen_api import API_KEY_ENV
from fyp.fyp_config import get_config
from fyp.logging_setup import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS = (429, 500, 502, 503, 504)
_EMBED_RETRIES = 4
_EMBED_WORKERS = 4






def _api_cf() -> dict:
    """The ``[embedding.qwen_api]`` config block with defaults."""
    stored = get_config().get("embedding", {}).get("qwen_api", {}) or {}
    defaults = {
        "model_id": "text-embedding-v4",
        "dim": 1024,
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "batch_size": 10,
        "request_timeout": 60,
    }
    return {**defaults, **stored}






def _embed_batch(key: str, cf: dict, chunk: list[str]) -> list[list[float]] | None:
    """Embed one batch of texts with retry, returning vectors or None on failure."""
    payload = {
        "model": cf["model_id"],
        "input": chunk,
        "dimensions": int(cf["dim"]),
        "encoding_format": "float",
    }
    for attempt in range(_EMBED_RETRIES):
        if attempt:
            time.sleep(2.0 * attempt)
        try:
            r = requests.post(
                f"{cf['base_url']}/embeddings",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=cf["request_timeout"])
        except requests.RequestException as exc:
            logger.warning(f"qwen_api embedding request failed: {exc}")
            continue
        if r.status_code != 200:
            logger.warning(f"qwen_api embedding HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code in _RETRYABLE_STATUS:
                continue
            return None
        data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
        if len(data) == len(chunk):
            return [d["embedding"] for d in data]
        logger.warning(f"qwen_api embedding returned {len(data)} vectors for "
                       f"{len(chunk)} inputs")
    return None






class QwenApiEmbeddingBackend(EmbeddingBackend):
    """Qwen text embeddings via the DashScope OpenAI-compatible API."""

    name = "qwen_api"
    cloud_run_capable = True


    def model_id(self) -> str:
        """The configured hosted embedding model id."""
        return _api_cf()["model_id"]


    def dim(self) -> int:
        """The configured (Matryoshka-truncated) output dimensionality."""
        return int(_api_cf()["dim"])


    def availability(self, deep: bool = False) -> BackendAvailability:
        """API-key (and optionally live-endpoint) readiness.

        Args:
            deep: When True, additionally list models on the configured
                endpoint to prove the key/region/network.

        Returns:
            The availability result with per-check detail rows.
        """
        checks: list[dict] = []
        key = os.environ.get(API_KEY_ENV, "")
        checks.append({
            "name": "api key", "ok": bool(key),
            "detail": f"{API_KEY_ENV} is set" if key else f"{API_KEY_ENV} is not set",
            "fix": "" if key else (
                f"Create a Model Studio API key (international region) and set "
                f"the {API_KEY_ENV} environment variable. "
                "See docs/installation.md#enabling-hosted-qwen-annotation."),
        })
        if not key:
            return BackendAvailability(
                ok=False,
                reason=(f"Hosted Qwen embedding is not configured: the "
                        f"{API_KEY_ENV} environment variable is not set. "
                        "See docs/installation.md#enabling-hosted-qwen-annotation."),
                checks=checks)

        if deep:
            ping = self._ping(key)
            checks.append(ping)
            if not ping["ok"]:
                return BackendAvailability(ok=False, reason=ping["detail"], checks=checks)

        return BackendAvailability(ok=True, reason="", checks=checks)


    def _ping(self, key: str) -> dict:
        """List models on the configured endpoint; return a check row."""
        cf = _api_cf()
        try:
            r = requests.get(f"{cf['base_url']}/models",
                             headers={"Authorization": f"Bearer {key}"}, timeout=30)
        except requests.RequestException as exc:
            return {"name": "api ping", "ok": False,
                    "detail": f"endpoint unreachable: {exc}",
                    "fix": "Check network access to DashScope."}
        if r.status_code != 200:
            return {"name": "api ping", "ok": False,
                    "detail": f"models list failed: HTTP {r.status_code}: {r.text[:200]}",
                    "fix": "Check the API key and its region (international vs Beijing)."}
        ids = {m.get("id") for m in (r.json().get("data") or [])}
        if cf["model_id"] not in ids:
            return {"name": "api ping", "ok": False,
                    "detail": f"model {cf['model_id']!r} not offered on this endpoint",
                    "fix": "Check [embedding.qwen_api].model_id against the Model Studio catalog."}
        return {"name": "api ping", "ok": True, "detail": f"{cf['model_id']} available", "fix": ""}


    def embed_texts(self, texts: list[str], reporter=None) -> np.ndarray:
        """Embed a list of texts into an ``(n, dim)`` float32 matrix.

        Calls the hosted endpoint in concurrent small batches (the API caps
        inputs per request). A batch that fails after all retries yields
        zero-vectors for its rows.

        Args:
            texts: Documents to embed (empty strings are replaced with a space).
            reporter: Optional status reporter for progress logging.

        Returns:
            A float32 array of shape ``(len(texts), dim())``.
        """
        cf = _api_cf()
        key = os.environ.get(API_KEY_ENV, "")
        dim = int(cf["dim"])
        batch_size = int(cf["batch_size"])
        safe = [t if t else " " for t in texts]
        batches = [(i, safe[i:i + batch_size]) for i in range(0, len(safe), batch_size)]
        out: dict[int, list[list[float]]] = {}
        done = 0

        with ThreadPoolExecutor(max_workers=_EMBED_WORKERS) as ex:
            futures = {ex.submit(_embed_batch, key, cf, chunk): i for i, chunk in batches}
            for fut in as_completed(futures):
                i = futures[fut]
                vecs = fut.result()
                if vecs is None:
                    vecs = [[0.0] * dim] * len(safe[i:i + batch_size])
                out[i] = vecs
                done += 1
                if reporter is not None and done % 50 == 0:
                    pct = int(done / len(batches) * 100)
                    reporter.update_progress(pct, f"Embedded {done}/{len(batches)} batches")

        matrix: list[list[float]] = []
        for i in range(0, len(safe), batch_size):
            matrix.extend(out[i])
        return np.asarray(matrix, dtype=np.float32)
