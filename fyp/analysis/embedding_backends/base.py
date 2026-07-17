"""Embedding-backend base class.

An embedding backend turns a list of text documents (built by
:func:`fyp.analysis.embeddings.build_document`) into an ``(n, dim)`` float32
matrix. Everything around that call — document building, the sharded parquet
store, incremental backlog computation, the video-map clustering — is
backend-agnostic, so a backend only has to get ``embed_texts`` right.

Subclasses auto-register via ``__init_subclass__``, exactly like
:class:`fyp.annotation.backends.base.AnnotationBackend` (whose
:class:`BackendAvailability` result type is reused here). Backend ids are
stable strings persisted in the admin settings store and stamped per-row into
the embedding shards' ``model`` column via :meth:`model_id`.
"""

from abc import ABC, abstractmethod

import numpy as np

from fyp.annotation.backends.base import BackendAvailability

__all__ = ["BackendAvailability", "EmbeddingBackend"]






class EmbeddingBackend(ABC):
    """One way of producing dense text embeddings (Gemini API, local Qwen, ...).

    Class attributes:
        name: Stable backend id (settings key).
        cloud_run_capable: Whether the backend can run on Cloud Run (a local
            model cannot — it needs the host machine).
    """

    name: str = ""
    cloud_run_capable: bool = True

    _registry: dict = {}


    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.name:
            EmbeddingBackend._registry[cls.name] = cls


    @abstractmethod
    def model_id(self) -> str:
        """The embedding model id this backend runs.

        Stamped per-row into the shard ``model`` column, so it scopes the
        store: :func:`fyp.analysis.embeddings.embedded_item_ids` and
        :func:`fyp.analysis.embeddings.load_embeddings` only see rows whose
        ``model`` matches the active backend's id.

        Returns:
            The model id string.
        """


    @abstractmethod
    def dim(self) -> int:
        """The output vector dimensionality (stamped per-row as ``dim``).

        Returns:
            The dimensionality.
        """


    @abstractmethod
    def availability(self, deep: bool = False) -> BackendAvailability:
        """Whether the backend can run, with actionable detail.

        Args:
            deep: When True, may perform a network/model probe; when False,
                must stay a cheap local config/dependency check safe to call
                on every page load.

        Returns:
            The availability result.
        """


    @abstractmethod
    def embed_texts(self, texts: list[str], reporter=None) -> np.ndarray:
        """Embed a list of texts into an ``(n, dim)`` float32 matrix.

        Rows whose embedding failed must come back as zero-vectors (never
        raise per-row) — the caller drops all-zero rows before writing the
        shard so those items retry on the next run.

        Args:
            texts: Documents to embed (may contain empty strings).
            reporter: Optional status reporter for progress logging.

        Returns:
            A float32 array of shape ``(len(texts), dim())``.
        """
