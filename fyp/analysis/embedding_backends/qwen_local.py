"""Local Qwen3-Embedding backend via sentence-transformers.

Runs the small Qwen3-Embedding text-embedding model (default: the 0.6B,
~1.2 GB) fully locally through sentence-transformers — MPS on Apple Silicon,
CUDA where present, plain CPU otherwise. Combined with the local Qwen
annotation backend this makes the embeddings + semantic map pipeline
cloud-free.

Vectors are Matryoshka-truncated to ``[embedding.qwen_local] dim`` and stored
raw (not normalised) — mean-centring/L2 happen downstream in
:mod:`fyp.analysis.video_map`, matching the Gemini backend's convention.
Documents are embedded without an instruction prefix (Qwen3-Embedding only
recommends instructions for retrieval *queries*; clustering documents are
embedded plain).
"""

import threading

import numpy as np

from fyp.analysis.embedding_backends.base import BackendAvailability, EmbeddingBackend
from fyp.fyp_config import get_config
from fyp.logging_setup import get_logger

try:
    from sentence_transformers import SentenceTransformer
except ImportError:  # installed only via the local_embeddings extra
    SentenceTransformer = None

logger = get_logger(__name__)






def _qwen_cf() -> dict:
    """The ``[embedding.qwen_local]`` config block with defaults."""
    stored = get_config().get("embedding", {}).get("qwen_local", {}) or {}
    defaults = {
        "model_id": "Qwen/Qwen3-Embedding-0.6B",
        "dim": 1024,
        "batch_size": 64,
    }
    return {**defaults, **stored}






def _pick_device() -> str:
    """The best available torch device: mps > cuda > cpu."""
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"






class QwenLocalEmbeddingBackend(EmbeddingBackend):
    """Qwen3-Embedding running locally via sentence-transformers."""

    name = "qwen_local"
    cloud_run_capable = False

    _model = None
    _load_lock = threading.Lock()


    def model_id(self) -> str:
        """The configured HF model id."""
        return _qwen_cf()["model_id"]


    def dim(self) -> int:
        """The configured (Matryoshka-truncated) output dimensionality."""
        return int(_qwen_cf()["dim"])


    def availability(self, deep: bool = False) -> BackendAvailability:
        """Dependency/model readiness (see ``qwen_support.check_all``).

        Args:
            deep: Accepted for interface parity; the shallow checks already
                cover everything except an actual encode.

        Returns:
            The availability result with per-check detail rows.
        """
        from fyp.analysis.embedding_backends import qwen_support

        return qwen_support.availability(self.model_id())


    def _ensure_model(self):
        """Load the model once per process (thread-safe); return it."""
        if self._model is not None:
            return self._model
        with QwenLocalEmbeddingBackend._load_lock:
            if QwenLocalEmbeddingBackend._model is None:
                if SentenceTransformer is None:
                    raise RuntimeError(
                        'sentence-transformers is not installed — pip install -e ".[local_embeddings]"')
                cf = _qwen_cf()
                device = _pick_device()
                logger.info(f"Loading {cf['model_id']} (dim={cf['dim']}) on {device}...")
                QwenLocalEmbeddingBackend._model = SentenceTransformer(
                    cf["model_id"], device=device, truncate_dim=int(cf["dim"]),
                )
        return QwenLocalEmbeddingBackend._model


    def embed_texts(self, texts: list[str], reporter=None) -> np.ndarray:
        """Embed a list of texts into an ``(n, dim)`` float32 matrix.

        Encodes sequentially in config-sized batches on the local device. A
        batch that raises yields zero-vectors for its rows (the caller drops
        all-zero rows before the shard write so those items retry next run).

        Args:
            texts: Documents to embed (empty strings are replaced with a space).
            reporter: Optional status reporter for progress logging.

        Returns:
            A float32 array of shape ``(len(texts), dim())``.
        """
        model = self._ensure_model()
        dim = self.dim()
        batch_size = int(_qwen_cf()["batch_size"])
        safe = [t if t else " " for t in texts]
        starts = list(range(0, len(safe), batch_size))
        parts: list[np.ndarray] = []

        for done, start in enumerate(starts, 1):
            chunk = safe[start:start + batch_size]
            try:
                vecs = model.encode(
                    chunk, batch_size=batch_size,
                    normalize_embeddings=False, show_progress_bar=False,
                )
                parts.append(np.asarray(vecs, dtype=np.float32))
            except Exception as e:
                logger.warning(f"Embedding batch at offset {start} failed: {e!r}")
                parts.append(np.zeros((len(chunk), dim), dtype=np.float32))
            if reporter is not None and done % 20 == 0:
                pct = int(done / len(starts) * 100)
                reporter.update_progress(pct, f"Embedded {done}/{len(starts)} batches")

        if not parts:
            return np.empty((0, dim), dtype=np.float32)
        return np.vstack(parts)
