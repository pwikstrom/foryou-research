"""Embedding-backend registry and active-backend selection.

Backends subclass :class:`EmbeddingBackend` (see ``base.py``) and register at
class definition. ``get_backend`` imports backend modules lazily so importing
this package never pulls optional dependencies (the local Qwen backend needs
``sentence-transformers``/torch, installed only via the ``local_embeddings``
pyproject extra).

``BACKEND_IDS`` is the closed set of ids the settings layer accepts. The
embedding backend is selected independently of the annotation backend — both
set to a local backend means the embeddings + semantic map pipeline makes no
cloud calls at all.
"""

from fyp.analysis.embedding_backends.base import BackendAvailability, EmbeddingBackend

# Stable, settings-visible backend ids. Order = UI display order.
BACKEND_IDS = ("gemini", "qwen_api", "qwen_local")

# Backend id -> implementing module (imported lazily on first get_backend()).
_BACKEND_MODULES = {
    "gemini": "fyp.analysis.embedding_backends.gemini",
    "qwen_api": "fyp.analysis.embedding_backends.qwen_api",
    "qwen_local": "fyp.analysis.embedding_backends.qwen_local",
}

_instances: dict = {}

__all__ = [
    "EmbeddingBackend",
    "BackendAvailability",
    "BACKEND_IDS",
    "active_backend_name",
    "get_backend",
    "implemented_backend_ids",
]






def get_backend(name: str) -> EmbeddingBackend:
    """Return the (cached) backend instance for ``name``.

    Args:
        name: A backend id from :data:`BACKEND_IDS`.

    Returns:
        The backend instance.

    Raises:
        ValueError: For an unknown or not-yet-implemented backend id.
    """
    if name not in _BACKEND_MODULES:
        raise ValueError(f"Unknown embedding backend: {name!r} (known: {BACKEND_IDS})")
    if name not in _instances:
        import importlib

        try:
            importlib.import_module(_BACKEND_MODULES[name])
        except ImportError as exc:
            raise ValueError(f"Embedding backend {name!r} is not available: {exc}") from exc
        cls = EmbeddingBackend._registry.get(name)
        if cls is None:
            raise ValueError(f"Embedding backend {name!r} did not register")
        _instances[name] = cls()
    return _instances[name]






def implemented_backend_ids() -> tuple:
    """Backend ids whose modules import cleanly on this machine.

    Returns:
        The subset of :data:`BACKEND_IDS` that ``get_backend`` would accept.
    """
    out = []
    for name in BACKEND_IDS:
        try:
            get_backend(name)
            out.append(name)
        except ValueError:
            continue
    return tuple(out)






def active_backend_name() -> str:
    """The admin-selected embedding backend id (default ``"gemini"``).

    Reads the admin settings store lazily; any error (missing file, fresh
    install) falls back to Gemini so embedding never breaks on settings
    plumbing.

    Returns:
        A backend id from :data:`BACKEND_IDS`.
    """
    from fyp.analysis.embedding_backends.settings import get_embedding_backend

    name = get_embedding_backend()
    return name if name in BACKEND_IDS else "gemini"
