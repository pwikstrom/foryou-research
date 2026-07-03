#!/usr/bin/env python3
"""Offline tests for make_slideshow, including the optional audio mux.

Builds tiny PIL-generated JPEGs and a stdlib-``wave`` sine tone — no network,
no production storage. Verifies the four contract cases: silent slideshow,
short audio kept, long audio trimmed to the video length, and graceful
degradation to a silent slideshow when the audio file is unusable.

Usage:
    python tests/unit/test_slideshow_audio.py
    pytest tests/unit/test_slideshow_audio.py
"""

import math
import os
import struct
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PIL import Image

from fyp.scrape import make_slideshow

N_IMAGES = 3
SECONDS_PER_IMAGE = 2
EXPECTED_DURATION = N_IMAGES * SECONDS_PER_IMAGE




def _make_images(dirpath: str) -> list[str]:
    """Write three small solid-color JPEGs and return their paths."""
    paths = []
    for i, color in enumerate([(200, 40, 40), (40, 200, 40), (40, 40, 200)]):
        p = os.path.join(dirpath, f"img_{i + 1:02}.jpeg")
        Image.new("RGB", (320, 568), color).save(p, "JPEG")
        paths.append(p)
    return paths




def _make_tone(path: str, seconds: float, freq: float = 440.0, rate: int = 22050) -> str:
    """Write a mono 16-bit sine-tone WAV of the given length."""
    n_frames = int(seconds * rate)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for n in range(n_frames):
            val = int(20000 * math.sin(2 * math.pi * freq * n / rate))
            frames += struct.pack("<h", val)
        w.writeframes(bytes(frames))
    return path




def _probe(path: str) -> tuple[float, bool]:
    """Return (duration_seconds, has_audio) of a video file via moviepy."""
    from moviepy import VideoFileClip

    clip = VideoFileClip(path)
    try:
        return float(clip.duration), clip.audio is not None
    finally:
        clip.close()




def test_silent_slideshow():
    """No audio_path → mp4 with no audio stream, duration = images * 2s."""
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "out.mp4")
        make_slideshow(_make_images(td), output=out, duration=SECONDS_PER_IMAGE, swipe=False)
        assert os.path.getsize(out) > 0
        dur, has_audio = _probe(out)
        assert not has_audio, "silent slideshow must have no audio stream"
        assert abs(dur - EXPECTED_DURATION) < 0.5, f"duration {dur} != ~{EXPECTED_DURATION}"
    print("PASS: silent slideshow")




def test_short_audio_kept():
    """3s tone under a 6s slideshow → audio present, video still 6s."""
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "out.mp4")
        tone = _make_tone(os.path.join(td, "tone.wav"), seconds=3.0)
        make_slideshow(_make_images(td), output=out, duration=SECONDS_PER_IMAGE,
                       swipe=False, audio_path=tone)
        dur, has_audio = _probe(out)
        assert has_audio, "audio stream expected"
        assert abs(dur - EXPECTED_DURATION) < 0.5, f"duration {dur} != ~{EXPECTED_DURATION}"
    print("PASS: short audio kept")




def test_long_audio_trimmed():
    """20s tone over a 6s slideshow → trimmed, video stays 6s."""
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "out.mp4")
        tone = _make_tone(os.path.join(td, "tone.wav"), seconds=20.0)
        make_slideshow(_make_images(td), output=out, duration=SECONDS_PER_IMAGE,
                       swipe=False, audio_path=tone)
        dur, has_audio = _probe(out)
        assert has_audio, "audio stream expected"
        assert abs(dur - EXPECTED_DURATION) < 0.5, f"duration {dur} != ~{EXPECTED_DURATION}"
    print("PASS: long audio trimmed")




def test_bad_audio_degrades_to_silent():
    """Garbage audio_path → silent mp4 is still produced (never fails the item)."""
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "out.mp4")
        garbage = os.path.join(td, "not_audio.wav")
        with open(garbage, "wb") as f:
            f.write(b"this is not audio data")
        make_slideshow(_make_images(td), output=out, duration=SECONDS_PER_IMAGE,
                       swipe=False, audio_path=garbage)
        assert os.path.getsize(out) > 0
        dur, has_audio = _probe(out)
        assert not has_audio, "unusable audio must degrade to a silent slideshow"
        assert abs(dur - EXPECTED_DURATION) < 0.5
    print("PASS: bad audio degrades to silent")




if __name__ == "__main__":
    test_silent_slideshow()
    test_short_audio_kept()
    test_long_audio_trimmed()
    test_bad_audio_degrades_to_silent()
    print("All make_slideshow audio tests passed.")
