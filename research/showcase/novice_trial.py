"""Replay a novice brief through existing product commands without an orientation hint.

This is an observed, known-case workflow trial, not a blind or independent
evaluation. It does not change product code, presets, or printer state.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from print_copilot.actions import SUPPORT_MODES
from print_copilot.model.mesh import load_mesh


@dataclass
class Protocol:
    brief: str
    purpose_inference: str
    head_authorization: str
    printer_state: str
    known_case: bool
    production_code_changes_allowed: bool
    criteria: list[str]


ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(sys.argv[1]).resolve()
OUT = ROOT / "runs" / f"novice-trial-{datetime.now():%Y%m%d-%H%M%S}"
OUT.mkdir(parents=True, exist_ok=False)
COMMANDS = []


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(name, data):
    path = OUT / name
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def cli(name, *args):
    command = [sys.executable, "-m", "print_copilot.cli", *map(str, args)]
    COMMANDS.append(command)
    save("commands.json", COMMANDS)
    print(name, flush=True)
    run = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=1200
    )
    (OUT / f"{name}.stdout.txt").write_text(run.stdout, encoding="utf-8")
    (OUT / f"{name}.stderr.txt").write_text(run.stderr, encoding="utf-8")
    if run.returncode and args[0] != "quality":
        raise RuntimeError(f"{name} failed: {run.stderr[-2000:]}")
    result = json.loads(run.stdout)
    save(f"{name}.json", result)
    if run.returncode:
        raise RuntimeError(f"{name} returned quality status {result.get('status')}; stopping.")
    return result


def main():
    protocol = Protocol(
        brief="I need this component to assemble the mechanism.",
        purpose_inference="functional: the stated purpose is mechanical assembly, not display",
        head_authorization="User already chose T0 white PLA in this conversation.",
        printer_state="Read-only discovery returned no U1; offline preparation only.",
        known_case=True,
        production_code_changes_allowed=False,
        criteria=[
            "No user orientation, support-type or numeric process-setting answers required.",
            "Orientation comes from the existing product recommendation, not a supplied rotation.",
            "Recommended pose fits the U1 and is geometrically stable by the labelled heuristic.",
            "Triangle count, volume and geometry are preserved by the selected rigid rotation.",
            "Functional setting proposals come from the existing suggest command.",
            "The exported G-code uses the chosen head and proposed effective settings.",
            "The selected and exported slices have no error-level quality findings.",
            "A new 3MF/G-code pair is delivered; no input file or user preset is overwritten.",
            "Opening in Orca, independent review and hardware outcomes are recorded separately.",
        ],
    )
    protocol_path = save("protocol.json", asdict(protocol))
    protocol_hash = digest(protocol_path)
    files = sorted((ROOT / "src/print_copilot").rglob("*.py"))
    code_before = {str(p.relative_to(ROOT)): digest(p) for p in files}
    source_before = digest(SOURCE)
    incoming = OUT / "input.stl"
    incoming.write_bytes(SOURCE.read_bytes())
    save(
        "input.json",
        {
            "source": str(SOURCE),
            "sha256": source_before,
            "brief": protocol.brief,
            "production_code_sha256": code_before,
        },
    )
    setup = cli("01-setup", "setup", "--json")
    if setup["machine"] != "Snapmaker U1 (0.4 nozzle)":
        raise RuntimeError("Unexpected selected printer; refusing a silent printer change.")
    # The goal is derived from the brief. No model-specific pose is supplied.
    orientation = cli(
        "02-orientation",
        "orient",
        incoming,
        "--goal",
        "quality",
        "--top",
        "2",
        "--support",
        "tree",
        "--support",
        "none",
        "--work-dir",
        OUT / "orientation",
        "--json",
    )
    chosen = orientation["recommended"]
    pose = next(
        c for c in orientation["analysis"]["candidates"] if c["label"] == chosen["candidate"]
    )
    if pose["fits_bed"] is not True or pose["stable"] is not True:
        raise RuntimeError("Product recommendation is not a stable pose fitting the bed.")
    oriented = Path(chosen["model_path"])
    before, after = load_mesh(incoming), load_mesh(oriented)
    if before.face_count != after.face_count or not np.isclose(
        before.volume_mm3, after.volume_mm3, rtol=1e-5
    ):
        raise RuntimeError("Recommended orientation did not preserve mesh geometry.")
    expected = before.transformed(np.array(pose["rotation_matrix"])).placed_on_bed()
    if not np.allclose(expected.triangles, after.triangles, rtol=1e-6, atol=0.0001):
        raise RuntimeError("Recommended orientation changed model geometry.")
    suggestion = cli(
        "03-functional-settings", "suggest", oriented, "--purpose", "functional", "--json"
    )
    overrides = {
        **SUPPORT_MODES[chosen["support_mode"]],
        **{p["key"]: p["proposed"] for p in suggestion["proposals"]},
    }
    variants = save(
        "functional-variant.json", [{"label": "Functional candidate", "overrides": overrides}]
    )
    review = cli(
        "04-candidate-review",
        "quality",
        oriented,
        "--head",
        "T0",
        "--no-printer",
        "--variants-file",
        variants,
        "--max-segments",
        "6000000",
        "--work-dir",
        OUT / "quality",
        "--json",
    )
    selected = next(v for v in review["variants"] if v["label"] == "Functional candidate")
    if selected["error"] or any(f["severity"] == "error" for f in selected["audit"]["findings"]):
        raise RuntimeError("Functional candidate failed quality checks.")
    export_args = []
    for key, value in overrides.items():
        export_args += ["--set", f"{key}={int(value) if isinstance(value, bool) else value}"]
    exports = OUT / "exports"
    exports.mkdir()
    exported = cli(
        "05-export",
        "export-project",
        oriented,
        "--head",
        "T0",
        "--no-printer",
        *export_args,
        "--out",
        exports / "functional-component.3mf",
        "--work-dir",
        OUT / "export-slices",
        "--json",
    )
    final_review = cli(
        "06-export-review",
        "quality",
        exported["gcode_path"],
        "--max-segments",
        "6000000",
        "--work-dir",
        OUT / "quality",
        "--json",
    )
    cli(
        "07-model-views",
        "render",
        oriented,
        "--view",
        "iso",
        "--view",
        "bottom",
        "--out",
        OUT / "chosen-orientation.png",
        "--json",
    )
    if digest(SOURCE) != source_before or digest(protocol_path) != protocol_hash:
        raise RuntimeError("Input or predeclared protocol changed during the trial.")
    if any(digest(p) != code_before[str(p.relative_to(ROOT))] for p in files):
        raise RuntimeError("Product code changed during the trial.")
    result = {
        "software_flow": "completed",
        "known_case_not_blind": True,
        "orientation_supplied_by_user": False,
        "additional_user_answers": 0,
        "orientation": chosen,
        "functional_proposals": suggestion["proposals"],
        "applied_overrides": overrides,
        "export": exported,
        "export_quality": final_review,
        "protocol_sha256": protocol_hash,
        "production_code_unchanged": True,
        "source_unchanged": True,
        "orca_opening": "pending direct UI observation",
        "independent_expert_review": "not performed",
        "physical_print_and_assembly": "not performed",
        "printer_state": protocol.printer_state,
    }
    save("result.json", result)
    print(f"Trial evidence: {OUT}", flush=True)


if __name__ == "__main__":
    main()
