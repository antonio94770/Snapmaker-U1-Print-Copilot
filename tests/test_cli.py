import json
import subprocess
from pathlib import Path

from print_copilot import cli
from print_copilot.orca import install as install_mod

U1 = "Snapmaker U1 (0.4 nozzle)"
U1_PROCESS = "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"


def test_format_duration() -> None:
    assert cli.format_duration(901) == "15m 1s"
    assert cli.format_duration(19) == "19s"
    assert cli.format_duration(3725) == "1h 2m 5s"


def test_presets_json(capsys, orca_data) -> None:
    code = cli.main(["presets", "--orca-data", str(orca_data), "--kind", "process", "--json"])
    out, err = capsys.readouterr()
    assert code == 0
    names = [p["name"] for p in json.loads(out)["process"]]
    assert U1_PROCESS in names
    assert "warning: skipped broken preset" in err


def test_presets_text_for_printer(capsys, orca_data) -> None:
    args = ["presets", "--orca-data", str(orca_data), "--kind", "filament", "--printer", U1]
    assert cli.main(args) == 0
    out, _ = capsys.readouterr()
    assert "filament (3)" in out
    assert "My PLA [user]" in out


def test_unknown_preset_is_an_error(capsys, monkeypatch, tmp_path, orca_data) -> None:
    exe = tmp_path / "snapmaker-orca.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv(install_mod.ENV_EXE, str(exe))
    args = ["slice", str(tmp_path / "cube.stl"), "--orca-data", str(orca_data)]
    code = cli.main([*args, "--printer", U1, "--process", "Nope", "--filament", "Generic PLA"])
    assert code == 1
    assert "error: No process preset named 'Nope'" in capsys.readouterr().err


def test_slice_single_filament_fills_all_heads(
    capsys, monkeypatch, tmp_path, orca_data, cube_gcode
) -> None:
    exe = tmp_path / "snapmaker-orca.exe"
    exe.write_bytes(b"")
    model = tmp_path / "cube.stl"
    model.write_text("solid\nendsolid\n")
    commands = []

    def run(command, stdout, **kwargs):
        commands.append(command)
        out_dir = Path(command[command.index("--outputdir") + 1])
        (out_dir / "plate_1.gcode").write_bytes(cube_gcode.read_bytes())
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    args = ["slice", str(model), "--orca-exe", str(exe), "--orca-data", str(orca_data)]
    args += ["--work-dir", str(tmp_path / "runs"), "--printer", U1, "--process", U1_PROCESS]
    code = cli.main([*args, "--filament", "Generic PLA", "--json"])

    out, err = capsys.readouterr()
    assert code == 0, err
    data = json.loads(out)
    assert data["filaments"] == ["Generic PLA"] * 4
    assert data["result"]["estimated_time_s"] == 901
    assert data["result"]["tools_used"] == [0]
    assert "on all 4 tool heads" in err
    assert len(commands) == 1
