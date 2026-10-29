"""Measure housing orientations with identical U1 process and material settings."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from print_copilot.model.mesh import load_mesh, write_stl

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "runs" / f"planetarium-optimization-{datetime.now():%Y%m%d-%H%M%S}"
OUT.mkdir(parents=True, exist_ok=False)
SOURCE = ROOT / "research/showcase/models/planetarium/Base/base_housing.stl"
COMMANDS = []


def cli(name, *args):
    command = [sys.executable, "-m", "print_copilot.cli", *map(str, args)]
    COMMANDS.append(command)
    (OUT / "commands.json").write_text(json.dumps(COMMANDS, indent=2), encoding="utf-8")
    print(f"Running {name}", flush=True)
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=900
    )
    (OUT / f"{name}.txt").write_text(result.stdout, encoding="utf-8")
    (OUT / f"{name}.stderr.txt").write_text(result.stderr, encoding="utf-8")
    if result.returncode and args[0] != "quality":
        raise RuntimeError(f"{name}: {result.stderr[-2000:]}")
    data = json.loads(result.stdout)
    (OUT / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


mesh = load_mesh(SOURCE)
rotated = mesh.transformed(np.diag([1, -1, -1])).placed_on_bed()
flipped = write_stl(rotated, OUT / "housing-flipped.stl")
if mesh.face_count != rotated.face_count or not np.isclose(mesh.volume_mm3, rotated.volume_mm3):
    raise ValueError("Rotation changed the geometry.")
for orientation, model in (("loaded", SOURCE), ("flipped", flipped)):
    cli(
        f"{orientation}-render",
        "render",
        model,
        "--view",
        "iso",
        "--view",
        "bottom",
        "--out",
        OUT / f"{orientation}.png",
        "--json",
    )
    for support in (False, True):
        name = f"{orientation}-{'tree' if support else 'none'}"
        result = cli(
            name,
            "slice-project",
            model,
            "--head",
            "T0",
            "--no-printer",
            "--set",
            "wall_loops=3",
            "--set",
            f"enable_support={int(support)}",
            "--set",
            "support_type=tree(auto)",
            "--set",
            "support_on_build_plate_only=1",
            "--work-dir",
            OUT / "slices",
            "--json",
        )
        cli(
            f"{name}-quality",
            "quality",
            result["gcode_path"],
            "--max-segments",
            "6000000",
            "--work-dir",
            OUT / "quality",
            "--json",
        )
print(f"Evidence: {OUT}", flush=True)
