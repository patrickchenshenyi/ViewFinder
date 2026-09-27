"""Generate a macOS app icon for the Brush wrapper.

Draws a dark rounded-square with a cluster of soft coloured blobs, standing in
for a rendered gaussian splat scene. Writes a full .iconset directory.
"""
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

S = 1024
OUT = sys.argv[1] if len(sys.argv) > 1 else "AppIcon.iconset"
os.makedirs(OUT, exist_ok=True)

# --- background: vertical gradient, dark slate -> deep indigo ---
top = np.array([28, 32, 48], dtype=np.float64)
bot = np.array([12, 14, 22], dtype=np.float64)
t = np.linspace(0.0, 1.0, S)[:, None, None]
bg = top[None, None, :] * (1 - t) + bot[None, None, :] * t
img = np.repeat(bg, S, axis=1)

# --- splat blobs ---
rng = np.random.default_rng(7)
yy, xx = np.mgrid[0:S, 0:S].astype(np.float64)

blobs = [
    (0.50, 0.44, 0.30, 0.115, (255, 176, 92), 1.00),   # warm core
    (0.36, 0.56, 0.19, 0.085, (255, 214, 140), 0.85),
    (0.63, 0.57, 0.20, 0.090, (240, 120, 96), 0.80),
    (0.47, 0.66, 0.24, 0.070, (120, 210, 220), 0.75),
    (0.32, 0.36, 0.13, 0.060, (150, 200, 255), 0.70),
    (0.68, 0.38, 0.14, 0.065, (210, 150, 255), 0.70),
]
# a scatter of small satellites, like individual gaussians
for _ in range(90):
    cx, cy = rng.uniform(0.18, 0.82, 2)
    r = rng.uniform(0.008, 0.030)
    col = tuple(int(c) for c in rng.integers(120, 255, 3))
    blobs.append((cx, cy, r, r * 0.55, col, rng.uniform(0.25, 0.75)))

for (cx, cy, rx, ry, col, strength) in blobs:
    d2 = ((xx - cx * S) / (rx * S)) ** 2 + ((yy - cy * S) / (ry * S)) ** 2
    w = np.exp(-2.2 * d2) * strength
    for c in range(3):
        img[:, :, c] = img[:, :, c] * (1 - w) + col[c] * w

img = np.clip(img, 0, 255).astype(np.uint8)
icon = Image.fromarray(img).convert("RGBA")

# --- macOS-style rounded square mask with transparent corners ---
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.2237), fill=255)
icon.putalpha(mask)
# inset the artwork slightly so the shape breathes like a real macOS icon
inner = Image.new("RGBA", (S, S), (0, 0, 0, 0))
inner.paste(icon, (0, 0))

sizes = {
    "icon_16x16.png": 16, "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128, "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256, "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
}
for name, size in sizes.items():
    inner.resize((size, size), Image.LANCZOS).save(os.path.join(OUT, name))

print(f"wrote {len(sizes)} pngs to {OUT}")
