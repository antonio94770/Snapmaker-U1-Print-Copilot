"""Assemble a measured before/after demo, with explicit reference and limitations.

Usage: python make_optimization_media.py HAKU_RUN HOUSING_RUN
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from make_media import ACCENT, MUTED, OUT, RUN, duration, metric_line, picture, slide, text

HAKU = RUN
HOUSING = Path(sys.argv[2]).resolve()


def read(folder, name):
    return json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))


variants = read(HAKU, "haku-comparison")["variants"]
baseline, candidate = [v["slice"] for v in variants[:2]]
before, after = [read(HOUSING, n) for n in ("loaded-tree", "flipped-tree")]
frames = []
im, d = slide(
    "Haku: less tower material, same layer height",
    "Saved process restored on U1 | 0.20 mm | same speeds, walls, infill and tree supports",
)
for x, title, result in (
    (50, "Reference: prime volume 45 mm3", baseline),
    (665, "Candidate: U1 default 36 mm3", candidate),
):
    text(d, (x, 190), title, 27, bold=True)
    metric_line(d, x, 260, "Slicer time prediction", duration(result["estimated_time_s"]))
    metric_line(d, x, 370, "Total filament", f"{result['total_filament_g']:.2f} g")
    metric_line(d, x + 300, 370, "Prime tower", f"{result['prime_tower_g']:.2f} g")
metric_line(d, 50, 505, "Measured reduction", "20.80 g / 39m 35s")
text(d, (665, 515), "10.7% less total filament", 30, ACCENT, True)
text(
    d,
    (50, 625),
    "Slicer prediction; priming/colour quality still needs a physical test.",
    23,
    MUTED,
)
frames.append((im, 7))
im, d = slide(
    "Verify where the saving comes from",
    "Effective G-code settings differ only in prime_volume: 45 -> 36 mm3",
)
for index, box in enumerate(((35, 185, 625, 550), (655, 185, 1245, 550))):
    q = read(HAKU, f"quality-{index}")
    picture(im, q["variants"][0]["preview"]["png_path"], box)
text(d, (50, 560), "All support paths and outer-wall paths match.", 26, ACCENT, True)
text(d, (50, 605), "Only model-path difference: one 0.024 mm internal infill segment.", 24, MUTED)
text(
    d,
    (50, 640),
    "The existing layer-count disagreement still blocks full quality acceptance.",
    20,
    MUTED,
)
frames.append((im, 7))
im, d = slide(
    "Planetarium housing: turn the roof into the base",
    "Same geometry and process | 0.16 mm / 3 walls / T0 white / automatic supports ON in both",
)
picture(im, HOUSING / "loaded.png", (35, 180, 625, 430))
picture(im, HOUSING / "flipped.png", (655, 180, 1245, 430))
for x, title, result in ((50, "As loaded", before), (665, "Rotated 180 degrees", after)):
    text(d, (x, 440), title, 28, bold=True)
    metric_line(d, x, 490, "Slicer time", duration(result["estimated_time_s"]))
    metric_line(d, x + 300, 490, "Filament", f"{result['total_filament_g']:.2f} g")
text(
    d,
    (50, 610),
    "58.30 g of generated supports -> 0.00 g | saves 57.58 g total and 7h 28m 11s",
    24,
    ACCENT,
)
frames.append((im, 8))
im, d = slide(
    "Keep the comparison honest",
    "The large housing reduction is against the supported orientation, not the unsupported STL",
)
text(d, (50, 205), "As loaded, supports OFF", 29, bold=True)
text(d, (50, 250), "3h 03m 40s / 101.84 g; large bridging and overhang regions", 27)
text(d, (50, 325), "Flipped, supports OFF", 29, bold=True)
text(d, (50, 370), "3h 01m 11s / 102.56 g; fewer bridge paths, no overhang flag", 27)
text(d, (50, 460), "Against this reference: 2m 29s faster, 0.72 g more filament.", 27, ACCENT, True)
text(
    d, (50, 530), "The orientation improves support requirements; it is not a 36% saving", 25, MUTED
)
text(
    d,
    (50, 570),
    "against an already unsupported slice. Fit and adhesion remain untested.",
    25,
    MUTED,
)
frames.append((im, 7))
im, d = slide(
    "Review candidates exported as 3MF + G-code",
    "No print started | Source files and user presets preserved | Exact commands and logs retained",
)
text(d, (50, 215), "Haku: keep the tower; use the U1 preset prime volume", 30, ACCENT, True)
text(d, (50, 275), "Normal supports used more material; no-tower stays experimental.", 25)
text(d, (50, 365), "Housing: rotate it and leave automatic supports enabled", 30, ACCENT, True)
text(d, (50, 425), "The slicer generates no supports in this position.", 25)
text(d, (50, 520), "Haku: Lynns / CC BY-NC-ND 4.0", 22, MUTED)
text(d, (50, 560), "Planetarium: HackerJack42, original Zippitybamba / CC BY-NC-SA 4.0", 22, MUTED)
text(d, (50, 620), "Full evidence and limitations: docs/optimization-results.md", 22, MUTED)
frames.append((im, 6))
directory = HAKU / "optimization-media"
directory.mkdir(exist_ok=True)
concat = []
for i, (frame, seconds) in enumerate(frames):
    frame.save(directory / f"{i:02d}.png")
    concat.extend((f"file '{i:02d}.png'", f"duration {seconds}"))
concat.append(f"file '{len(frames) - 1:02d}.png'")
(directory / "frames.txt").write_text("\n".join(concat) + "\n", encoding="utf-8")
frames[0][0].save(
    OUT / "optimization.gif",
    save_all=True,
    append_images=[im for im, _ in frames[1:]],
    duration=[s * 1000 for _, s in frames],
    loop=0,
    optimize=True,
)
frames[0][0].save(OUT / "optimization-comparison.png")
subprocess.run(
    [
        shutil.which("ffmpeg"),
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
        str(sum(s for _, s in frames)),
        "-c:v",
        "libx264",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(OUT / "optimization.mp4"),
    ],
    cwd=directory,
    check=True,
    capture_output=True,
)
print(f"Optimization media written: {sum(s for _, s in frames)} seconds")
