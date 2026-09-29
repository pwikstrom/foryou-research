"""Photo-post slideshows: render a carousel's images (and its audio) into one video.

TikTok and Instagram photo posts have no video stream; the scraper downloads
their images and this module renders them into an MP4 the viewer can play.
"""

import os
from collections.abc import Sequence

import numpy as np
from PIL import Image, ImageColor

from fyp.core.runtime import cf as _cf


# Longest edge of a slideshow canvas, in pixels. TikTok photo-mode source
# images run up to 2160x3840; rendering the slideshow at that native size makes
# moviepy hold ~15 GiB for a 20-image post (frame buffers scale with canvas
# area), which OOM-pressured the task-runner. Slideshows are display media, not
# archival: capping the longest edge bounds the whole moviepy/ffmpeg pipeline
# to well under 1 GiB. Overridable via ``[misc] slideshow_max_dimension``.
def _slideshow_max_dimension() -> int:
    """Lazy accessor for the slideshow canvas cap (see comment above)."""
    try:
        return int(_cf()["misc"].get("slideshow_max_dimension", 1000))
    except (KeyError, TypeError, ValueError):
        return 1000


def _patch_moviepy_audio_reader_del() -> None:
    """Give moviepy's FFMPEG_AudioReader a class-level ``proc`` default.

    ``AudioFileClip`` on a file ffmpeg cannot parse raises inside
    ``__init__`` before ``self.proc`` is assigned; the interpreter then runs
    ``__del__`` → ``close()`` → ``if self.proc`` and prints an
    ``AttributeError`` traceback that Cloud Logging files as ERROR-severity.
    The audio failure itself is already handled (silent
    slideshow); the class attribute only makes the destructor quiet.
    """
    try:
        from moviepy.audio.io.readers import FFMPEG_AudioReader
    except Exception:
        return
    if "proc" not in vars(FFMPEG_AudioReader):
        FFMPEG_AudioReader.proc = None


def make_slideshow(
    files: list[str],
    output: str = "slideshow.mp4",
    duration: float = 3.0,
    transition: float = 0.6,
    swipe: bool = True,
    canvas_size: tuple[int, int] = None,  # auto if None
    bg_color: str | tuple[int, int, int] = "#000000",
    fps: int = 1,
    codec: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    audio_path: str | None = None,
    verbose=False,
):
    """Create a slideshow video from a list of image files.

    Args:
        files: image file paths, in slide order.
        output: output mp4 path.
        duration: seconds each image is shown.
        transition: swipe transition length in seconds (capped at duration).
        swipe: animate each slide in with a horizontal swipe.
        canvas_size: output (width, height); inferred from the images when None.
            Always clamped so the longest edge is at most
            ``SLIDESHOW_MAX_DIMENSION`` (moviepy frame buffers scale with
            canvas area — an uncapped 2160x3840 canvas costs ~15 GiB).
        bg_color: letterbox background color (name/hex string or RGB tuple).
        fps: output frame rate.
        codec: video codec passed to ffmpeg.
        crf: constant rate factor (quality) passed to ffmpeg.
        preset: ffmpeg encoder preset.
        audio_path: optional audio file muxed under the slideshow; trimmed to
            the video length when longer, ends early when shorter. Any audio
            problem degrades to a silent slideshow rather than failing.
        verbose: unused; kept for call-site symmetry.
    """
    from moviepy import (
        AudioFileClip,
        ColorClip,
        CompositeVideoClip,
        ImageClip,
        concatenate_videoclips,
    )

    _patch_moviepy_audio_reader_del()

    def _normalize_color(color):
        if isinstance(color, str):
            try:
                rgb = ImageColor.getrgb(color)
            except ValueError as exc:
                raise ValueError(f"Invalid bg_color {color!r}") from exc
            return tuple(int(c) for c in rgb)
        if isinstance(color, Sequence) and len(color) == 3:
            try:
                return tuple(int(c) for c in color)
            except (TypeError, ValueError) as exc:
                raise ValueError("bg_color tuple must contain numeric values") from exc
        raise TypeError("bg_color must be a color string or an RGB tuple of length 3")

    def _load_fitted(image_path: str, canvas_size: tuple[int, int]) -> np.ndarray:
        # Decode and downscale with PIL *before* the image enters moviepy, so
        # ImageClip holds a canvas-sized array instead of the native-resolution
        # photo and no per-frame Resize effect is needed.
        W, H = canvas_size
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            scale = min(W / im.width, H / im.height)
            target = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
            if target != im.size:
                im = im.resize(target, Image.LANCZOS)
            return np.asarray(im)

    def _make_swipe_pos(width, transition):
        def pos(t):
            if transition <= 0:
                return (0, "center")
            if t <= transition:
                x = width * (1 - t / transition)
            else:
                x = 0
            return (x, "center")

        return pos

    def _build_slide(
        image_path: str,
        duration: float,
        canvas_size: tuple[int, int],
        bg_color,
        swipe: bool,
        transition: float,
    ):
        bg = ColorClip(size=canvas_size, color=bg_color, duration=duration)
        boxed = ImageClip(_load_fitted(image_path, canvas_size), duration=duration)

        if swipe and transition > 0:
            pos = _make_swipe_pos(canvas_size[0], transition)
            animated = boxed.with_position(pos)
        else:
            animated = boxed.with_position(("center", "center"))

        slide = CompositeVideoClip([bg, animated]).with_duration(duration)
        return slide

    def _infer_canvas_size(files: list[str]) -> tuple[int, int]:
        widths = []
        heights = []
        for f in files:
            try:
                with Image.open(f) as im:
                    w, h = im.size
                    widths.append(w)
                    heights.append(h)
            except Exception:
                pass

        if not widths or not heights:
            return (1920, 1080)

        return (max(widths), max(heights))

    def _clamp_canvas(size: tuple[int, int]) -> tuple[int, int]:
        # Bound the longest edge, then round down to even dimensions
        # (libx264 with yuv420p rejects odd frame sizes).
        w, h = size
        longest = max(w, h)
        if longest > _slideshow_max_dimension():
            scale = _slideshow_max_dimension() / longest
            w = round(w * scale)
            h = round(h * scale)
        return (max(2, w - (w % 2)), max(2, h - (h % 2)))

    # Main function logic starts here
    if not files:
        raise ValueError("No input files provided")

    bg_color = _normalize_color(bg_color)

    if canvas_size is None:
        canvas_size = _infer_canvas_size(files)
    canvas_size = _clamp_canvas(canvas_size)

    transition = max(0.0, min(transition, duration))

    slides = []
    for f in files:
        slide = _build_slide(f, duration, canvas_size, bg_color, swipe, transition)
        slides.append(slide)

    final = concatenate_videoclips(slides, method="compose")

    audio_clip = None
    if audio_path and os.path.exists(audio_path):
        try:
            audio_clip = AudioFileClip(audio_path)
            if audio_clip.duration and audio_clip.duration > final.duration:
                audio_clip = audio_clip.subclipped(0, final.duration)
            final = final.with_audio(audio_clip)
        except Exception:
            audio_clip = None  # degrade to a silent slideshow

    final.write_videofile(
        output,
        fps=fps,
        codec=codec,
        audio=final.audio is not None,
        audio_codec="aac",
        # moviepy's default temp-audio filename is not unique per process/thread;
        # derive it from the (unique) output path to survive concurrent workers.
        temp_audiofile=f"{output}.TEMP_audio.m4a",
        preset=preset,
        threads=0,
        ffmpeg_params=["-crf", str(crf)],
        logger=None,
    )

    for s in slides:
        s.close()
    if audio_clip is not None:
        audio_clip.close()
    final.close()
