#!/usr/bin/env python3
"""Generate the Fisherman app icon at all required macOS sizes.

The mark is a lowercase italic serif "f" set in Georgia Bold Italic on
a warm-ink squircle. The letterform's natural swash-and-descender
silhouette doubles as a fishhook — a typographic mark that earns its
name. Cream on warm ink, matching the website palette (see
website/src/styles.css: --paper / --ink).

We use Georgia Bold Italic because it's preinstalled on every macOS
and has the heaviest, most fishhook-like stroke of the system serifs.
"""

import os
from PIL import Image, ImageDraw, ImageFont


# ── Palette (must match website/src/styles.css) ───────────────────
INK_TOP = (24, 22, 20)        # warm deep ink (top of gradient)
INK_BOT = (12, 12, 11)        # near-black (bottom)
PAPER = (243, 236, 220, 255)  # cream — same as site --paper


# Mac-system Georgia Bold Italic — preinstalled on every macOS install.
# We fall back to plain Georgia Italic if Bold Italic is missing for any
# reason, then to Charter, then to PIL's default font. The fallback
# chain means the script won't hard-fail in oddball environments.
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Georgia Bold Italic.ttf",
    "/System/Library/Fonts/Supplemental/Georgia Italic.ttf",
    "/System/Library/Fonts/Supplemental/Charter.ttc",
]


def draw_rounded_rect(draw, xy, radius, fill):
    x0, y0, x1, y1 = xy
    r = radius
    draw.pieslice([x0, y0, x0 + 2 * r, y0 + 2 * r], 180, 270, fill=fill)
    draw.pieslice([x1 - 2 * r, y0, x1, y0 + 2 * r], 270, 360, fill=fill)
    draw.pieslice([x0, y1 - 2 * r, x0 + 2 * r, y1], 90, 180, fill=fill)
    draw.pieslice([x1 - 2 * r, y1 - 2 * r, x1, y1], 0, 90, fill=fill)
    draw.rectangle([x0 + r, y0, x1 - r, y1], fill=fill)
    draw.rectangle([x0, y0 + r, x1, y1 - r], fill=fill)


def load_font(pixel_size):
    """Load the first available serif font from the candidate list."""
    for path in FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            if path.endswith(".ttc"):
                # Charter.ttc carries multiple weights; index=1 is italic.
                return ImageFont.truetype(path, size=pixel_size, index=1)
            return ImageFont.truetype(path, size=pixel_size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_background(size, radius):
    """Warm-ink gradient inside the squircle, transparent outside."""
    bg_rgb = Image.new("RGB", (size, size), INK_TOP)
    px = bg_rgb.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        r = int(INK_TOP[0] + (INK_BOT[0] - INK_TOP[0]) * t)
        g = int(INK_TOP[1] + (INK_BOT[1] - INK_TOP[1]) * t)
        b = int(INK_TOP[2] + (INK_BOT[2] - INK_TOP[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b)

    mask = Image.new("L", (size, size), 0)
    draw_rounded_rect(ImageDraw.Draw(mask), (0, 0, size, size), radius, fill=255)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(bg_rgb, mask=mask)
    return out


def draw_mark(size):
    """Render the lowercase italic 'f' centered on a transparent layer."""
    # Render at 4× then downsample for crisp edges at every output size.
    super_scale = 4 if size <= 256 else 2
    big = size * super_scale
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # The "f" body fills about 78% of the icon area. The font's natural
    # bounding box includes ascender + descender room we don't need —
    # cap-height + descender ≈ 0.78 * em, so a font size of em = size
    # leaves comfortable air around the glyph.
    font_px = int(big * 0.86)
    font = load_font(font_px)

    # Anchor "mm" centers the bounding box on the given point. Italic
    # glyphs have a slight visual lean — nudge right a touch so the
    # optical center matches the geometric center.
    cx = big // 2 + int(big * 0.012)
    cy = big // 2 + int(big * 0.02)  # bias down slightly for descender weight
    draw.text((cx, cy), "f", fill=PAPER, font=font, anchor="mm")

    return layer.resize((size, size), Image.LANCZOS)


def generate_icon(size):
    radius = int(size * 0.225)  # modern macOS squircle radius
    bg = render_background(size, radius)
    mark = draw_mark(size)
    return Image.alpha_composite(bg, mark)


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    iconset_dir = os.path.join(script_dir, "icon.iconset")
    os.makedirs(iconset_dir, exist_ok=True)

    specs = [
        ("icon_16x16.png", 16),
        ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32),
        ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512),
        ("icon_512x512@2x.png", 1024),
    ]

    cache = {}
    for name, px in specs:
        if px not in cache:
            cache[px] = generate_icon(px)
            print(f"  rendered {px}x{px}")
        cache[px].save(os.path.join(iconset_dir, name), "PNG")
        print(f"  saved {name}")

    print(f"\nIconset ready at {iconset_dir}")
    print("Run: iconutil -c icns icon.iconset -o AppIcon.icns")


if __name__ == "__main__":
    main()
