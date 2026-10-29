"""Build an explicitly assembled evidence demo from run_showcase.py output.

Requires Pillow and ffmpeg; neither is a production project dependency.
Usage: python research/showcase/make_media.py runs/complex-showcase-TIMESTAMP
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[2]
RUN = Path(sys.argv[1]).resolve()
OUT = ROOT / "docs/images"
OUT.mkdir(exist_ok=True)
W, H = 1280, 720
BG, FG, MUTED, ACCENT = "#101827", "#f0f4fa", "#a8b5c8", "#50dfbd"
FONT = "C:/Windows/Fonts/segoeui.ttf"
BOLD = "C:/Windows/Fonts/segoeuib.ttf"


def read(name):
    return json.loads((RUN / f"{name}.json").read_text(encoding="utf-8"))


def text(draw, xy, value, size=24, color=FG, bold=False):
    draw.text(xy, value, font=ImageFont.truetype(BOLD if bold else FONT, size), fill=color)


def slide(title, subtitle):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 10, H), fill=ACCENT)
    text(d, (40, 24), "SNAPMAKER PRINT COPILOT  /  REAL MODEL TRIALS", 18, ACCENT, True)
    text(d, (40, 64), title, 38, bold=True)
    text(d, (40, 122), subtitle, 21, MUTED)
    text(
        d,
        (40, 680),
        "Assembled from real CLI evidence | Slicer predictions; no physical print",
        18,
        MUTED,
    )
    return im, d


def picture(im, path, box):
    source = Image.open(path).convert("RGB")
    tile = ImageOps.contain(source, (box[2] - box[0], box[3] - box[1]))
    im.paste(
        tile,
        (
            box[0] + (box[2] - box[0] - tile.width) // 2,
            box[1] + (box[3] - box[1] - tile.height) // 2,
        ),
    )


def duration(seconds):
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def metric_line(draw, x, y, label, value):
    text(draw, (x, y), label, 19, MUTED)
    text(draw, (x, y + 26), value, 30, ACCENT, True)


def main():
    haku = read("haku-comparison")["variants"]
    slices = [v["slice"] for v in haku]
    if len(slices) != 2 or any(v["error"] for v in haku):
        raise ValueError("Two successful Haku slices are required.")
    parts = [
        (name, read(f"planetarium-{name}"))
        for name in ("herringbone", "housing", "calendar", "axle")
    ]
    frames = []
    im, d = slide(
        "Two complex models. One reproducible workflow.",
        "U1 / 0.4 mm nozzle / Snapmaker PLA SnapSpeed / no printer operations",
    )
    picture(im, RUN / "haku.png", (40, 180, 810, 620))
    metric_line(d, 870, 190, "Haku mesh", "168,066 triangles")
    metric_line(
        d, 870, 295, "Planetarium inventory", f"{read('inputs')['planetarium_stl_count']} STL files"
    )
    metric_line(d, 870, 400, "Representative parts sliced", "4 mechanical parts")
    text(
        d,
        (40, 630),
        "Haku: Lynns / CC BY-NC-ND 4.0 | Planetarium: HackerJack42 / CC BY-NC-SA 4.0",
        18,
        MUTED,
    )
    frames.append((im, 5))
    im, d = slide(
        "Haku: preserve supports when importing a project",
        "95.7 x 85.9 x 120 mm | Four project filaments mapped to T0-T3 for this slicing trial",
    )
    picture(im, RUN / "haku.png", (40, 190, 830, 625))
    text(d, (860, 210), "Saved project: 0.20 mm", 25, bold=True)
    text(d, (860, 260), "Selected preset: 0.16 mm", 25, bold=True)
    text(d, (860, 330), "Tree supports kept in both", 24, ACCENT)
    text(d, (860, 375), "Prime tower enabled", 24, ACCENT)
    text(d, (860, 450), "Red: geometric overhang", 21, MUTED)
    text(d, (860, 485), "heuristic; inspect toolpaths", 21, MUTED)
    text(
        d,
        (40, 630),
        "Project: white, navy, cream, teal. App colours differ; no physical print prepared.",
        20,
        MUTED,
    )
    frames.append((im, 6))
    im, d = slide(
        "Haku: measured layer-height comparison",
        "Same grid infill, shell layers, tree supports and prime tower; only layer height changes",
    )
    for x, v, result in zip((55, 665), haku, slices, strict=True):
        text(d, (x, 190), v["label"], 29, bold=True)
        metric_line(d, x, 255, "Slicer print-time prediction", duration(result["estimated_time_s"]))
        metric_line(d, x, 355, "Total filament", f"{result['total_filament_g']:.2f} g")
        metric_line(d, x + 300, 355, "Support paths", f"{result['support']['support_g']:.2f} g")
        metric_line(d, x, 455, "Tool changes", str(result["tool_changes"]))
        metric_line(d, x + 300, 455, "Prime tower", f"{result['prime_tower_g']:.2f} g")
    text(
        d,
        (55, 595),
        "Firmware macro extrusion is not included in toolpath waste figures.",
        22,
        MUTED,
    )
    text(
        d,
        (55, 630),
        "Layer height alone does not prove finish; visual and physical checks remain necessary.",
        20,
        MUTED,
    )
    frames.append((im, 7))
    im, d = slide(
        "Haku: inspect the actual sliced paths",
        "0.20 mm: layer-count conflict | 0.16 mm: full HTML exceeds 100 MB; excerpt only",
    )
    for index, box in enumerate(((35, 180, 630, 630), (650, 180, 1245, 630))):
        report = read(f"haku-quality-{index}")
        preview = report["variants"][0]["preview"]
        if not preview:
            preview = json.loads((RUN / "haku-fine-selected" / "excerpt.json").read_text())
        picture(im, preview["png_path"], box)
    frames.append((im, 6))
    im, d = slide(
        "Planetarium: gears, housing and a thin axle",
        "49 STL files inventoried; four representative components sliced on T0 white",
    )
    for i, (name, _) in enumerate(parts):
        x, y = 35 + (i % 2) * 620, 180 + (i // 2) * 235
        picture(im, RUN / f"planetarium-{name}.png", (x, y, x + 590, y + 195))
        text(d, (x, y + 195), name.title(), 20, ACCENT)
    frames.append((im, 6))
    im, d = slide(
        "A serious demo also shows what failed",
        "Slicing completed; quality review remains a separate, explicit check",
    )
    text(d, (55, 210), "Haku: conflicting layer counts in Orca output", 31, ACCENT, True)
    text(d, (55, 265), "0.20 mm: header/path markers 599; final summary 600", 27)
    text(d, (55, 315), "0.16 mm: both counts agree at 749 layers", 27)
    text(d, (55, 400), "0.16 mm full HTML exceeds 100 MB; selected-layer excerpt retained.", 25)
    text(d, (55, 450), "Source geometry unchanged; no model downloads bundled in the repo.", 25)
    text(
        d,
        (55, 540),
        "No hardware print, fit test or surface-quality validation performed.",
        25,
        MUTED,
    )
    text(d, (55, 590), "See docs/complex-model-trials.md for sources and limitations.", 25, MUTED)
    limitations = (im, 6)
    im, d = slide(
        "Planetarium: measured preparation results",
        "0.16 mm Standard / 3 walls / T0 / fit and toolpaths inspected; assembly unverified",
    )
    for x, value in ((55, "Component"), (425, "Slicer time"), (710, "Filament"), (990, "Support")):
        text(d, (x, 205), value, 22, MUTED)
    for i, (name, result) in enumerate(parts):
        y = 270 + i * 70
        for x, value in (
            (55, name.title()),
            (425, duration(result["estimated_time_s"])),
            (710, f"{result['total_filament_g']:.2f} g"),
            (990, f"{result['support']['support_g']:.2f} g"),
        ):
            text(d, (x, y), value, 28, ACCENT if x > 55 else FG, x == 55)
    text(
        d,
        (55, 595),
        "Download sources, hashes, exact commands, logs and quality reports are retained.",
        22,
        MUTED,
    )
    frames.append((im, 6))
    frames.append(limitations)
    directory = RUN / "media"
    directory.mkdir(exist_ok=True)
    concat = []
    for i, (im, seconds) in enumerate(frames):
        im.save(directory / f"{i:02d}.png")
        concat.extend((f"file '{i:02d}.png'", f"duration {seconds}"))
    concat.append(f"file '{len(frames) - 1:02d}.png'")
    (directory / "frames.txt").write_text("\n".join(concat) + "\n", encoding="utf-8")
    gif = OUT / "complex-models.gif"
    frames[0][0].save(
        gif,
        save_all=True,
        append_images=[im for im, _ in frames[1:]],
        duration=[s * 1000 for _, s in frames],
        loop=0,
        optimize=True,
    )
    frames[2][0].save(OUT / "haku-comparison.png")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to finish MP4 output.")
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            "frames.txt",
            "-vf",
            "fps=24",
            "-t",
            str(sum(seconds for _, seconds in frames)),
            "-c:v",
            "libx264",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(OUT / "complex-models.mp4"),
        ],
        cwd=directory,
        check=True,
        capture_output=True,
    )
    print(f"GIF and MP4 written to {OUT}")


if __name__ == "__main__":
    main()
