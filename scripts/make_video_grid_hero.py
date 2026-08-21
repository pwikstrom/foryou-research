"""Build the public mini-site's video-mosaic hero from real corpus media.

Samples random videos from the configured media store (GCS bucket or the local
media directory), grabs one early frame from each, and tiles those frames into a
single wide image — a dense grid of opening frames that shows how large and how
varied the short-video corpus is.

Run from the project root:

    python scripts/make_video_grid_hero.py

The default output is the about page's hero image
(``web_interface/static/landing/video_grid_hero.webp``). Extracted frames are
cached under ``tmp/hero_frames/`` and the sampled id pool under
``tmp/hero_frames/pool.json``, so re-running with a different layout costs no
downloads. Pass ``--refresh-pool`` to draw a fresh random sample of videos.

Requires ``ffmpeg`` on PATH.
"""

import argparse
import io
import json
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Objects whose name ends in one of these are treated as playable media.
MEDIA_EXTS = (".mp4",)

# Listing shards. The bucket holds over a million media objects, far too many to
# walk serially, so the listing is partitioned on the characters that follow the
# media prefix and the partitions are walked in parallel. Item ids are numeric
# and heavily clustered on one leading digit, so digits are split two deep;
# everything else (the per-platform subdirectories) gets one shard per letter.
SHARD_TAILS = (
    [a + b for a in string.digits for b in string.digits]
    + list(string.ascii_lowercase + "_-")
)

# Frames flatter or darker than this are almost always a black lead-in frame or
# a plain title card, which make the mosaic look dead. Measured on a 32x32
# greyscale thumbnail.
MIN_FRAME_STDDEV = 14.0
MIN_FRAME_MEAN = 16.0

# Letterbox trimming: a row or column this dark on average is padding, not
# picture. Trimming stops if it would eat into more than the remaining fraction
# of either dimension, which leaves genuinely dark frames intact.
LETTERBOX_LEVEL = 18.0
MIN_TRIMMED_FRACTION = 0.45

# How much of a video to fetch before asking ffmpeg for a frame. Enough for the
# moov atom plus the first GOP in a faststart file; anything else falls back to
# a full download.
HEAD_BYTES = 1_200_000

# Long edge of a cached frame. Generous enough to re-crop for bigger cells
# without re-downloading, small enough that thousands of them stay cheap.
CACHE_LONG_EDGE = 400

_print_lock = threading.Lock()


def log(msg: str) -> None:
    """Print one progress line to stderr, safely from any worker thread."""
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Sampling the media store
# ---------------------------------------------------------------------------


def _reservoir_add(reservoir: list, item, seen: int, size: int, rng: random.Random) -> None:
    """Fold ``item`` (the ``seen``-th, 1-based) into a size-``size`` reservoir."""
    if len(reservoir) < size:
        reservoir.append(item)
        return
    j = rng.randrange(seen)
    if j < size:
        reservoir[j] = item


def _list_gcs_shard(client, bucket_name: str, prefix: str, size: int, seed: int) -> tuple[list[str], int]:
    """Reservoir-sample one listing shard; return (sample, objects seen)."""
    rng = random.Random(f"{seed}:{prefix}")
    reservoir: list[str] = []
    seen = 0
    blobs = client.list_blobs(bucket_name, prefix=prefix, fields="items(name),nextPageToken")
    for blob in blobs:
        if not blob.name.endswith(MEDIA_EXTS):
            continue
        seen += 1
        _reservoir_add(reservoir, blob.name, seen, size, rng)
    return reservoir, seen


def sample_gcs(bucket_name: str, media_prefix: str, pool_size: int, seed: int, workers: int) -> list[str]:
    """Draw ``pool_size`` random media object names from the bucket.

    The bucket holds over a million objects, so the listing is sharded on the
    characters after the media prefix and walked in parallel. Each shard
    keeps its own reservoir; the shards are then merged in proportion to how
    many objects each actually held, which keeps the overall draw close to
    uniform over the whole store.
    """
    from google.cloud import storage

    client = storage.Client()
    base = media_prefix.rstrip("/") + "/"
    # Over-sample each shard so the weighted merge below has room to draw from.
    per_shard = max(128, pool_size * 2)

    shards: list[tuple[list[str], int]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_list_gcs_shard, client, bucket_name, base + tail, per_shard, seed): tail
            for tail in SHARD_TAILS
        }
        done = 0
        for fut in as_completed(futures):
            reservoir, seen = fut.result()
            done += 1
            if seen:
                shards.append((reservoir, seen))
                log(f"  shard {futures[fut]!r}: {seen:,} objects  ({done}/{len(futures)} shards walked)")

    total = sum(seen for _, seen in shards)
    if not total:
        raise SystemExit(f"No media objects found under gs://{bucket_name}/{base}")
    log(f"  {total:,} media objects across {len(shards)} non-empty shards")

    rng = random.Random(seed)
    weights = [seen for _, seen in shards]
    picked: set[str] = set()
    # Draw a shard in proportion to its size, then an unused name from it.
    attempts = 0
    while len(picked) < pool_size and attempts < pool_size * 40:
        attempts += 1
        (reservoir, _), = rng.choices(shards, weights=weights, k=1)
        if reservoir:
            picked.add(reservoir[rng.randrange(len(reservoir))])
    return sorted(picked)


def sample_local(media_dir: str, pool_size: int, seed: int) -> list[str]:
    """Draw ``pool_size`` random media paths from the local media directory."""
    rng = random.Random(seed)
    reservoir: list[str] = []
    seen = 0
    for root, _dirs, files in os.walk(media_dir):
        for name in files:
            if not name.endswith(MEDIA_EXTS):
                continue
            seen += 1
            _reservoir_add(reservoir, os.path.join(root, name), seen, pool_size, rng)
    if not seen:
        raise SystemExit(f"No media files found under {media_dir}")
    log(f"  {seen:,} media files on disk")
    rng.shuffle(reservoir)
    return reservoir


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------


def _ffmpeg_frame(path: str, seconds: float) -> bytes | None:
    """Extract one frame at ``seconds`` from ``path`` as JPEG bytes."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        out = tmp.name
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error",
                "-ss", f"{seconds:.2f}", "-i", path,
                "-frames:v", "1", "-q:v", "3", "-y", out,
            ],
            capture_output=True,
        )
        if proc.returncode != 0 or not os.path.getsize(out):
            return None
        with open(out, "rb") as fh:
            return fh.read()
    except OSError:
        return None
    finally:
        os.unlink(out)


def _frame_is_lively(jpeg: bytes) -> bool:
    """True when a frame has enough tone and contrast to earn a mosaic cell."""
    try:
        img = Image.open(io.BytesIO(jpeg)).convert("L")
    except Exception:
        return False
    img.thumbnail((32, 32))
    pixels = list(img.getdata())
    if not pixels:
        return False
    mean = sum(pixels) / len(pixels)
    variance = sum((p - mean) ** 2 for p in pixels) / len(pixels)
    return variance ** 0.5 >= MIN_FRAME_STDDEV and mean >= MIN_FRAME_MEAN


def _shrink(jpeg: bytes) -> bytes | None:
    """Downscale a frame to the cache's long edge, re-encoded as JPEG."""
    try:
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    except Exception:
        return None
    img.thumbnail((CACHE_LONG_EDGE, CACHE_LONG_EDGE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def _fetch_gcs(bucket, blob_name: str, head_only: bool) -> bytes | None:
    """Download a blob, or just its first :data:`HEAD_BYTES` bytes."""
    try:
        blob = bucket.blob(blob_name)
        if head_only:
            return blob.download_as_bytes(start=0, end=HEAD_BYTES - 1, raw_download=True)
        return blob.download_as_bytes(raw_download=True)
    except Exception:
        return None


def _best_frame(path: str, frame_times: list[float]) -> tuple[bytes | None, bool]:
    """Pick the first lively frame from a file.

    Returns the cache-sized JPEG (or ``None``) alongside whether ffmpeg managed
    to decode *every* requested timestamp — the caller uses that to tell "this
    video has no usable opening frame" apart from "this file is truncated".
    """
    decoded = 0
    for seconds in frame_times:
        jpeg = _ffmpeg_frame(path, seconds)
        if not jpeg:
            continue
        decoded += 1
        if _frame_is_lively(jpeg):
            return _shrink(jpeg), True
    return None, decoded == len(frame_times)


def extract_frame(source, name: str, is_gcs: bool, frame_times: list[float]) -> bytes | None:
    """Return one lively early frame from a video as a cache-sized JPEG.

    Each timestamp in ``frame_times`` is tried in order and the first frame that
    passes the liveliness test wins, so a video that opens on black is still
    represented by a later frame rather than dropped. For GCS sources only the
    head of the object is fetched; the whole object is downloaded just for the
    files ffmpeg could not fully read from that head — a non-faststart file
    whose moov atom sits at the end, or a timestamp past the fetched bytes.
    """
    tmp_path: str | None = None
    try:
        if not is_gcs:
            return _best_frame(name, frame_times)[0]

        for head_only in (True, False):
            data = _fetch_gcs(source, name, head_only)
            if not data:
                continue
            if tmp_path is None:
                fd, tmp_path = tempfile.mkstemp(suffix=".mp4", prefix="fyp_hero_")
                os.close(fd)
            with open(tmp_path, "wb") as fh:
                fh.write(data)
            jpeg, complete = _best_frame(tmp_path, frame_times)
            if jpeg or complete:
                return jpeg
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def trim_letterbox(img: Image.Image) -> Image.Image:
    """Crop the black bars a padded upload bakes into its frame.

    Landscape and square uploads reach the platform padded into a portrait
    frame, so their stored frame has black bands that survive an aspect crop and
    read as holes in the mosaic. Bands are only trimmed while a healthy majority
    of the frame survives, so a genuinely dark frame is left alone.
    """
    grey = img.convert("L")
    w, h = grey.size
    pixels = grey.load()

    def band_is_dark(coords) -> bool:
        total = sum(pixels[x, y] for x, y in coords)
        return total / len(coords) <= LETTERBOX_LEVEL

    # Sample every 4th pixel along a band: enough to classify, 4x cheaper.
    top, bottom = 0, h - 1
    while top < bottom and band_is_dark([(x, top) for x in range(0, w, 4)]):
        top += 1
    while bottom > top and band_is_dark([(x, bottom) for x in range(0, w, 4)]):
        bottom -= 1
    left, right = 0, w - 1
    while left < right and band_is_dark([(left, y) for y in range(0, h, 4)]):
        left += 1
    while right > left and band_is_dark([(right, y) for y in range(0, h, 4)]):
        right -= 1

    if (bottom - top + 1) < h * MIN_TRIMMED_FRACTION or (right - left + 1) < w * MIN_TRIMMED_FRACTION:
        return img
    return img.crop((left, top, right + 1, bottom + 1))


def make_cell(jpeg_path: Path, width: int, height: int) -> Image.Image | None:
    """Trim, centre-crop and resize one cached frame to exactly ``width`` x ``height``."""
    try:
        img = Image.open(jpeg_path).convert("RGB")
    except Exception:
        return None
    img = trim_letterbox(img)
    target = width / height
    w, h = img.size
    if w / h > target:
        new_w = int(round(h * target))
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(round(w / target))
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    return img.resize((width, height), Image.LANCZOS)


def compose(frames: list[Path], cols: int, rows: int, cell_w: int, cell_h: int,
            gap: int, background: str) -> Image.Image:
    """Tile cached frames into the mosaic, left-to-right, top-to-bottom."""
    width = cols * cell_w + (cols - 1) * gap
    height = rows * cell_h + (rows - 1) * gap
    canvas = Image.new("RGB", (width, height), background)
    placed = 0
    for path in frames:
        cell = make_cell(path, cell_w, cell_h)
        if cell is None:
            continue
        col, row = placed % cols, placed // cols
        canvas.paste(cell, (col * (cell_w + gap), row * (cell_h + gap)))
        placed += 1
        if placed >= cols * rows:
            break
    if placed < cols * rows:
        raise SystemExit(f"Only {placed} usable frames for a {cols}x{rows} grid ({cols * rows} cells)")
    return canvas


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def load_media_source(bucket_override: str | None, prefix_override: str | None):
    """Resolve where media lives; return (source, is_gcs, label, prefix)."""
    if bucket_override:
        from google.cloud import storage

        client = storage.Client()
        prefix = prefix_override or "media"
        return (client.bucket(bucket_override), True, f"gs://{bucket_override}/{prefix}", prefix)

    from fyp.fyp_config import fyp_cf

    data_io = fyp_cf["data_io"]
    if data_io["use_gcs_for_media"]:
        bucket = data_io.get("bucket")
        if bucket is None:
            raise SystemExit("use_gcs_for_media is set but no GCS bucket handle is configured")
        prefix = prefix_override or data_io["gcs_media_prefix"]
        return (bucket, True, f"gs://{bucket.name}/{prefix}", prefix)

    media_dir = fyp_cf["paths"]["media"]
    return (None, False, media_dir, media_dir)


def get_pool(args, pool_size: int, source, is_gcs: bool, prefix: str, pool_path: Path) -> list[str]:
    """Load the cached sample of media names, drawing a fresh one when needed."""
    if pool_path.exists() and not args.refresh_pool:
        pool = json.loads(pool_path.read_text())
        if len(pool) >= pool_size:
            log(f"Reusing sampled pool of {len(pool):,} videos ({pool_path})")
            return pool
        log(f"Cached pool has only {len(pool):,} videos; drawing a larger sample")

    log(f"Sampling {pool_size:,} random videos...")
    if is_gcs:
        pool = sample_gcs(source.name, prefix, pool_size, args.seed, args.workers)
    else:
        pool = sample_local(prefix, pool_size, args.seed)
    random.Random(args.seed).shuffle(pool)
    pool_path.parent.mkdir(parents=True, exist_ok=True)
    pool_path.write_text(json.dumps(pool))
    return pool


def harvest(pool: list[str], source, is_gcs: bool, cache_dir: Path, needed: int,
            frame_times: list[float], workers: int) -> list[Path]:
    """Fill the frame cache until ``needed`` usable frames exist; return them."""
    cache_dir.mkdir(parents=True, exist_ok=True)

    def cache_path(name: str) -> Path:
        return cache_dir / (Path(name).stem + ".jpg")

    have = [cache_path(n) for n in pool if cache_path(n).exists()]
    if len(have) >= needed:
        log(f"Frame cache already holds {len(have):,} frames")
        return have[:needed]

    todo = [n for n in pool if not cache_path(n).exists()]
    log(f"Have {len(have):,} cached frames; extracting up to {needed - len(have):,} more "
        f"from {len(todo):,} candidates ({workers} workers)")

    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as pool_exec:
        futures = {pool_exec.submit(extract_frame, source, n, is_gcs, frame_times): n for n in todo}
        try:
            for i, fut in enumerate(as_completed(futures), 1):
                name = futures[fut]
                try:
                    jpeg = fut.result()
                except Exception:
                    jpeg = None
                if jpeg:
                    cache_path(name).write_bytes(jpeg)
                    have.append(cache_path(name))
                else:
                    failures += 1
                if i % 25 == 0 or len(have) >= needed:
                    log(f"  {len(have):,}/{needed:,} frames  ({i:,} tried, {failures:,} unusable)")
                if len(have) >= needed:
                    break
        finally:
            for fut in futures:
                fut.cancel()

    if len(have) < needed:
        raise SystemExit(
            f"Only {len(have)} usable frames from {len(pool)} videos — "
            f"raise --pool or lower --cols/--rows"
        )
    return have[:needed]


def main() -> None:
    """Sample videos, extract frames, and write the mosaic hero image."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cols", type=int, default=48, help="cells across (default: 48)")
    parser.add_argument("--rows", type=int, default=12, help="cells down (default: 12)")
    parser.add_argument("--cell-width", type=int, default=42,
                        help="cell width in px; height follows --aspect (default: 42). Cells are "
                             "deliberately small: the grid is meant to read as mass and variety, "
                             "not to make the people in any one video identifiable.")
    parser.add_argument("--aspect", default="9:16",
                        help="cell aspect ratio W:H, portrait for short video (default: 9:16)")
    parser.add_argument("--gap", type=int, default=0, help="px between cells (default: 0)")
    parser.add_argument("--background", default="#0b0b12", help="colour behind the cells (default: #0b0b12)")
    parser.add_argument("--frame-times", default="0.8,2.5,5.0",
                        help="seconds to try, in order, until a frame looks lively "
                             "(default: 0.8,2.5,5.0)")
    parser.add_argument("--pool", type=int, default=0,
                        help="videos to sample before extraction (default: 3x the cell count)")
    parser.add_argument("--seed", type=int, default=20260821, help="sampling seed")
    parser.add_argument("--workers", type=int, default=12, help="parallel downloads (default: 12)")
    parser.add_argument("--quality", type=int, default=75, help="WebP quality (default: 75)")
    parser.add_argument("--refresh-pool", action="store_true",
                        help="draw a fresh random sample instead of reusing the cached one")
    parser.add_argument("--clear-cache", action="store_true",
                        help="delete cached frames first (forces a full re-download)")
    parser.add_argument("--cache-dir", default=str(PROJECT_ROOT / "tmp" / "hero_frames"),
                        help="where extracted frames are cached")
    parser.add_argument("--bucket", default=None,
                        help="GCS bucket to read media from (default: the configured one)")
    parser.add_argument("--media-prefix", default=None,
                        help="media prefix within the bucket (default: the configured one)")
    parser.add_argument("--out", default=str(
        PROJECT_ROOT / "web_interface" / "static" / "landing" / "video_grid_hero.webp"),
        help="output image path")
    args = parser.parse_args()

    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is not on PATH — install it and re-run")

    aw, _, ah = args.aspect.partition(":")
    cell_w = args.cell_width
    cell_h = int(round(cell_w * int(ah) / int(aw)))
    cells = args.cols * args.rows
    pool_size = args.pool or cells * 3
    frame_times = [float(t) for t in args.frame_times.split(",") if t.strip()]

    cache_dir = Path(args.cache_dir)
    if args.clear_cache and cache_dir.exists():
        shutil.rmtree(cache_dir)

    source, is_gcs, label, prefix = load_media_source(args.bucket, args.media_prefix)
    log(f"Media source: {label}")
    log(f"Grid: {args.cols}x{args.rows} = {cells} videos, cells {cell_w}x{cell_h}px")

    pool = get_pool(args, pool_size, source, is_gcs, prefix, cache_dir / "pool.json")
    frames = harvest(pool, source, is_gcs, cache_dir, cells, frame_times, args.workers)

    random.Random(args.seed).shuffle(frames)
    image = compose(frames, args.cols, args.rows, cell_w, cell_h, args.gap, args.background)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "WEBP", quality=args.quality, method=6)
    log(f"Wrote {out} — {image.width}x{image.height}px, {cells} videos, "
        f"{out.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
