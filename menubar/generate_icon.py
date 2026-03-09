#!/usr/bin/env python3
"""Generate Fisherman app icon at all required macOS sizes."""

import math
import os
from PIL import Image, ImageDraw


def draw_rounded_rect(draw, xy, radius, fill):
    """Draw a rounded rectangle."""
    x0, y0, x1, y1 = xy
    r = radius
    # Four corners
    draw.pieslice([x0, y0, x0 + 2 * r, y0 + 2 * r], 180, 270, fill=fill)
    draw.pieslice([x1 - 2 * r, y0, x1, y0 + 2 * r], 270, 360, fill=fill)
    draw.pieslice([x0, y1 - 2 * r, x0 + 2 * r, y1], 90, 180, fill=fill)
    draw.pieslice([x1 - 2 * r, y1 - 2 * r, x1, y1], 0, 90, fill=fill)
    # Fill center rects
    draw.rectangle([x0 + r, y0, x1 - r, y1], fill=fill)
    draw.rectangle([x0, y0 + r, x1, y1 - r], fill=fill)


def draw_gradient_bg(img, size, radius):
    """Draw ocean blue gradient background with rounded rect mask."""
    # Create gradient on full image
    draw = ImageDraw.Draw(img)
    top_color = (20, 100, 200)  # deeper blue at top
    bot_color = (30, 160, 220)  # lighter blue at bottom

    for y in range(size):
        t = y / max(size - 1, 1)
        r = int(top_color[0] + (bot_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bot_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bot_color[2] - top_color[2]) * t)
        draw.line([(0, y), (size - 1, y)], fill=(r, g, b))

    # Create rounded rect mask
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    draw_rounded_rect(mask_draw, (0, 0, size, size), radius, fill=255)

    # Apply mask — set pixels outside rounded rect to transparent
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg.paste(img, mask=mask)
    return bg


def draw_fisherman(draw, size):
    """Draw a white fisherman silhouette scaled to the given size."""
    s = size / 512.0  # scale factor relative to 512px reference

    white = (255, 255, 255, 255)
    line_w = max(int(6 * s), 1)
    thin_w = max(int(3 * s), 1)

    # --- Person standing on right side ---
    # Head
    head_cx = int(310 * s)
    head_cy = int(175 * s)
    head_r = int(28 * s)
    draw.ellipse(
        [head_cx - head_r, head_cy - head_r, head_cx + head_r, head_cy + head_r],
        fill=white,
    )

    # Body (torso)
    body_top = (int(310 * s), int(203 * s))
    body_bot = (int(300 * s), int(320 * s))
    draw.line([body_top, body_bot], fill=white, width=line_w)

    # Legs
    hip = body_bot
    left_foot = (int(270 * s), int(410 * s))
    right_foot = (int(330 * s), int(410 * s))
    draw.line([hip, left_foot], fill=white, width=line_w)
    draw.line([hip, right_foot], fill=white, width=line_w)

    # Arms — one arm holds rod forward, other arm supports
    shoulder = (int(308 * s), int(230 * s))
    # Back hand (left arm, reaching back slightly)
    back_hand = (int(265 * s), int(260 * s))
    draw.line([shoulder, back_hand], fill=white, width=line_w)

    # Front hand (right arm, extended forward holding rod)
    front_hand = (int(340 * s), int(240 * s))
    draw.line([shoulder, front_hand], fill=white, width=line_w)

    # --- Fishing rod ---
    rod_base = front_hand
    rod_tip = (int(140 * s), int(130 * s))
    draw.line([rod_base, rod_tip], fill=white, width=max(int(4 * s), 1))

    # --- Fishing line (curved, from rod tip down to water) ---
    # Use a series of points to simulate a curve
    points = []
    tip_x, tip_y = rod_tip
    hook_x, hook_y = int(120 * s), int(380 * s)

    num_pts = 30
    for i in range(num_pts + 1):
        t = i / num_pts
        # Quadratic bezier with control point to create a nice droop
        ctrl_x, ctrl_y = int(80 * s), int(300 * s)
        x = (1 - t) ** 2 * tip_x + 2 * (1 - t) * t * ctrl_x + t**2 * hook_x
        y = (1 - t) ** 2 * tip_y + 2 * (1 - t) * t * ctrl_y + t**2 * hook_y
        points.append((int(x), int(y)))

    for i in range(len(points) - 1):
        draw.line([points[i], points[i + 1]], fill=white, width=thin_w)

    # --- Water surface (wavy line near bottom) ---
    water_y_base = int(410 * s)
    wave_pts = []
    for x in range(int(30 * s), int(482 * s), max(int(4 * s), 1)):
        wy = water_y_base + int(8 * s * math.sin(x / (20 * s)))
        wave_pts.append((x, wy))
    if len(wave_pts) > 1:
        draw.line(wave_pts, fill=white, width=thin_w)

    # Small bobber/float at hook point
    bobber_r = max(int(6 * s), 1)
    draw.ellipse(
        [hook_x - bobber_r, hook_y - bobber_r, hook_x + bobber_r, hook_y + bobber_r],
        fill=white,
    )


def generate_icon(size):
    """Generate a single icon at the given pixel size."""
    radius = int(size * 0.18)  # macOS icon corner radius ~18%

    # Start with RGB gradient
    img = Image.new("RGB", (size, size), (0, 0, 0))
    img = draw_gradient_bg(img, size, radius)

    draw = ImageDraw.Draw(img)
    draw_fisherman(draw, size)

    return img


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    iconset_dir = os.path.join(script_dir, "icon.iconset")
    os.makedirs(iconset_dir, exist_ok=True)

    # Apple iconset spec: (name, pixel_size)
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

    # Cache rendered sizes to avoid re-drawing duplicates
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
