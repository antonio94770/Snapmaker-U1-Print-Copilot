"""Local, offline previews of measured G-code paths. No browser dependencies."""

from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from print_copilot.gcode.paths import Toolpaths
from print_copilot.model.render import _draw_line, _draw_text, encode_png

PALETTE = (
    "#2563eb",
    "#9333ea",
    "#0f766e",
    "#c2410c",
    "#be123c",
    "#475569",
    "#7c6f00",
    "#0891b2",
    "#db2777",
    "#166534",
    "#6d28d9",
    "#a16207",
    "#155e75",
    "#991b1b",
)


@dataclass
class Preview:
    png_path: Path
    html_path: Path
    layers_shown: list[int]
    all_layers: int
    segment_count: int
    notes: list[str]


def preview_layers(paths: Toolpaths) -> list[int]:
    layers = paths.layers
    picks = {0, len(layers) // 2, len(layers) - 1}
    for feature in ("Bridge", "Overhang", "Support interface"):
        best = max(
            range(len(layers)),
            key=lambda i: sum(feature.lower() in s.feature.lower() for s in layers[i].segments),
        )
        if any(feature.lower() in s.feature.lower() for s in layers[best].segments):
            picks.add(best)
    return sorted(picks)


def write_preview(paths: Toolpaths, directory: Path, *, layers: list[int] | None = None) -> Preview:
    """Write every path into HTML and up to six representative layers into PNG.

    No downsampling: an oversized HTML file fails visibly rather than silently
    omitting toolpaths. The PNG explicitly lists its selected layers.
    """
    png_path, html_path = directory / "toolpaths.png", directory / "toolpaths.html"
    if png_path.exists() or html_path.exists():
        raise FileExistsError("Preview output already exists; use a fresh directory.")
    features = sorted({s.feature for layer in paths.layers for s in layer.segments})
    colors = {name: PALETTE[i % len(PALETTE)] for i, name in enumerate(features)}
    starts = np.array([s.start[:2] for layer in paths.layers for s in layer.segments])
    ends = np.array([s.end[:2] for layer in paths.layers for s in layer.segments])
    low, high = np.minimum(starts.min(0), ends.min(0)), np.maximum(starts.max(0), ends.max(0))
    span = max(float((high - low).max()), 1)
    center = (low + high) / 2
    picks = preview_layers(paths)
    if layers is not None:
        available = {layer.number: i for i, layer in enumerate(paths.layers)}
        if not layers or len(layers) > 6 or set(layers) - available.keys():
            raise ValueError("Choose one to six nonempty layer numbers present in the G-code.")
        picks = sorted({available[number] for number in layers})
    columns = min(3, len(picks))
    cell = 1000 if columns == 1 else 600 if columns == 2 else 420
    rows = (len(picks) + columns - 1) // columns
    legend_columns = max(1, columns * cell // 310)
    footer_height = 65 + math.ceil(len(features) / legend_columns) * 18
    canvas = np.full((rows * cell + footer_height, columns * cell, 3), 250 / 255, dtype=float)
    scale = (cell - 60) / span
    for n, idx in enumerate(picks):
        ox, oy = (n % columns) * cell, (n // columns) * cell
        layer = paths.layers[idx]
        _draw_text(
            canvas,
            f"LAYER {layer.number} Z {layer.z_mm:.3f} MM",
            ox + 12,
            oy + 12,
            (25 / 255, 35 / 255, 50 / 255),
        )
        for seg in layer.segments:
            a = (np.array(seg.start[:2]) - center) * scale * [1, -1] + [
                ox + cell / 2,
                oy + cell / 2,
            ]
            b = (np.array(seg.end[:2]) - center) * scale * [1, -1] + [ox + cell / 2, oy + cell / 2]
            color = np.array(tuple(bytes.fromhex(colors[seg.feature][1:]))) / 255
            _draw_line(canvas, a, b, color)
            if seg.path_start and seg.feature == "Outer wall":
                x, y = np.rint(a).astype(int)
                canvas[max(0, y - 2) : y + 3, max(0, x - 2) : x + 3] = (0, 0, 0)
    _draw_text(
        canvas,
        "TOP VIEW: SELECTED LAYERS ONLY / X RIGHT / Y UP",
        12,
        rows * cell + 8,
        (25 / 255, 35 / 255, 50 / 255),
    )
    _draw_text(
        canvas,
        "BLACK DOTS: OUTER WALL STARTS (SEAM CANDIDATES)",
        12,
        rows * cell + 24,
        (25 / 255, 35 / 255, 50 / 255),
    )
    _draw_text(
        canvas,
        f"XY BOUNDS: {low[0]:.2f} {low[1]:.2f} TO {high[0]:.2f} {high[1]:.2f} MM",
        12,
        rows * cell + 42,
        (25 / 255, 35 / 255, 50 / 255),
    )
    for i, name in enumerate(features):
        _draw_text(
            canvas,
            name.upper(),
            12 + (i % legend_columns) * 310,
            rows * cell + 62 + (i // legend_columns) * 18,
            np.array(tuple(bytes.fromhex(colors[name][1:]))) / 255,
        )
    payload = {
        "features": features,
        "colors": [colors[name] for name in features],
        "bounds": [low.tolist(), high.tolist()],
        "notes": paths.notes,
        "layers": [
            {
                "number": layer.number,
                "z": layer.z_mm,
                "segments": [
                    [
                        *[round(v, 4) for v in (*s.start[:2], *s.end[:2])],
                        s.tool,
                        features.index(s.feature),
                        s.feed_mm_s,
                        s.path_start and s.feature == "Outer wall",
                    ]
                    for s in layer.segments
                ],
            }
            for layer in paths.layers
        ],
    }
    encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
    if len(encoded) > 100_000_000:
        raise ValueError(
            "Offline preview exceeds 100 MB; use a smaller plate. No paths were omitted."
        )
    template = Path(__file__).with_name("quality_viewer.html").read_text(encoding="utf-8")
    content = template.replace("__PAYLOAD__", encoded).replace(
        "__TITLE__", html.escape(directory.name)
    )
    # Exclusive output prevents accidentally overwriting a prior visual review.
    with png_path.open("xb") as stream:
        stream.write(encode_png(canvas))
    with html_path.open("x", encoding="utf-8") as stream:
        stream.write(content)
    return Preview(
        png_path,
        html_path,
        [paths.layers[i].number for i in picks],
        len(paths.layers),
        paths.segment_count,
        paths.notes,
    )
