"""Build the social-share card the public pages advertise as ``og:image``.

Every link to foryouresearch.net posted on LinkedIn, Slack, X, WhatsApp or
Discord renders from this one file. Without it those platforms pick an
arbitrary image off the page, or nothing, and the project's own promotion —
which runs mostly through LinkedIn and TikTok — looks unattributed.

The card reuses the About page's video mosaic as its background, so the share
image and the site show the same corpus, and overlays the wordmark, the
landing-page question and the domain. Output is a 1200x630 JPEG: the size every
major platform crops to, in a format all of them decode (WebP is still uneven
across link unfurlers, so this is deliberately not WebP).

Run from the project root after changing the hero or the wording:

    python scripts/make_og_card.py
"""

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LANDING = PROJECT_ROOT / "web_interface" / "static" / "landing"
DEFAULT_SOURCE = LANDING / "video_grid_hero.webp"
DEFAULT_OUTPUT = LANDING / "og_card.jpg"

WIDTH, HEIGHT = 1200, 630
MARGIN = 72

EYEBROW = "THE FOR YOU RESEARCH PROJECT"
HEADLINE = ("What shapes your", "For You feed?")
DOMAIN = "foryouresearch.net"

# --fyr-grad from style.css, as (stop, RGB). The card carries the site's accent
# stripe so a share and a visit look like the same project.
BRAND_STOPS = ((0.0, (236, 72, 153)), (0.35, (249, 115, 22)),
               (0.70, (13, 148, 136)), (1.0, (139, 92, 246)))
STRIPE_HEIGHT = 10

# The site sets DM Sans over Inter over the system sans. None of those ship
# with the repo, so try the closest faces a macOS or Linux box is likely to
# have and fall back to PIL's bitmap font rather than failing the build.
FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Futura.ttc",
    "/System/Library/Fonts/Avenir Next.ttc",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def load_font(size):
    """The first available candidate face at ``size``, else PIL's default."""
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def cover(image, width, height):
    """Resize and centre-crop ``image`` to exactly ``width`` x ``height``."""
    scale = max(width / image.width, height / image.height)
    resized = image.resize((round(image.width * scale), round(image.height * scale)),
                           Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def scrim(width, height):
    """A black gradient that makes overlaid text readable.

    Two gradients composited: one bottom-weighted, one left-weighted. The
    vertical one alone left the eyebrow line sitting mid-image over a wall of
    thumbnails at full contrast, where it was effectively unreadable. The
    horizontal one darkens the text column and has faded out by the right
    third, so the mosaic still reads as a mosaic.

    Computed at a tenth scale and resized up — a smooth gradient loses nothing
    to interpolation, and it saves iterating over 756,000 pixels in Python.
    """
    w, h = max(2, width // 10), max(2, height // 10)
    layer = Image.new("L", (w, h))
    pixels = layer.load()
    for y in range(h):
        vertical = min(1.0, 0.10 + 0.86 * (y / (h - 1)) ** 1.5)
        for x in range(w):
            horizontal = 0.60 * max(0.0, 1.0 - (x / (w - 1)) / 0.62) ** 1.4
            # Alpha-composite the two rather than adding them, so neither can
            # push the result past opaque and flatten the image to black.
            pixels[x, y] = round(255 * min(0.96, 1 - (1 - vertical) * (1 - horizontal)))
    return layer.resize((width, height), Image.BILINEAR)


def brand_stripe(width, height):
    """The four-stop brand gradient as a horizontal bar."""
    bar = Image.new("RGB", (width, 1))
    for x in range(width):
        t = x / (width - 1)
        for i in range(len(BRAND_STOPS) - 1):
            t0, c0 = BRAND_STOPS[i]
            t1, c1 = BRAND_STOPS[i + 1]
            if t0 <= t <= t1:
                k = (t - t0) / (t1 - t0)
                bar.putpixel((x, 0), tuple(round(a + (b - a) * k)
                                          for a, b in zip(c0, c1, strict=True)))
                break
    return bar.resize((width, height))


def draw_tracked(draw, xy, text, font, fill, tracking):
    """Draw ``text`` with extra letter spacing; PIL has no tracking of its own."""
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + tracking


def build(source, output):
    """Compose the card and write it as a JPEG."""
    with Image.open(source) as raw:
        card = cover(raw.convert("RGB"), WIDTH, HEIGHT)

    card.paste(Image.new("RGB", (WIDTH, HEIGHT), (8, 12, 18)), (0, 0), scrim(WIDTH, HEIGHT))
    card.paste(brand_stripe(WIDTH, STRIPE_HEIGHT), (0, HEIGHT - STRIPE_HEIGHT))

    draw = ImageDraw.Draw(card)
    eyebrow_font = load_font(23)
    headline_font = load_font(72)
    domain_font = load_font(28)

    # Laid out from the baseline up, so the block stays anchored to the stripe
    # however tall the headline turns out to be.
    y = HEIGHT - STRIPE_HEIGHT - MARGIN - 34
    draw.text((MARGIN, y), DOMAIN, font=domain_font, fill=(214, 222, 232))

    y -= 24
    for line in reversed(HEADLINE):
        y -= 82
        draw.text((MARGIN, y), line, font=headline_font, fill=(255, 255, 255))

    y -= 44
    draw_tracked(draw, (MARGIN, y), EYEBROW, eyebrow_font, (176, 196, 214), 2.4)

    card.save(output, "JPEG", quality=88, optimize=True, progressive=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help="background image (default: the About page hero)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if not args.source.exists():
        sys.exit(f"Background image not found: {args.source}")

    written = build(args.source, args.output)
    size_kb = written.stat().st_size / 1024
    print(f"Wrote {written.relative_to(PROJECT_ROOT)} ({WIDTH}x{HEIGHT}, {size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
