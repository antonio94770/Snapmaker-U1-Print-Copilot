"""Record real CLI experiments; downloaded models are never executed or published.

Run with the repository's Python environment from the repository root.
Inputs must already be in research/showcase/models. Output is a fresh runs folder.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from print_copilot.model.mesh import load_mesh

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "research/showcase/models"
OUT = (
    Path(sys.argv[1]).resolve()
    if len(sys.argv) > 1
    else ROOT / "runs" / f"complex-showcase-{datetime.now():%Y%m%d-%H%M%S}"
)
OUT.mkdir(parents=True, exist_ok=True)
COMMANDS = (
    json.loads((OUT / "commands.json").read_text(encoding="utf-8"))
    if (OUT / "commands.json").exists()
    else []
)


def save(name, value):
    path = OUT / name
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return path


def cli(name, *args):
    recorded = OUT / f"{name}.json"
    if recorded.exists():
        return json.loads(recorded.read_text(encoding="utf-8"))
    command = [sys.executable, "-m", "print_copilot.cli", *map(str, args)]
    COMMANDS.append(command)
    save("commands.json", COMMANDS)
    print(f"Running {name}", flush=True)
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    (OUT / f"{name}.txt").write_text(result.stdout, encoding="utf-8")
    (OUT / f"{name}.stderr.txt").write_text(result.stderr, encoding="utf-8")
    # Quality findings are evidence, including a blocked report. Preserve and
    # surface them instead of silently turning the report into a successful run.
    if result.returncode and args[0] != "quality":
        raise RuntimeError(f"{name}: exit {result.returncode}: {result.stderr[-2000:]}")
    data = json.loads(result.stdout)
    save(f"{name}.json", data)
    if result.returncode:
        print(f"{name}: quality status {data.get('status')}; see retained report", flush=True)
    return data


def main():
    inventory = []
    for path in sorted((MODELS / "planetarium").rglob("*.stl")):
        mesh = load_mesh(path)
        inventory.append(
            {
                "path": str(path.relative_to(MODELS)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "triangles": mesh.face_count,
            }
        )
    save("planetarium-inventory.json", inventory)
    haku = MODELS / "Haku_Dragon_Diorama_(Spirited_Away)_SM.3mf"
    save(
        "inputs.json",
        {
            "haku_sha256": hashlib.sha256(haku.read_bytes()).hexdigest(),
            "planetarium_stl_count": len(inventory),
        },
    )
    cli("setup", "setup", "--json")
    cli("haku-inspect", "project", haku, "--no-printer", "--json")
    cli(
        "haku-render",
        "render",
        haku,
        "--view",
        "front",
        "--view",
        "iso",
        "--out",
        OUT / "haku.png",
        "--json",
    )
    common = {
        "initial_layer_print_height": 0.25,
        "top_shell_layers": 5,
        "bottom_shell_layers": 3,
        "sparse_infill_pattern": "grid",
        "enable_support": True,
        "support_on_build_plate_only": True,
        "support_type": "tree(auto)",
        "support_threshold_angle": "30",
        "enable_prime_tower": True,
    }
    variants = [
        {
            "label": f"Haku {height:.2f} mm tree supports",
            "overrides": {**common, "layer_height": height},
        }
        for height in (0.20, 0.16)
    ]
    variants_file = save("haku-variants.json", variants)
    haku_result = cli(
        "haku-comparison",
        "strategies",
        haku,
        "--plate",
        "1",
        "--no-printer",
        "--map",
        "1=T0",
        "--map",
        "2=T1",
        "--map",
        "3=T2",
        "--map",
        "4=T3",
        "--variants-file",
        variants_file,
        "--work-dir",
        OUT / "slices",
        "--json",
    )
    for index, variant in enumerate(haku_result["variants"]):
        if variant["error"]:
            raise RuntimeError(variant["error"])
        cli(
            f"haku-quality-{index}",
            "quality",
            variant["slice"]["gcode_path"],
            "--max-segments",
            "6000000",
            "--work-dir",
            OUT / "quality",
            "--json",
        )
    parts = {
        "herringbone": "Base/base_drivegear_herringbone.stl",
        "housing": "Base/base_housing.stl",
        "calendar": "Earth & Moon/calendar_gear.stl",
        "axle": "Base/main_axle.stl",
    }
    for name, relative in parts.items():
        model = MODELS / "planetarium" / relative
        cli(
            f"planetarium-{name}-render",
            "render",
            model,
            "--view",
            "iso",
            "--view",
            "top",
            "--out",
            OUT / f"planetarium-{name}.png",
            "--json",
        )
        result = cli(
            f"planetarium-{name}",
            "slice-project",
            model,
            "--head",
            "T0",
            "--no-printer",
            "--set",
            "wall_loops=3",
            "--work-dir",
            OUT / "slices",
            "--json",
        )
        cli(
            f"planetarium-{name}-quality",
            "quality",
            result["gcode_path"],
            "--max-segments",
            "6000000",
            "--work-dir",
            OUT / "quality",
            "--json",
        )
    print(f"Evidence: {OUT}", flush=True)


if __name__ == "__main__":
    main()
