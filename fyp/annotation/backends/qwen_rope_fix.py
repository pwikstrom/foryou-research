"""Version-guarded mlx-vlm workarounds for Qwen3-Omni (mlx-vlm 0.6.x).

Two upstream bugs, both filed with reproductions and workarounds:

1. **Blaizzy/mlx-vlm#1619 — decode loses the multimodal RoPE delta.** The
   generic generate loop calls the thinker's language model directly during
   decode; the M-RoPE delta computed at prefill is never stored, and the first
   decode step receives stale full-prompt position_ids. Generation degenerates
   from the first token ("the the the…"). Patch: wrap
   ``LanguageModel.__call__`` to stash the delta at prefill (recomputing when
   needed), slice chunked-prefill position_ids to the chunk, and inject the
   delta on decode steps.

2. **Blaizzy/mlx-vlm#1620 — audio mask sized in raw samples.** The processor's
   feature extractor returns ``attention_mask`` over raw audio samples
   (240,396 for 15 s) while ``input_features`` is mel frames (1,502): ~30k
   audio placeholder tokens get spliced into the prompt and the position
   tensors OOM (~78 GB Metal alloc). Patch: rescale the mask to mel-frame
   length at the feature-extractor boundary.

``apply_patches()`` is idempotent, guarded to mlx-vlm 0.6.x, and probes for
the upstream fix before patching — on a fixed release it becomes a no-op and
logs that the patch was skipped. Call it BEFORE ``mlx_vlm.load``.
"""

import numpy as np

from fyp.logging_setup import get_logger

logger = get_logger(__name__)

_APPLIED = False
# mlx-vlm versions these workarounds are validated against. Anything newer
# is assumed to carry the upstream fixes (#1619 / #1620) unless probing says
# otherwise — re-validate on upgrade before extending this prefix list.
_PATCHED_VERSION_PREFIXES = ("0.6.",)






def _version_needs_patch() -> bool:
    """Whether the installed mlx-vlm is one this patch set is validated for."""
    try:
        import mlx_vlm

        version = getattr(mlx_vlm, "__version__", "")
    except ImportError:
        return False
    return any(version.startswith(prefix) for prefix in _PATCHED_VERSION_PREFIXES)






def apply_patches() -> bool:
    """Install both workarounds (idempotent, version-guarded).

    Returns:
        True when the patches are active (now or from an earlier call);
        False when skipped (mlx-vlm absent, or a version outside the
        validated range — assumed fixed upstream).
    """
    global _APPLIED
    if _APPLIED:
        return True
    if not _version_needs_patch():
        try:
            import mlx_vlm

            logger.info(f"qwen_rope_fix: skipping patches for mlx-vlm "
                        f"{getattr(mlx_vlm, '__version__', '?')} (outside validated 0.6.x; "
                        f"verify upstream #1619/#1620 are fixed)")
        except ImportError:
            pass
        return False

    _patch_language_model()
    _patch_audio_mask()
    _APPLIED = True
    logger.info("qwen_rope_fix: mlx-vlm 0.6.x Qwen3-Omni patches applied (#1619, #1620)")
    return True






def _patch_language_model() -> None:
    """Workaround #1619: keep multimodal RoPE positions correct during decode."""
    from mlx_vlm.models.qwen3_omni_moe import language as _language

    if getattr(_language.LanguageModel, "_fyp_rope_fix", False):
        return
    _orig_lm_call = _language.LanguageModel.__call__

    def _patched_lm_call(self, inputs, inputs_embeds=None, mask=None, cache=None, **kwargs):
        seq_len = inputs.shape[-1] if inputs is not None else inputs_embeds.shape[1]
        is_prefill = seq_len > 1

        if is_prefill:
            # Chunked prefill passes the FULL prompt position_ids with every
            # chunk; slice out this chunk's range using the KV cache offset.
            pos = kwargs.get("position_ids", None)
            if pos is not None and pos.shape[-1] != seq_len:
                offset = 0
                if cache and cache[0] is not None:
                    c0 = cache[0]
                    offset = c0._idx if hasattr(c0, "_idx") else c0.offset
                    offset = int(offset) if not isinstance(offset, int) else offset
                kwargs["position_ids"] = pos[..., offset : offset + seq_len]
            rope_deltas = kwargs.get("rope_deltas", None)
            if rope_deltas is None and kwargs.get("image_grid_thw") is None and kwargs.get("video_grid_thw") is None:
                pass  # text-only: default handling is fine
            else:
                if rope_deltas is None:
                    grid_kwargs = (kwargs.get("image_grid_thw"), kwargs.get("video_grid_thw"))
                    position_ids, rope_deltas = self.get_rope_index(
                        inputs, grid_kwargs[0], grid_kwargs[1], mask
                    )
                    kwargs.setdefault("position_ids", position_ids)
                    kwargs["rope_deltas"] = rope_deltas
                # Reset-proof stash: the original __call__ clears _rope_deltas
                # when pixel values are present, so keep our own copy.
                self._omni_fix_rope_deltas = rope_deltas
            out = _orig_lm_call(self, inputs, inputs_embeds=inputs_embeds, mask=mask, cache=cache, **kwargs)
            if getattr(self, "_omni_fix_rope_deltas", None) is not None:
                self._rope_deltas = self._omni_fix_rope_deltas
            return out

        # Decode step: kill stale full-prompt position ids leaked from prefill
        # kwargs, and restore the multimodal rope delta before position lookup.
        pos = kwargs.get("position_ids", None)
        if pos is not None and pos.shape[-1] != seq_len:
            kwargs.pop("position_ids")
        stashed = getattr(self, "_omni_fix_rope_deltas", None)
        if stashed is not None:
            kwargs.pop("position_ids", None)
            kwargs["rope_deltas"] = stashed
            self._rope_deltas = stashed
        return _orig_lm_call(self, inputs, inputs_embeds=inputs_embeds, mask=mask, cache=cache, **kwargs)

    _language.LanguageModel.__call__ = _patched_lm_call
    _language.LanguageModel._fyp_rope_fix = True






def _rescale_sample_mask(out):
    """Rescale a raw-sample attention_mask to mel-frame length in place."""
    feats = out.get("input_features", None)
    mask = out.get("attention_mask", None)
    if feats is None or mask is None:
        return out
    feats = np.asarray(feats)
    mask = np.asarray(mask)
    n_frames = feats.shape[-1]
    if mask.shape[-1] == n_frames:
        return out  # already frame-length: upstream fixed, nothing to do
    ratio = max(1, round(mask.shape[-1] / n_frames))
    lengths = np.rint(mask.sum(-1) / ratio).astype(np.int32)
    fixed = np.zeros((mask.shape[0], n_frames), dtype=np.int32)
    for i, length in enumerate(lengths):
        fixed[i, : min(int(length), n_frames)] = 1
    out["attention_mask"] = fixed
    return out






def _patch_audio_mask() -> None:
    """Workaround #1620: normalize the audio attention mask to mel frames."""
    from mlx_vlm.models.qwen3_omni_moe import (
        processing_qwen3_omni_moe as _processing,
    )

    if getattr(_processing.Qwen3OmniMoeProcessor, "_fyp_audio_mask_fix", False):
        return
    _orig_proc_call = _processing.Qwen3OmniMoeProcessor.__call__

    def _patched_proc_call(self, text=None, images=None, videos=None, audio=None, **kwargs):
        if audio is not None:
            fe_cls = type(self.feature_extractor)
            if not getattr(fe_cls, "_fyp_mask_fix", False):
                orig_fe_call = fe_cls.__call__

                def _fe_call(fe_self, *args, **fe_kwargs):
                    return _rescale_sample_mask(orig_fe_call(fe_self, *args, **fe_kwargs))

                fe_cls.__call__ = _fe_call
                fe_cls._fyp_mask_fix = True
        return _orig_proc_call(self, text=text, images=images, videos=videos, audio=audio, **kwargs)

    _processing.Qwen3OmniMoeProcessor.__call__ = _patched_proc_call
    _processing.Qwen3OmniMoeProcessor._fyp_audio_mask_fix = True
