"""Config-declared annotation-backend variants (model-version pinning).

A ``[machine.<backend>.variants.<name>]`` block in ``config/config.toml``
declares a named *selection*: the parent backend implementation plus config
overrides — typically a different model id, so two model generations of the
same backend (e.g. ``gemini`` on 3.0 and a ``gemini_35`` variant on 3.5) stay
selectable side by side::

    [machine.gemini.variants.gemini_35]
    label = "Gemini 3.5 Flash"            # optional display name
    model = "gemini-3.5-flash"            # override keys = the parent block's keys
    pricing = {input = 0.30, output = 2.50}   # optional, USD per 1M tokens

The admin setting ``annotation_backend`` may store either an implementation id
(the default variant — exactly the historical behavior) or a variant name.
A variant's effective model/params flow through the normal version-descriptor
mechanics, so each variant forks its own ``av_`` annotation version; the plain
``gemini`` selection keeps its byte-identical legacy hash path. ``label`` /
``pricing`` (and a redundant ``backend`` key) are metadata, never overrides —
they can never affect the hash.

Legacy flat ``[machine.variants.<name>]`` blocks (with a ``backend`` key) are
hoisted into the nested layout at config load by
``fyp_config._normalize_machine_config``, so this module only reads the
nested form. Validation is log-and-skip: a malformed variant block is ignored
with a warning, never allowed to break config load or backend selection.
"""

import re
from dataclasses import dataclass, field

from fyp.logging_setup import get_logger

logger = get_logger(__name__)

# Persisted in admin settings, run manifests and HTML option values.
_NAME_RE = re.compile(r"^[a-z0-9_]+$")

# Keys of a variant block that are metadata, not config overrides.
_META_KEYS = ("backend", "label", "pricing")




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
        pricing: Optional ``{input, output}`` USD-per-1M-token prices for
            cost display (variants inherit the backend block's ``pricing``
            when absent — resolved by the consumer, not here).
    """

    selection: str
    backend_id: str
    overrides: dict = field(default_factory=dict)
    label: str = ""
    pricing: dict | None = None






def declared_variants() -> dict:
    """The valid ``[machine.<backend>.variants]`` blocks as ``{name: VariantSpec}``.

    Invalid blocks (a name colliding with an implementation id or an earlier
    variant, characters outside ``[a-z0-9_]``, or a ``backend`` key that
    contradicts the parent block) are skipped with a logged warning. Override
    keys unknown to the implementation's config surface only warn — they may
    still be meaningful to a newer implementation, and a typo shows up in the
    log.

    Returns:
        Mapping of variant name to spec, in backend then declaration order.
    """
    from fyp.annotation.backends import BACKEND_IDS
    from fyp.fyp_config import get_config

    machine = get_config()["machine"]
    out: dict = {}
    for backend_id in BACKEND_IDS:
        blocks = (machine.get(backend_id, {}) or {}).get("variants", {}) or {}
        for name, block in blocks.items():
            if not isinstance(block, dict):
                logger.warning(f"[machine.{backend_id}.variants.{name}] is not a table — skipped")
                continue
            if not _NAME_RE.match(name):
                logger.warning(f"[machine.{backend_id}.variants.{name}]: name must "
                               f"match [a-z0-9_]+ — skipped")
                continue
            if name in BACKEND_IDS or name in out:
                logger.warning(f"[machine.{backend_id}.variants.{name}]: name collides "
                               f"with a backend id or another variant — skipped")
                continue
            declared_backend = block.get("backend")
            if declared_backend not in (None, backend_id):
                logger.warning(f"[machine.{backend_id}.variants.{name}]: backend "
                               f"{declared_backend!r} contradicts the parent block — skipped")
                continue
            overrides = {k: v for k, v in block.items() if k not in _META_KEYS}
            unknown = [k for k in overrides if k not in _known_override_keys(backend_id)]
            if unknown:
                logger.warning(f"[machine.{backend_id}.variants.{name}]: override keys "
                               f"{unknown} are not known {backend_id!r} config keys (typo?)")
            pricing = block.get("pricing")
            out[name] = VariantSpec(
                selection=name, backend_id=backend_id, overrides=overrides,
                label=str(block.get("label") or name),
                pricing=dict(pricing) if isinstance(pricing, dict) else None)
    return out






def _known_override_keys(backend_id: str) -> tuple:
    """The implementation's override-key surface, for the typo warning.

    Gemini's overridable keys are its model/generation params (connection
    keys like ``project`` are process-level, not per-variant); the other
    backends' full key set is the defaults dict in their ``_*_cf()`` helper.

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






def selection_pricing(selection: str) -> dict | None:
    """The ``{input, output}`` USD-per-1M-token prices for a selection.

    A variant's own ``pricing`` wins; otherwise the implementing backend
    block's ``pricing`` applies (for gemini that is ``[machine.gemini]``).
    Local backends typically declare none — they cost nothing per token.

    Args:
        selection: A selection id accepted by :func:`resolve`.

    Returns:
        The price entry, or ``None`` when nothing is declared.
    """
    from fyp.fyp_config import get_config

    spec = resolve(selection)
    if spec.pricing is not None:
        return spec.pricing
    block = get_config()["machine"].get(spec.backend_id, {}) or {}
    pricing = block.get("pricing")
    return dict(pricing) if isinstance(pricing, dict) else None






def selection_ids() -> tuple:
    """All valid selection ids: implementation ids then declared variants.

    Returns:
        The ordered id tuple (also the UI display order).
    """
    from fyp.annotation.backends import BACKEND_IDS

    return BACKEND_IDS + tuple(declared_variants())
