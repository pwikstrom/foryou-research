"""Annotation-backend registry and active-backend selection.

Backends subclass :class:`AnnotationBackend` (see ``base.py``) and register at
class definition. ``get_backend`` imports backend modules lazily so importing
this package never pulls optional dependencies (the local Qwen backend needs
``mlx_vlm``, which only exists on Apple Silicon dev machines).

``BACKEND_IDS`` is the closed set of implementation ids; the settings layer
additionally accepts config-declared variant names (see ``variants.py``) —
named selections that bind an implementation to config overrides such as a
pinned model version.
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
        name: A backend id from :data:`BACKEND_IDS`, or a config-declared
            variant name (an implementation bound to config overrides).

    Returns:
        The backend instance (cached per selection).

    Raises:
        ValueError: For an unknown selection or a not-importable backend.
    """
    from fyp.annotation.backends import variants

    spec = variants.resolve(name)
    if name not in _instances:
        import importlib

        try:
            importlib.import_module(_BACKEND_MODULES[spec.backend_id])
        except ImportError as exc:
            raise ValueError(f"Annotation backend {name!r} is not available: {exc}") from exc
        cls = AnnotationBackend._registry.get(spec.backend_id)
        if cls is None:
            raise ValueError(f"Annotation backend {spec.backend_id!r} did not register")
        _instances[name] = cls(overrides=spec.overrides, selection=name)
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
    """The admin-selected backend selection id (default ``"gemini"``).

    Reads the admin settings store lazily; any error (missing file, fresh
    install) falls back to Gemini so annotation never breaks on settings
    plumbing. A stored selection that no longer resolves (e.g. a variant
    removed from config) also falls back, with a logged warning.

    Returns:
        A backend id from :data:`BACKEND_IDS` or a declared variant name.
    """
    from fyp.annotation.backends import variants
    from fyp.annotation.backends.settings import get_annotation_backend
    from fyp.logging_setup import get_logger

    name = get_annotation_backend()
    if name in BACKEND_IDS:
        return name
    try:
        if name in variants.declared_variants():
            return name
    except Exception:
        pass
    if name != "gemini":
        get_logger(__name__).warning(
            f"Selected annotation backend {name!r} is not a known backend or "
            f"declared variant — falling back to gemini")
    return "gemini"
