"""Patch mlx-vlm's MiniCPM-o weight sanitizer for pre-sanitized checkpoints.

mlx-vlm 0.6.5's ``minicpmo.Model.sanitize`` only recognizes the original
OpenBMB checkpoint naming (``llm.`` / ``vpm.`` / ``apm.`` / ``resampler.`` /
``audio_projection_layer.``) and silently DROPS every other key. The
mlx-community MLX quants (e.g. ``mlx-community/MiniCPM-o-4_5-5bit``) ship with
the post-sanitize canonical names (``language_model.`` / ``vision_tower.`` /
``audio_tower.``), so sanitize discards nearly the whole checkpoint and
``load_weights`` fails with ~1200 missing parameters.

Fix: rename canonical prefixes back to the raw ones before delegating to the
original sanitize, which then renames them forward again (and still applies
its conv-weight transposes, guarded by shape checks, so this is idempotent).

Import this module for its side effect before ``mlx_vlm.load()`` on a
MiniCPM-o model.
"""

from mlx_vlm.models.minicpmo import minicpmo as _minicpmo

_CANONICAL_TO_RAW = (
    ("language_model.", "llm."),
    ("vision_tower.", "vpm."),
    ("audio_tower.", "apm."),
)






def _patched_sanitize(self, weights):
    remapped = {}
    for key, value in weights.items():
        for canonical, raw in _CANONICAL_TO_RAW:
            if key.startswith(canonical):
                key = raw + key[len(canonical):]
                break
        remapped[key] = value
    return _orig_sanitize(self, remapped)






if not getattr(_minicpmo.Model.sanitize, "_prefix_fix", False):
    _orig_sanitize = _minicpmo.Model.sanitize
    _patched_sanitize._prefix_fix = True
    _minicpmo.Model.sanitize = _patched_sanitize
