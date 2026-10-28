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


def test_setup_text(capsys, orca_data) -> None:
    assert cli.main(["setup", "--orca-data", str(orca_data)]) == 0
    out = capsys.readouterr().out
    assert f"printer   : {U1}" in out
    assert "T1        : Generic PLA  (#000000)" in out
    assert "    T1      703      0    684    545" in out


def test_setup_json_other_printer(capsys, orca_data) -> None:
    args = ["setup", "--orca-data", str(orca_data), "--printer", "Default Printer", "--json"]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["filaments"] == ["AliZ PLA @System"]


def test_slice_set_overrides(capsys, fake_orca, fake_install, model, tmp_path) -> None:
    args = ["slice", str(model), "--orca-exe", str(fake_install.exe)]
    args += ["--orca-data", str(fake_install.data_dir), "--work-dir", str(tmp_path / "runs")]
    code = cli.main([*args, "--set", "sparse_infill_density=30%", "--json"])
    out, err = capsys.readouterr()
    assert code == 0, err
    data = json.loads(out)
    assert data["overrides"] == {"sparse_infill_density": "30%"}
    assert data["result"]["total_filament_g"] == 4.71
    assert "note: process" in err


def test_slice_malformed_set(capsys, fake_orca, fake_install, model) -> None:
    args = ["slice", str(model), "--orca-exe", str(fake_install.exe)]
    code = cli.main([*args, "--orca-data", str(fake_install.data_dir), "--set", "wall_loops"])
    assert code == 1
    assert "expected KEY=VALUE" in capsys.readouterr().err


def test_compare_text(capsys, fake_orca, fake_install, model, tmp_path) -> None:
    args = ["compare", str(model), "--orca-exe", str(fake_install.exe)]
    args += ["--orca-data", str(fake_install.data_dir), "--work-dir", str(tmp_path / "runs")]
    code = cli.main([*args, "--variant", "", "--variant", "sparse_infill_density=30%;wall_loops=2"])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "presets as is" in out
    assert "sparse_infill_density=30%, wall_loops=2" in out
    assert "+1.06" in out
    assert len(fake_orca.commands) == 2


def test_compare_variants_file(capsys, fake_orca, fake_install, model, tmp_path) -> None:
    variants = tmp_path / "variants.json"
    variants.write_text(
        json.dumps(
            [
                {"label": "base", "overrides": {}},
                {"label": "dense", "overrides": {"sparse_infill_density": "30%"}},
            ]
        ),
        encoding="utf-8",
    )
    args = ["compare", str(model), "--orca-exe", str(fake_install.exe), "--json"]
    args += ["--orca-data", str(fake_install.data_dir), "--work-dir", str(tmp_path / "runs")]
    assert cli.main([*args, "--variants-file", str(variants)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [v["label"] for v in data["variants"]] == ["base", "dense"]


def test_compare_needs_a_variant(capsys, fake_install, model) -> None:
    args = ["compare", str(model), "--orca-exe", str(fake_install.exe)]
    assert cli.main([*args, "--orca-data", str(fake_install.data_dir)]) == 1
    assert "at least one --variant" in capsys.readouterr().err


TEE = str(Path(__file__).parent / "fixtures" / "tee.stl")


def test_analyze_text(capsys, orca_data) -> None:
    assert cli.main(["analyze", TEE, "--orca-data", str(orca_data)]) == 0
    out, err = capsys.readouterr()
    assert "support threshold: 30 degrees" in out
    assert " 1 +Z down" in out
    assert "heuristic, not a slice" in out
    assert "from Snapmaker_Orca.conf" in err


def test_analyze_without_presets_json(capsys) -> None:
    assert cli.main(["analyze", TEE, "--no-presets", "--threshold", "45", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["threshold_deg"] == 45.0
    assert data["candidates"][0]["fits_bed"] is None


def test_orient(capsys, fake_orca, fake_install, tmp_path) -> None:
    args = ["orient", TEE, "--orca-exe", str(fake_install.exe), "--top", "1"]
    args += ["--orca-data", str(fake_install.data_dir), "--work-dir", str(tmp_path / "runs")]
    assert cli.main([*args, "--support", "tree"]) == 0
    out = capsys.readouterr().out
    assert "<- recommended" in out
    assert "1-posZ_down.stl" in out
    assert len(fake_orca.commands) == 2  # best candidate + as loaded


def test_render(capsys, tmp_path) -> None:
    out = tmp_path / "tee.png"
    args = ["render", TEE, "--no-presets", "--out", str(out), "--view", "front"]
    face = '{"label": "cap", "direction": [0, 0, 1], "angle_deg": 10}'
    assert cli.main([*args, "--down", "0,0,1", "--protect", face]) == 0
    text = capsys.readouterr().out
    assert out.read_bytes()[1:4] == b"PNG"
    assert "protected needs support: 0.0 mm2" in text
    # Never overwrite an existing file.
    assert cli.main(args) == 1
    assert "already exists" in capsys.readouterr().err


def test_analyze_quality_with_protect(capsys) -> None:
    face = '{"label": "cap", "direction": [0, 0, 1], "angle_deg": 10}'
    args = ["analyze", TEE, "--no-presets", "--goal", "quality", "--protect", face]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "protected: cap (450.0 mm2)" in out
    assert "lowest quality cost" in out
    # Upside down would put the protected cap top on the bed.
    first = next(line for line in out.splitlines() if line.startswith(" 1 "))
    assert "+Z down" not in first


def test_bad_protect_is_an_error(capsys) -> None:
    assert cli.main(["analyze", TEE, "--no-presets", "--protect", "{nope"]) == 1
    assert "not valid JSON" in capsys.readouterr().err
    assert cli.main(["analyze", TEE, "--no-presets", "--protect", '{"colour": 1}']) == 1
    assert "keys from" in capsys.readouterr().err
