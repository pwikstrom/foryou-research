"""Annotation-backend registry and active-backend selection.

Backends subclass :class:`AnnotationBackend` (see ``base.py``) and register at
class definition. ``get_backend`` imports backend modules lazily so importing
this package never pulls optional dependencies (the local Qwen backend needs
``mlx_vlm``, which only exists on Apple Silicon dev machines).

``BACKEND_IDS`` is the closed set of ids the settings layer accepts.
"""

from fyp.annotation.backends.base import AnnotationBackend, BackendAvailability

# Stable, settings-visible backend ids. Order = UI display order.
BACKEND_IDS = ("gemini", "qwen_api", "qwen_local", "minicpm_local")

# Backend id -> implementing module (imported lazily on first get_backend()).
_BACKEND_MODULES = {
    "gemini": "fyp.annotation.backends.gemini",
    "qwen_api": "fyp.annotation.backends.qwen_api",
    "qwen_local": "fyp.annotation.backends.qwen_local",
    "minicpm_local": "fyp.annotation.backends.minicpm_local",
}

_instances: dict = {}

__all__ = [
    "AnnotationBackend",
    "BackendAvailability",
    "BACKEND_IDS",
    "active_backend_name",
    "get_backend",
    "implemented_backend_ids",
]






def get_backend(name: str) -> AnnotationBackend:
    """Return the (cached) backend instance for ``name``.

    Args:
        name: A backend id from :data:`BACKEND_IDS`.

    Returns:
        The backend instance.

    Raises:
        ValueError: For an unknown or not-yet-implemented backend id.
    """
    if name not in _BACKEND_MODULES:
        raise ValueError(f"Unknown annotation backend: {name!r} (known: {BACKEND_IDS})")
    if name not in _instances:
        import importlib

        try:
            importlib.import_module(_BACKEND_MODULES[name])
        except ImportError as exc:
            raise ValueError(f"Annotation backend {name!r} is not available: {exc}") from exc
        cls = AnnotationBackend._registry.get(name)
        if cls is None:
            raise ValueError(f"Annotation backend {name!r} did not register")
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
    """The admin-selected backend id (default ``"gemini"``).

    Reads the admin settings store lazily; any error (missing file, fresh
    install) falls back to Gemini so annotation never breaks on settings
    plumbing.

    Returns:
        A backend id from :data:`BACKEND_IDS`.
    """
    from fyp.annotation.backends.settings import get_annotation_backend

    name = get_annotation_backend()
    return name if name in BACKEND_IDS else "gemini"
