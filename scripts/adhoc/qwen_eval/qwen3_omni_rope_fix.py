"""Workaround for degenerate ("the the the...") output from Qwen3-Omni in mlx-vlm 0.6.5.

Root cause: mlx-vlm's generic generate loop calls the thinker's language model
directly during decode. The correct multimodal RoPE state (position_ids /
rope_deltas) is only computed at prefill; the language model never stores the
rope delta in its cached state (`_rope_deltas`), and the first decode step even
receives the stale full-prompt position_ids left over in the loop's kwargs.
Decode positions therefore come out as 0, then raw cache_offset without the
multimodal delta — the rotary embeddings are wrong from the first generated
token, and generation instantly degenerates into repetition.

Import this module BEFORE calling mlx_vlm.load / generate. It wraps
LanguageModel.__call__ for qwen3_omni_moe to:
  1. capture the rope delta whenever a multimodal prefill happens, and
  2. on decode steps (single-token input), drop any stale position_ids and
     inject the captured delta so positions resume at cache_offset + delta.

Second workaround (audio-path OOM): the processor's feature extractor returns
`attention_mask` over RAW AUDIO SAMPLES (e.g. 240,396 for a 15 s clip) while
`input_features` is the mel spectrogram (1,502 frames). Everything downstream
divides by the wrong unit: ~30k audio placeholder tokens get spliced into the
prompt (instead of ~190) and the multimodal position tensors blow up into a
~78 GB Metal allocation. `_patched_proc_call` below rescales the mask to
mel-frame length before the processor consumes it.
"""

import numpy as np

from mlx_vlm.models.qwen3_omni_moe import language as _language
from mlx_vlm.models.qwen3_omni_moe import (
    processing_qwen3_omni_moe as _processing,
)

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
            # Reset-proof stash: the original __call__ clears _rope_deltas when
            # pixel values are present, so keep our own copy.
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


_orig_proc_call = _processing.Qwen3OmniMoeProcessor.__call__


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
        return out
    ratio = max(1, round(mask.shape[-1] / n_frames))
    lengths = np.rint(mask.sum(-1) / ratio).astype(np.int32)
    fixed = np.zeros((mask.shape[0], n_frames), dtype=np.int32)
    for i, length in enumerate(lengths):
        fixed[i, : min(int(length), n_frames)] = 1
    out["attention_mask"] = fixed
    return out


def _patched_proc_call(self, text=None, images=None, videos=None, audio=None, **kwargs):
    if audio is not None:
        fe_cls = type(self.feature_extractor)
        if not getattr(fe_cls, "_omni_mask_fix", False):
            orig_fe_call = fe_cls.__call__

            def _fe_call(fe_self, *a, **k):
                return _rescale_sample_mask(orig_fe_call(fe_self, *a, **k))

            fe_cls.__call__ = _fe_call
            fe_cls._omni_mask_fix = True
    return _orig_proc_call(self, text=text, images=images, videos=videos, audio=audio, **kwargs)


_processing.Qwen3OmniMoeProcessor.__call__ = _patched_proc_call
