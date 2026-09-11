#!/usr/bin/env python3
"""Rasterize web/public/icons/dotori-mark.svg into dotori.ico at the repo root.

This is a one-time (or run-when-the-mark-changes) build step, not a runtime
dependency: install.py's desktop shortcut and dotori_tray.py's tray icon
both just load the resulting dotori.ico, so end users never need svg.path or
this script. Requires `pip install svg.path` (pure Python, no native deps).

Usage: python scripts/generate_dotori_ico.py
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw
from svg.path import parse_path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_SVG = REPO_ROOT / "web" / "public" / "icons" / "dotori-mark.svg"
OUTPUT_ICO = REPO_ROOT / "dotori.ico"

VIEWBOX_SIZE = 64
SUPERSAMPLE = 8  # render at 512x512 then downsample for anti-aliasing
SAMPLES_PER_PATH = 400  # bezier flattening resolution
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)]


def _load_paths(svg_path: Path) -> list[tuple[str, str]]:
    """Return [(fill_color, path_d), ...] in document order."""
    ns = {"svg": "http://www.w3.org/2000/svg"}
    root = ET.parse(svg_path).getroot()
    paths = root.findall(".//svg:path", ns) or root.findall(".//path")
    return [(el.get("fill", "#000000"), el.get("d", "")) for el in paths]


def _flatten(d: str, scale: float) -> list[tuple[float, float]]:
    path = parse_path(d)
    points = []
    for i in range(SAMPLES_PER_PATH + 1):
        t = i / SAMPLES_PER_PATH
        point = path.point(t)
        points.append((point.real * scale, point.imag * scale))
    return points


def render(svg_path: Path = SOURCE_SVG, output_ico: Path = OUTPUT_ICO) -> Path:
    scale = SUPERSAMPLE
    canvas_size = VIEWBOX_SIZE * scale
    image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    for fill, d in _load_paths(svg_path):
        if not d:
            continue
        polygon = _flatten(d, scale)
        draw.polygon(polygon, fill=fill)

    base = image.resize((256, 256), Image.LANCZOS)
    base.save(output_ico, format="ICO", sizes=ICO_SIZES)
    return output_ico


if __name__ == "__main__":
    path = render()
    print(f"Wrote {path}")
