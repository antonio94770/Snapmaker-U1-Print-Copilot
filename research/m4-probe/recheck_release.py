"""Recheck historical CLI issues against an installed release without modifying it.

Usage: python research/m4-probe/recheck_release.py <fresh work directory>
Copies bundled vendor presets into the work directory. Each probe keeps its
command, log, input settings and output. No printer communication is performed.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from print_copilot.actions import select_presets
from print_copilot.gcode.header import parse_gcode, read_config_block
from print_copilot.orca.install import locate_exe
from print_copilot.orca.presets import PresetLibrary
from print_copilot.orca.project import _plate_xml, read_project, write_slicing_copy
from print_copilot.orca.slicer import prepare_cli_settings

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from synthetic_project import build_project


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    exe = locate_exe()
    data = root / "data"
    for vendor in ("Snapmaker", "OrcaFilamentLibrary"):
        shutil.copytree(
            exe.parent / "resources" / "profiles" / vendor, data / "system" / vendor
        )
    library = PresetLibrary(data)
    selection = select_presets(
        library,
        printer="Snapmaker U1 (0.4 nozzle)",
        process="0.20mm High Quality @Snapmaker U1 (0.4 nozzle)",
        filaments=["Generic PLA"],
    )
    machine, process, filaments = prepare_cli_settings(
        selection.machine, selection.process, selection.filaments
    )
    for f, c in zip(filaments, ["#FFFFFF", "#000000", "#F4E2C1", "#ED1C24"]):
        f["filament_colour"] = [c]
    src = build_project(root / "source.3mf")
    info = read_project(src)
    safe = root / "safe.3mf"
    write_slicing_copy(info, 1, {1: 4, 2: 3, 3: 2, 4: 1}, safe, bed_mm=(270, 270, 270))
    cube = Path(__file__).resolve().parents[1] / "cli-probe" / "cube.stl"
    results = []

    def rewrite(name, application=None, plate=False, settings=False):
        dst = root / f"{name}.3mf"
        with zipfile.ZipFile(safe) as zin, zipfile.ZipFile(dst, "x") as zout:
            for item in zin.infolist():
                content = zin.read(item.filename)
                if item.filename == "3D/3dmodel.model" and application:
                    import re

                    match = re.search(rb"<model\b[^>]*>", content)
                    at = match.end()
                    content = (
                        content[:at]
                        + f'<metadata name="Application">{application}</metadata>'.encode()
                        + content[at:]
                    )
                if item.filename == "Metadata/model_settings.config" and plate:
                    content = content.replace(
                        b"</config>", _plate_xml(["10", "11"]) + b"</config>"
                    )
                zout.writestr(item, content)
            if settings:
                from print_copilot.orca.project import project_settings

                cfg = project_settings(
                    machine,
                    process,
                    filaments,
                    colours=["#FFFFFF", "#000000", "#F4E2C1", "#ED1C24"],
                    machine_name=selection.machine.name,
                    process_name=selection.process.name,
                    filament_names=[f.name for f in selection.filaments],
                )
                zout.writestr("Metadata/project_settings.config", json.dumps(cfg))
        return dst

    def probe(
        name,
        model=cube,
        change=None,
        extra=(),
        raw=False,
        path_style=False,
        output_length=None,
    ):
        work = root / name
        work.mkdir()
        m, p, fs = copy.deepcopy((machine, process, filaments))
        if change:
            change(m, p, fs)
        paths = []
        for label, value in [
            ("machine", m),
            ("process", p),
            *[(f"filament_{i}", f) for i, f in enumerate(fs)],
        ]:
            path = work / f"{label}.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            paths.append(path.as_posix())
        out = work / "out"
        if output_length:
            out = work / ("x" * (output_length - len(str(work)) - 1))
        out.mkdir()
        mp, pp, *fps = paths
        if raw:
            mp, pp = (
                selection.machine.ref.path.as_posix(),
                selection.process.ref.path.as_posix(),
            )
            fps = [f.ref.path.as_posix() for f in selection.filaments]
        if path_style:
            mp = "/" + mp[0].lower() + mp[2:]
        command = [
            str(exe),
            "--slice",
            "0",
            "--load-settings",
            mp + ";" + pp,
            "--load-filaments",
            ";".join(fps),
            "--outputdir",
            out.as_posix(),
            *extra,
            model.as_posix(),
        ]
        (work / "command.json").write_text(json.dumps(command, indent=2))
        row = {"probe": name, "output_dir_chars": len(str(out))}
        try:
            with (work / "slicer.log").open("wb") as log:
                run = subprocess.run(
                    command, stdout=log, stderr=subprocess.STDOUT, timeout=45
                )
            row["exit_code"] = run.returncode
            gcodes = list(out.glob("*.gcode"))
            row["gcode_count"] = len(gcodes)
            row["exports"] = [p.name for p in out.glob("*.3mf")]
            if gcodes:
                measured = parse_gcode(gcodes[0])
                from print_copilot.gcode.toolchanges import tool_changes

                row["tool_changes"] = tool_changes(gcodes[0]).tool_changes
                row.update(
                    generator=measured.generator,
                    time_s=measured.estimated_time_s,
                    filament_g=measured.total_filament_g,
                    filament_per_head_g=measured.filament_used_g,
                )
                cfg = read_config_block(gcodes[0])
                row["config"] = {
                    k: cfg.get(k)
                    for k in [
                        "enable_prime_tower",
                        "support_type",
                        "wipe_tower_filament",
                        "filament_colour",
                        "flush_volumes_matrix",
                        "prime_volume",
                    ]
                }
        except Exception as exc:
            row["error"] = str(exc)
        results.append(row)
        (root / "summary.json").write_text(json.dumps(results, indent=2))
        print(json.dumps(row), flush=True)

    help_dir = root / "help"
    help_dir.mkdir()
    command = [str(exe), "--help"]
    (help_dir / "command.json").write_text(json.dumps(command))
    with (help_dir / "slicer.log").open("wb") as log:
        run = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=45)
    print("help exit", run.returncode, flush=True)
    bottom = root / "painted-bottom.3mf"
    write_slicing_copy(info, 2, {4: 1, 5: 2}, bottom, bed_mm=(270, 270, 270))
    probe("painted-bottom-colours", bottom, extra=["--arrange", "0"])
    probe(
        "painted-bottom-no-colours",
        bottom,
        change=lambda m, p, fs: [f.pop("filament_colour", None) for f in fs],
        extra=["--arrange", "0"],
    )
    probe(
        "export-slicedata",
        extra=["--export-slicedata", str(root / "export-slicedata" / "out")],
    )
    probe("baseline")
    probe("raw-system-presets", raw=True)
    probe("machine-user", change=lambda m, p, fs: m.update({"from": "User"}))
    probe(
        "wipe-tower-filament",
        change=lambda m, p, fs: p.update({"wipe_tower_filament": "0"}),
    )
    probe(
        "export-3mf",
        extra=["--export-3mf", str(root / "export-3mf" / "out" / "cube.3mf")],
    )
    probe("msys-settings-path", path_style=True)
    probe("invalid-enum", change=lambda m, p, fs: p.update({"support_type": "bogus"}))
    probe("output-229", output_length=229)
    probe("output-230", output_length=230)
    probe("safe-project", safe, extra=["--arrange", "0"])
    probe("plate-only", rewrite("plate-only", plate=True), extra=["--arrange", "0"])
    probe(
        "application-only",
        rewrite("application-only", application="Snapmaker_Orca-2.4.1"),
        extra=["--arrange", "0", "--allow-newer-file"],
    )
    probe(
        "full-project",
        rewrite(
            "full-project",
            application="Snapmaker_Orca-2.4.1",
            plate=True,
            settings=True,
        ),
        extra=["--arrange", "0", "--allow-newer-file"],
    )
    probe(
        "no-colours",
        safe,
        change=lambda m, p, fs: [f.pop("filament_colour", None) for f in fs],
        extra=["--arrange", "0"],
    )
    probe(
        "purge-zero",
        safe,
        change=lambda m, p, fs: p.update({"flush_volumes_matrix": ["0"] * 16}),
        extra=["--arrange", "0"],
    )
    probe(
        "purge-500",
        safe,
        change=lambda m, p, fs: p.update({"flush_volumes_matrix": ["500"] * 16}),
        extra=["--arrange", "0"],
    )
    probe(
        "prime-volume-5",
        safe,
        change=lambda m, p, fs: p.update({"prime_volume": "5"}),
        extra=["--arrange", "0"],
    )
    probe(
        "no-tower",
        safe,
        change=lambda m, p, fs: p.update({"enable_prime_tower": "0"}),
        extra=["--arrange", "0"],
    )
    probe(
        "by-object",
        safe,
        change=lambda m, p, fs: p.update({"print_sequence": "by object"}),
        extra=["--arrange", "0"],
    )


if __name__ == "__main__":
    main()
