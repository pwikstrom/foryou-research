"""Config-declared annotation-backend variants (model-version pinning).

A ``[machine.variants.<name>]`` block in ``config/config.toml`` declares a
named *selection*: an existing backend implementation plus config overrides —
typically a different model id, so two model generations of the same backend
(e.g. ``gemini`` on 3.0 and a ``gemini_35`` variant on 3.5) stay selectable
side by side::

    [machine.variants.gemini_35]
    backend = "gemini"           # implementation id from BACKEND_IDS
    label = "Gemini 3.5 Flash"   # optional display name
    model = "gemini-3.5-flash"   # override keys = the implementation's config keys

The admin setting ``annotation_backend`` may store either an implementation id
(the default variant — exactly the historical behavior) or a variant name.
A variant's effective model/params flow through the normal version-descriptor
mechanics, so each variant forks its own ``av_`` annotation version; the plain
``gemini`` selection keeps its byte-identical legacy hash path.

Validation is log-and-skip: a malformed variant block is ignored with a
warning, never allowed to break config load or backend selection.
"""

import re
from dataclasses import dataclass, field

from fyp.logging_setup import get_logger

logger = get_logger(__name__)

# Persisted in admin settings, run manifests and HTML option values.
_NAME_RE = re.compile(r"^[a-z0-9_]+$")

# Keys of a variant block that are not config overrides.
_META_KEYS = ("backend", "label")




@dataclass(frozen=True)
class VariantSpec:
    """One resolved backend selection.

    Attributes:
        selection: The settings-visible id (a variant name, or an
            implementation id for a default variant).
        backend_id: The implementing backend's id (in ``BACKEND_IDS``).
        overrides: Config overrides layered over the implementation's own
            config block (empty for default variants).
        label: Display name for UI dropdowns.
    """

    selection: str
    backend_id: str
    overrides: dict = field(default_factory=dict)
    label: str = ""






def declared_variants() -> dict:
    """The valid ``[machine.variants]`` blocks as ``{name: VariantSpec}``.

    Invalid blocks (unknown/missing ``backend``, a name colliding with an
    implementation id or containing characters outside ``[a-z0-9_]``) are
    skipped with a logged warning. Override keys unknown to the
    implementation's config surface only warn — they may still be meaningful
    to a newer implementation, and a typo shows up in the log.

    Returns:
        Mapping of variant name to spec, in config declaration order.
    """
    from fyp.annotation.backends import BACKEND_IDS
    from fyp.fyp_config import get_config

    blocks = get_config()["machine"].get("variants", {}) or {}
    out: dict = {}
    for name, block in blocks.items():
        if not isinstance(block, dict):
            logger.warning(f"[machine.variants.{name}] is not a table — skipped")
            continue
        if not _NAME_RE.match(name):
            logger.warning(f"[machine.variants.{name}]: name must match [a-z0-9_]+ — skipped")
            continue
        if name in BACKEND_IDS:
            logger.warning(f"[machine.variants.{name}]: name collides with a "
                           f"backend id — skipped")
            continue
        backend_id = block.get("backend")
        if backend_id not in BACKEND_IDS:
            logger.warning(f"[machine.variants.{name}]: backend must be one of "
                           f"{BACKEND_IDS} (got {backend_id!r}) — skipped")
            continue
        overrides = {k: v for k, v in block.items() if k not in _META_KEYS}
        unknown = [k for k in overrides if k not in _known_override_keys(backend_id)]
        if unknown:
            logger.warning(f"[machine.variants.{name}]: override keys {unknown} are "
                           f"not known {backend_id!r} config keys (typo?)")
        out[name] = VariantSpec(selection=name, backend_id=backend_id,
                                overrides=overrides,
                                label=str(block.get("label") or name))
    return out






def _known_override_keys(backend_id: str) -> tuple:
    """The implementation's config-key surface, for the typo warning.

    Gemini reads the flat ``[machine]`` keys; the other backends each read
    their own ``[machine.<backend_id>]`` block whose full key set is the
    defaults dict in their ``_*_cf()`` helper.

    Returns:
        The known override key names for ``backend_id``.
    """
    if backend_id == "gemini":
        return ("model", "temperature", "thinking_budget", "media_resolution",
                "max_output_tokens")
    if backend_id == "qwen_api":
        from fyp.annotation.backends.qwen_api import _api_cf

        return tuple(_api_cf())
    if backend_id == "qwen_local":
        from fyp.annotation.backends.qwen_local import _qwen_cf

        return tuple(_qwen_cf())
    if backend_id == "minicpm_local":
        from fyp.annotation.backends.minicpm_local import _minicpm_cf

        return tuple(_minicpm_cf())
    return ()






def resolve(selection: str) -> VariantSpec:
    """Resolve a selection id to its spec.

    Args:
        selection: An implementation id (default variant) or a declared
            variant name.

    Returns:
        The variant spec (implementation ids get empty overrides).

    Raises:
        ValueError: For an unknown selection.
    """
    from fyp.annotation.backends import BACKEND_IDS

    if selection in BACKEND_IDS:
        return VariantSpec(selection=selection, backend_id=selection, label=selection)
    variants = declared_variants()
    if selection in variants:
        return variants[selection]
    raise ValueError(f"Unknown annotation backend selection: {selection!r} "
                     f"(known: {selection_ids()})")






def selection_ids() -> tuple:
    """All valid selection ids: implementation ids then declared variants.

    Returns:
        The ordered id tuple (also the UI display order).
    """
    from fyp.annotation.backends import BACKEND_IDS

    return BACKEND_IDS + tuple(declared_variants())
