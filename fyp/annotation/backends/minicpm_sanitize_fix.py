"""Version-guarded mlx-vlm workaround for MiniCPM-o checkpoint naming.

mlx-vlm 0.6.x's ``minicpmo.Model.sanitize`` only recognizes the original
OpenBMB checkpoint tensor naming (``llm.`` / ``vpm.`` / ``apm.`` /
``resampler.`` / ``audio_projection_layer.``) and silently DROPS every other
key. The published MLX quants (e.g. ``mlx-community/MiniCPM-o-4_5-4bit``)
ship with the post-sanitize canonical names (``language_model.`` /
``vision_tower.`` / ``audio_tower.``), so sanitize discards nearly the whole
checkpoint and ``load_weights`` fails with ~1200 "missing" parameters even
though every tensor is present on disk.

Fix: rename canonical prefixes back to the raw ones before delegating to the
original sanitize, which renames them forward again (its conv-weight
transposes are shape-guarded, so the round-trip is idempotent).

``apply_patches()`` is idempotent and guarded to mlx-vlm 0.6.x — on a newer
release it becomes a no-op (assumed fixed upstream; re-validate on upgrade).
Call it BEFORE ``mlx_vlm.load``.
"""

from fyp.logging_setup import get_logger

logger = get_logger(__name__)

_APPLIED = False
# mlx-vlm versions this workaround is validated against (same policy as
# qwen_rope_fix): anything newer is assumed fixed upstream.
_PATCHED_VERSION_PREFIXES = ("0.6.",)

_CANONICAL_TO_RAW = (
    ("language_model.", "llm."),
    ("vision_tower.", "vpm."),
    ("audio_tower.", "apm."),
)






def _version_needs_patch() -> bool:
    """Whether the installed mlx-vlm is one this patch is validated for."""
    try:
        import mlx_vlm

        version = getattr(mlx_vlm, "__version__", "")
    except ImportError:
        return False
    return any(version.startswith(prefix) for prefix in _PATCHED_VERSION_PREFIXES)






def apply_patches() -> bool:
    """Install the sanitize workaround (idempotent, version-guarded).

    Returns:
        True when the patch is active (now or from an earlier call);
        False when skipped (mlx-vlm absent, or a version outside the
        validated range — assumed fixed upstream).
    """
    global _APPLIED
    if _APPLIED:
        return True
    if not _version_needs_patch():
        try:
            import mlx_vlm

            logger.info(f"minicpm_sanitize_fix: skipping patch for mlx-vlm "
                        f"{getattr(mlx_vlm, '__version__', '?')} (outside validated 0.6.x; "
                        f"verify canonical-named checkpoints load upstream)")
        except ImportError:
            pass
        return False

    from mlx_vlm.models.minicpmo import minicpmo as _minicpmo

    orig_sanitize = _minicpmo.Model.sanitize

    def _patched_sanitize(self, weights):
        remapped = {}
        for key, value in weights.items():
            for canonical, raw in _CANONICAL_TO_RAW:
                if key.startswith(canonical):
                    key = raw + key[len(canonical):]
                    break
            remapped[key] = value
        return orig_sanitize(self, remapped)

    _minicpmo.Model.sanitize = _patched_sanitize
    _APPLIED = True
    logger.info("minicpm_sanitize_fix: mlx-vlm 0.6.x MiniCPM-o sanitize patch applied")
    return True
