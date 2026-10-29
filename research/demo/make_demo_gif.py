"""Assemble the README demo (GIF + MP4) from a recorded CLI session.

Every terminal frame shows the real output of a command run on the
maintainer's machine, saved to text files in the demo folder; lines are only
dropped (marked with "..."), never edited. Images are the CLI's own renders
and a window capture of Snapmaker Orca with the exported project.

Recording (from the repository root, demo folder `runs/demo`):

    python tests/synthetic_bust.py runs/demo/bust.stl
    python research/demo/make_demo_bust.py runs/demo/bust-2c.3mf
    print-copilot printer fake --port 7125          # simulated U1, other window
    set PRINT_COPILOT_PRINTER_URL=http://127.0.0.1:7125
    cd runs/demo
    print-copilot orient tee.stl --top 2 --support none --support tree > 03-orient-tee.txt
    print-copilot render bust.stl --out bust-views.png
    print-copilot project bust-2c.3mf > 04-project.txt
    print-copilot strategies bust-2c.3mf --plate 1 > 05-strategies.txt
    print-copilot export-project bust-2c.3mf --plate 1 --out bust-u1.3mf --open > 06-export.txt
    (capture the Snapmaker Orca window as orca-prepare.png)
    print-copilot printer upload bust-u1.gcode > 08-upload.txt
    print-copilot printer start bust-u1.gcode --dry-run > 09-start.txt

Then: `python research/demo/make_demo_gif.py runs/demo docs/images`; it also
writes the forum cover image to `runs/demo/cover.png`
(needs Pillow and ffmpeg; Pillow is not a project dependency).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
FPS = 10
BG = (24, 26, 31)
FG = (220, 222, 228)
DIM = (130, 135, 145)
PROMPT = (110, 200, 150)
ACCENT = (240, 120, 60)
BAR = (40, 43, 51)
FONT = "C:/Windows/Fonts/consola.ttf"
FONT_BOLD = "C:/Windows/Fonts/consolab.ttf"
UI = "C:/Windows/Fonts/segoeui.ttf"
UI_BOLD = "C:/Windows/Fonts/segoeuib.ttf"
REPO = "github.com/antonio94770/Snapmaker-U1-Print-Copilot"
SIMULATED = "printer: simulated U1 (print-copilot printer fake), same API as the real one"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


MONO = font(FONT, 17)
MONO_B = font(FONT_BOLD, 17)
LINE_H = 22


def pick(path: Path, keep) -> list[str]:
    """Lines of a recorded output for which `keep(line)` is true; a run of
    dropped lines becomes one '...' line."""
    out: list[str] = []
    dropped = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if keep(line):
            if dropped and out:
                out.append("  ...")
            out.append(line.rstrip())
            dropped = False
        else:
            dropped = True
    return out


def caption_bar(img: Image.Image, title: str, note: str = "") -> None:
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 44], fill=BAR)
    d.text((18, 8), title, font=font(UI_BOLD, 20), fill=FG)
    if note:
        d.text((18, H - 12), note, font=font(UI, 16), fill=DIM, anchor="ld")


def terminal(title: str, command: str, lines: list[str], typed: int, note: str) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    caption_bar(img, title, note)
    d = ImageDraw.Draw(img)
    y = 60
    d.text((18, y), ">", font=MONO_B, fill=PROMPT)
    d.text((38, y), command[:typed], font=MONO_B, fill=FG)
    y += LINE_H + 6
    for line in lines:
        colour = ACCENT if "<- recommended" in line or line.startswith("fastest") else FG
        if line.strip() == "...":
            colour = DIM
        rest = line
        while True:
            if y > H - 2 * LINE_H:
                raise ValueError(f"{title!r}: output does not fit the frame")
            cut = len(rest) if len(rest) <= 118 else rest.rfind(" ", 0, 118)
            d.text((18, y), rest[:cut], font=MONO, fill=colour)
            y += LINE_H
            rest = "      " + rest[cut:].lstrip()
            if not rest.strip():
                break
    return img


def picture(title: str, images: list[Path], note: str = "", crop=None) -> Image.Image:
    img = Image.new("RGB", (W, H), (245, 245, 242))
    tiles = []
    for p in images:
        im = Image.open(p).convert("RGB")
        if crop:
            im = im.crop(crop)
        tiles.append(im)
    area_w, area_h = (W - 20 * (len(tiles) + 1)) // len(tiles), H - 64
    x = 20
    for im in tiles:
        im.thumbnail((area_w, area_h), Image.LANCZOS)
        img.paste(im, (x + (area_w - im.width) // 2, 54 + (area_h - im.height) // 2))
        x += area_w + 20
    caption_bar(img, title, note)
    return img


def card(lines: list[tuple[str, int, tuple[int, int, int]]]) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    total = sum(size + 18 for _, size, _ in lines)
    y = (H - total) // 2
    for text, size, colour in lines:
        f = font(UI_BOLD if size >= 30 else UI, size)
        d.text((W // 2, y), text, font=f, fill=colour, anchor="ma")
        y += size + 18
    return img


def scenes(demo: Path, docs: Path):
    def no_conf_notes(line: str) -> bool:
        return not line.startswith("note: ") and "Work dir" not in line and "model file" not in line

    yield (
        "card",
        card(
            [
                ("Snapmaker Print Copilot", 46, FG),
                (
                    "Prepare Snapmaker U1 prints with any AI agent, or from the command line",
                    24,
                    DIM,
                ),
                ("Snapmaker Orca slices; every number shown is the slicer's own", 22, ACCENT),
            ]
        ),
        3.0,
    )

    orient = pick(demo / "03-orient-tee.txt", lambda s: not s.startswith("note: "))
    first = next(i for i, s in enumerate(orient) if s.startswith("Sliced with"))
    orient = ["  ...  (heuristic ranking of the candidate orientations)"] + [
        s for s in orient[first:] if no_conf_notes(s) and not s.startswith("Recommendation rule")
    ]
    yield (
        "term",
        (
            "1. Which way up? Orientations sliced with and without supports",
            "print-copilot orient tee.stl --top 2 --support none --support tree",
            orient,
            "",
        ),
        4.5,
    )
    yield (
        "pic",
        picture(
            "Renders the agent looks at: red = needs support",
            [docs / "tee-as-loaded.png", docs / "tee-y-down.png"],
            "as loaded vs lying on its side",
        ),
        3.0,
    )

    conf_notes = ("note: printer", "note: process", "note: filaments")
    project = pick(demo / "04-project.txt", lambda s: not s.startswith(conf_notes))
    yield (
        "term",
        (
            "2. A downloaded two-colour project, checked against the loaded spools",
            "print-copilot project bust-2c.3mf",
            project,
            SIMULATED,
        ),
        4.5,
    )
    yield "pic", picture("The model as the agent sees it", [demo / "bust-views.png"]), 2.5

    strat = pick(demo / "05-strategies.txt", lambda s: no_conf_notes(s) or "prime tower each" in s)
    yield (
        "term",
        (
            "3. U1 tool changes and prime tower, measured from the G-code",
            "print-copilot strategies bust-2c.3mf --plate 1",
            strat,
            "",
        ),
        5.0,
    )

    export = pick(demo / "06-export.txt", lambda s: no_conf_notes(s) and "Plate 1 objects" not in s)
    yield (
        "term",
        (
            "4. Write the project, slice that same file, open it in Snapmaker Orca",
            "print-copilot export-project bust-2c.3mf --plate 1 --out bust-u1.3mf --open",
            [s.replace(str(demo.resolve()) + "\\", "") for s in export],
            "",
        ),
        4.5,
    )
    yield (
        "pic",
        picture(
            "Opened in Snapmaker Orca: U1 preset, filament 1 white, 2 black",
            [demo / "orca-prepare.png"],
        ),
        3.0,
    )
    yield (
        "pic",
        picture(
            "Opened in Snapmaker Orca (detail)",
            [demo / "orca-prepare.png"],
            crop=(1000, 330, 2150, 980),
        ),
        2.5,
    )

    start = [*pick(demo / "08-upload.txt", all_lines), "", *pick(demo / "09-start.txt", all_lines)]
    yield (
        "term",
        (
            "5. Upload, then a start plan with per-head RFID checks",
            "print-copilot printer upload bust-u1.gcode"
            "  &&  print-copilot printer start bust-u1.gcode --dry-run",
            start,
            SIMULATED,
        ),
        5.0,
    )

    yield (
        "card",
        card(
            [
                ("24 MCP tools for Claude Code, Codex, Claude Desktop, Cursor...", 26, FG),
                ("and the same features as print-copilot commands", 24, DIM),
                (REPO, 24, ACCENT),
                (
                    "GPL-3.0-or-later  ·  Windows  ·  Snapmaker Orca 2.4.1  ·  U1 firmware 2.0",
                    18,
                    DIM,
                ),
            ]
        ),
        3.5,
    )


def cover(demo: Path) -> Image.Image:
    """Forum cover: measured strategy table next to the project in Snapmaker Orca."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((40, 36), "Snapmaker Print Copilot", font=font(UI_BOLD, 44), fill=FG)
    d.text(
        (40, 96),
        "Prepare Snapmaker U1 prints with any AI agent, or from the command line",
        font=font(UI, 24),
        fill=DIM,
    )
    rows = [
        s
        for s in pick(demo / "05-strategies.txt", all_lines)
        if s.startswith(("strategy", "presets as is", "no prime tower"))
    ]
    y = 160
    d.text((40, y), "> print-copilot strategies bust-2c.3mf", font=MONO_B, fill=PROMPT)
    y += 32
    for row in rows:
        d.text((40, y), row, font=MONO, fill=FG)
        y += 24
    lines = [
        "Two colours on the",
        "loaded RFID spools,",
        "tool changes and",
        "prime tower measured",
        "from the G-code.",
    ]
    for i, text in enumerate(lines):
        d.text((40, 330 + 34 * i), text, font=font(UI, 24), fill=FG)
    orca = Image.open(demo / "orca-prepare.png").convert("RGB").crop((1000, 330, 2150, 980))
    orca.thumbnail((700, 340), Image.LANCZOS)
    img.paste(orca, (W - orca.width - 40, 300))
    d.text(
        (W - 40, 300 + orca.height + 8),
        "exported project in Snapmaker Orca",
        font=font(UI, 16),
        fill=DIM,
        anchor="ra",
    )
    d.text((40, H - 40), REPO, font=font(UI, 22), fill=ACCENT)
    return img


def all_lines(line: str) -> bool:
    return True


def main(demo: Path, out_dir: Path) -> None:
    cover(demo).save(demo / "cover.png")
    frames = demo / "frames"
    frames.mkdir(exist_ok=True)
    for old in frames.glob("*.png"):
        old.unlink()
    n = 0

    def emit(img: Image.Image, seconds: float) -> None:
        nonlocal n
        for _ in range(max(1, round(seconds * FPS))):
            img.save(frames / f"{n:05d}.png")
            n += 1

    for kind, payload, hold in scenes(demo, out_dir):
        if kind == "term":
            title, command, lines, note = payload
            for typed in range(0, len(command) + 1, 4):
                emit(terminal(title, command, [], typed, note), 1 / FPS)
            emit(terminal(title, command, [], len(command), note), 0.4)
            emit(terminal(title, command, lines, len(command), note), hold)
        else:
            emit(payload, hold)

    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    src = ["-framerate", str(FPS), "-i", str(frames / "%05d.png")]
    mp4 = out_dir / "demo.mp4"
    gif = out_dir / "demo.gif"
    run = [ffmpeg, "-y", "-loglevel", "error", *src]
    subprocess.run(
        [*run, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(mp4)], check=True
    )
    vf = (
        "scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];"
        "[b][p]paletteuse=dither=none:diff_mode=rectangle"
    )
    subprocess.run([*run, "-vf", vf, str(gif)], check=True)
    sizes = f"{mp4.stat().st_size // 1024} KiB, {gif.stat().st_size // 1024} KiB"
    print(f"{n} frames, {n / FPS:.0f} s -> {mp4}, {gif} ({sizes})")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
