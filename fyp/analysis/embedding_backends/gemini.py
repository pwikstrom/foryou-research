"""Gemini embedding backend (Vertex AI or API-key mode).

Holds the Gemini embedding call path that historically lived in
:mod:`fyp.analysis.embeddings`: a process-wide client pinned to the embedding
endpoint location, concurrent batched ``embed_content`` calls with retry, and
the zero-vector convention for batches that fail all retries.

The API details live in the ``[embedding.gemini]`` config block (model_id /
dim / location / task_type, with the historical values as defaults) so the
model can be upgraded by config edit. The shard store is model-scoped, so a
model change simply re-embeds into new shards — old shards stay valid for the
old model id.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

import fyp.core.gemini_client as gemini_client
from fyp.analysis.embedding_backends.base import BackendAvailability, EmbeddingBackend
from fyp.fyp_config import get_config
from google import genai
from google.genai.types import EmbedContentConfig

# Concurrency / batching for the Vertex embedding calls.
_EMBED_BATCH = 20
_EMBED_WORKERS = 8
_EMBED_RETRIES = 4

_client: genai.Client | None = None






def _gemini_cf() -> dict:
    """The ``[embedding.gemini]`` config block with the historical defaults.

    gemini-embedding-001 supports Matryoshka truncation to 768 / 1536 / 3072
    dims; 1536 is the quality/size sweet spot. The location pins the Vertex
    embedding endpoint (the annotation client's ``global`` endpoint serves
    generation, not embeddings); API-key mode ignores it.
    """
    stored = get_config().get("embedding", {}).get("gemini", {}) or {}
    defaults = {
        "model_id": "gemini-embedding-001",
        "dim": 1536,
        "location": "us-central1",
        "task_type": "CLUSTERING",
    }
    return {**defaults, **stored}






def _get_client() -> genai.Client:
    """Return a process-wide GenAI client for embedding calls.

    Honours whichever Gemini mode is configured — Vertex AI or the plain Gemini
    API — via :func:`fyp.core.gemini_client.make_client`. In Vertex mode the
    location is pinned to ``[embedding.gemini].location``; in API-key mode the
    endpoint takes no region and the argument is ignored.

    Returns:
        A cached :class:`google.genai.Client`.

    Raises:
        GeminiNotConfiguredError: When no usable Gemini mode is configured.
    """
    global _client
    if _client is None:
        _client = gemini_client.make_client(location=_gemini_cf()["location"])
    return _client






def _embed_batch(client: genai.Client, chunk: list[str]) -> list[list[float]] | None:
    """Embed one batch of texts with retry, returning vectors or None on failure."""
    cf = _gemini_cf()
    config = EmbedContentConfig(task_type=cf["task_type"],
                                output_dimensionality=int(cf["dim"]))
    for attempt in range(_EMBED_RETRIES):
        try:
            resp = client.models.embed_content(model=cf["model_id"], contents=chunk,
                                               config=config)
            return [e.values for e in resp.embeddings]
        except Exception:
            if attempt == _EMBED_RETRIES - 1:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None






class GeminiEmbeddingBackend(EmbeddingBackend):
    """The production Gemini embedding backend."""

    name = "gemini"
    cloud_run_capable = True


    def model_id(self) -> str:
        """The configured Gemini embedding model id."""
        return _gemini_cf()["model_id"]


    def dim(self) -> int:
        """The configured (Matryoshka-truncated) output dimensionality."""
        return int(_gemini_cf()["dim"])


    def availability(self, deep: bool = False) -> BackendAvailability:
        """Config readiness of Gemini embedding (credentials resolve).

        Embedding is text-only, so unlike the annotation backend there is no
        media-access constraint — any resolvable Gemini mode works.

        Args:
            deep: Accepted for interface parity; no live probe is issued
                (an embedding ping would spend quota on every health poll).

        Returns:
            The availability result.
        """
        mode, reason = gemini_client.gemini_mode()
        checks = [{"name": "credentials", "ok": mode is not None,
                   "detail": reason if mode is None else f"mode: {mode}",
                   "fix": "" if mode is not None else
                   "See docs/installation.md#enabling-gemini-later."}]
        if mode is None:
            return BackendAvailability(ok=False, reason=reason, checks=checks)
        return BackendAvailability(ok=True, reason="", checks=checks)


    def embed_texts(self, texts: list[str], reporter=None) -> np.ndarray:
        """Embed a list of texts into an ``(n, dim)`` float32 matrix.

        Calls the Vertex embedding endpoint in concurrent batches. A batch that
        fails after all retries yields zero-vectors for its rows; the caller is
        expected to detect and skip those item_ids (they retry on the next run).

        Args:
            texts: Documents to embed (empty strings are replaced with a space).
            reporter: Optional status reporter for progress logging.

        Returns:
            A float32 array of shape ``(len(texts), dim())``.
        """
        client = _get_client()
        dim = self.dim()
        safe = [t if t else " " for t in texts]
        batches = [(i, safe[i:i + _EMBED_BATCH]) for i in range(0, len(safe), _EMBED_BATCH)]
        out: dict[int, list[list[float]]] = {}
        done = 0

        with ThreadPoolExecutor(max_workers=_EMBED_WORKERS) as ex:
            futures = {ex.submit(_embed_batch, client, chunk): i for i, chunk in batches}
            for fut in as_completed(futures):
                i = futures[fut]
                vecs = fut.result()
                if vecs is None:
                    vecs = [[0.0] * dim] * len(safe[i:i + _EMBED_BATCH])
                out[i] = vecs
                done += 1
                if reporter is not None and done % 50 == 0:
                    pct = int(done / len(batches) * 100)
                    reporter.update_progress(pct, f"Embedded {done}/{len(batches)} batches")

        matrix: list[list[float]] = []
        for i in range(0, len(safe), _EMBED_BATCH):
            matrix.extend(out[i])
        return np.asarray(matrix, dtype=np.float32)
