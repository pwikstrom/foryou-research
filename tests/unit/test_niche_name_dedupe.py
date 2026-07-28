"""Test _dedupe_niche_names in fyp/video_map.py with a stubbed naming model.

Covers: case-insensitive collision detection, largest-niche-keeps-name,
retry on a still-colliding Gemini reply, and the deterministic term-suffix
fallback when the model errors.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fyp.video_map import _dedupe_niche_names






def test_collisions_renamed_largest_kept() -> None:
    """Duplicate labels are renamed; the largest niche in each group keeps its name."""
    meta = {
        0: {"name": "Pet Antics", "size": 500, "terms": ["dog", "puppy"]},
        1: {"name": "pet antics", "size": 300, "terms": ["cat", "kitten"]},
        2: {"name": "Pet Antics", "size": 800, "terms": ["funny", "pets"]},
        3: {"name": "Comedy Skits", "size": 400, "terms": ["skit"]},
        4: {"name": "Comedy Skits", "size": 200, "terms": ["sketch"]},
    }

    calls = []

    def ask_stub(prompt: str) -> str:
        calls.append(prompt)
        # First re-prompt returns yet another collision, forcing one retry.
        if len(calls) == 1:
            return "Comedy Skits"
        return f"Unique Label {len(calls)}"

    renamed = _dedupe_niche_names(meta, lambda n: f"- exemplar for {n}", ask_stub)

    assert renamed == 3
    assert meta[2]["name"] == "Pet Antics"
    assert meta[3]["name"] == "Comedy Skits"
    keys = {" ".join(m["name"].lower().split()) for m in meta.values()}
    assert len(keys) == 5, "all niche names must be unique"
    print("test_collisions_renamed_largest_kept OK")






def test_fallback_suffix_on_model_failure() -> None:
    """When the model errors, the duplicate gets a top-term suffix."""
    meta = {
        0: {"name": "Pet Antics", "size": 500, "terms": ["dog"]},
        1: {"name": "Pet Antics", "size": 300, "terms": ["cat"]},
    }

    renamed = _dedupe_niche_names(meta, lambda n: "", lambda p: None)

    assert renamed == 1
    assert meta[0]["name"] == "Pet Antics"
    assert meta[1]["name"] == "Pet Antics (cat)"
    print("test_fallback_suffix_on_model_failure OK")






if __name__ == "__main__":
    test_collisions_renamed_largest_kept()
    test_fallback_suffix_on_model_failure()
    print("All tests passed.")
