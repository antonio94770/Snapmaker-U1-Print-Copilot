"""Multi-colour project actions with a fake slicer (no Snapmaker Orca needed)."""

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from print_copilot import cli, multicolor
from print_copilot.actions import Variant, run_slice, select_presets
from print_copilot.errors import ModelError, SliceError
from print_copilot.orca.project import PROJECT_CONFIG, read_project
from synthetic_project import build_project

U1 = "Snapmaker U1 (0.4 nozzle)"


class FakeMultiOrca:
    """Writes G-code like the U1 slices: the heads the 3MF assigns (object and
    part extruders, painting) print 3 layers; with the prime tower each change
    purges 1 mm. `print_sequence = by object` changes tools once per head.
    With `tower_conflict`, a run without `wipe_tower_x` fails like the real
    slicer when the tower overlaps an object."""

    def __init__(self, tower_conflict: bool = False) -> None:
        self.tower_conflict = tower_conflict
        self.commands: list[list[str]] = []
        self.processes: list[dict] = []

    def __call__(self, command, stdout, **kwargs):
        self.commands.append(command)
        settings = command[command.index("--load-settings") + 1].split(";")
        machine = json.loads(Path(settings[0]).read_text(encoding="utf-8"))
        process = json.loads(Path(settings[1]).read_text(encoding="utf-8"))
        filaments = command[command.index("--load-filaments") + 1].split(";")
        self.processes.append(process)
        if self.tower_conflict and "wipe_tower_x" not in process:
            stdout.write(b"[error] gcode path conflicts found between WipeTower and cube\n")
            return subprocess.CompletedProcess(command, 0xFFFFFF9B)
        project = read_project(command[-1])
        heads = [n - 1 for n in project.plate_filaments(1)]
        tower = process.get("enable_prime_tower") != "0"
        by_object = process.get("print_sequence") == "by object"
        lines = [f"T{heads[0]}", "M83"]
        used = {h: 0.0 for h in range(len(filaments))}
        current = heads[0]
        order = (
            [(h, layer) for h in heads for layer in range(3)]
            if by_object
            else [(h, layer) for layer in range(3) for h in heads]
        )
        last_layer = -1
        for head, layer in order:
            if layer != last_layer:
                lines.append(";LAYER_CHANGE")
                last_layer = layer
            if head != current:
                lines.append(f"T{head}")
                lines.append(f"SM_PRINT_PREEXTRUDE_FILAMENT INDEX={head}")
                current = head
                if tower:
                    lines += [";TYPE:Prime tower", "; CP TOOLCHANGE WIPE", "G1 X1 Y1 E1"]
                    lines.append("; CP TOOLCHANGE END")
                    used[head] += 1
            lines += [";TYPE:Outer wall", "G1 X10 Y10 E10"]
            used[head] += 10
        mm = [used[h] for h in range(len(filaments))]
        grams = [round(v * 0.003, 2) for v in mm]
        changes = sum(1 for line in lines if line.startswith("T")) - 1
        lines += [
            "; EXECUTABLE_BLOCK_END",
            "; filament used [mm] = " + ", ".join(f"{v:.2f}" for v in mm),
            "; filament used [g] = " + ", ".join(f"{v:.2f}" for v in grams),
            f"; total filament used [g] = {sum(grams):.2f}",
            f"; estimated printing time (normal mode) = {10 + changes}m 0s",
            "; total layer number: 3",
            "; CONFIG_BLOCK_START",
            f"; printer_settings_id = {machine['name']}",
            f"; print_settings_id = {process['name']}",
        ]
        for key in ("enable_prime_tower", "print_sequence", "wipe_tower_x", "wipe_tower_y"):
            value = process.get(key)
            if value is not None:
                lines.append(f"; {key} = {value[0] if isinstance(value, list) else value}")
        lines.append("; CONFIG_BLOCK_END")
        out_dir = Path(command[command.index("--outputdir") + 1])
        (out_dir / "plate_1.gcode").write_text("\n".join(lines) + "\n", encoding="utf-8")
        stdout.write(b"ok\n")
        return subprocess.CompletedProcess(command, 0)


@pytest.fixture
def fake_multi(monkeypatch) -> FakeMultiOrca:
    fake = FakeMultiOrca()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


@pytest.fixture
def project_file(tmp_path: Path) -> Path:
    return build_project(tmp_path / "synthetic.3mf")


@pytest.fixture
def selection(library):
    return select_presets(library, printer=U1)


def test_inspect_project(project_file, selection) -> None:
    summary = multicolor.inspect_project(project_file, selection)
    assert [h.colour for h in summary.heads] == ["#FFFFFF", "#000000", "#F4E2C1", "#ED1C24"]
    plate1, plate2 = summary.plates
    assert plate1.objects == ["left cube", "tower"]
    assert [w.number for w in plate1.filaments] == [1, 2, 3, 4]
    assert plate1.assignment.mapping() == {1: 4, 2: 3, 3: 2, 4: 1}  # red, green, blue, white
    assert plate2.assignment.mapping() == {4: 1, 5: 2}  # white -> T0, black -> T1
    assert any("object id(s) ['99']" in n for n in summary.notes)


def test_slice_project(fake_install, fake_multi, project_file, selection, tmp_path) -> None:
    result = multicolor.slice_project(
        fake_install, project_file, selection, plate=1, work_dir=tmp_path / "w"
    )
    command = fake_multi.commands[0]
    assert command[-3:-1] == ["--arrange", "0"]
    assert command[-1].endswith("plate1-cli.3mf")
    filament_1 = json.loads(
        Path(command[command.index("--load-filaments") + 1].split(";")[0]).read_text()
    )
    assert filament_1["filament_colour"] == ["#FFFFFF"]
    assert result.tool_changes == 11  # 4 heads x 3 layers, minus the first selection
    assert result.layers_with_changes == 3
    assert result.prime_tower_g == pytest.approx(11 * 0.003, abs=0.01)
    assert [h.head for h in result.heads] == [0, 1, 2, 3]
    assert result.heads[3].project_filaments == [1]
    assert any("SM_PRINT_PREEXTRUDE_FILAMENT 11 times" in n for n in result.notes)
    assert any("saved for 'Bambu Lab X1 Carbon" in n for n in result.notes)
    assert (result.run_dir / "project_slice.json").is_file()


def test_slice_project_needs_a_plate_and_known_filaments(
    fake_install, fake_multi, project_file, selection, tmp_path
) -> None:
    with pytest.raises(SliceError, match="2 plates"):
        multicolor.slice_project(fake_install, project_file, selection, work_dir=tmp_path)
    with pytest.raises(SliceError, match="not used on this plate"):
        multicolor.slice_project(
            fake_install, project_file, selection, plate=2, mapping={1: 0}, work_dir=tmp_path
        )


def test_plain_slice_refuses_a_project(fake_install, project_file, selection) -> None:
    with pytest.raises(ModelError, match="slice-project"):
        run_slice(fake_install, project_file, selection)


def test_tower_is_moved_after_a_conflict(
    fake_install, monkeypatch, project_file, selection, tmp_path
) -> None:
    fake = FakeMultiOrca(tower_conflict=True)
    monkeypatch.setattr(subprocess, "run", fake)
    result = multicolor.slice_project(
        fake_install, project_file, selection, plate=2, work_dir=tmp_path
    )
    x = float(fake.processes[-1]["wipe_tower_x"][0])
    y = float(fake.processes[-1]["wipe_tower_y"][0])
    assert 0 <= x <= 270 and 0 <= y <= 270
    # The far cube (15 mm) is centred: the tower keeps clear of 127.5..142.5.
    assert x + 40 < 127.5 or x > 142.5 or y + 80 < 127.5 or y > 142.5
    assert any("prime tower collided" in n for n in result.notes)


def test_compare_strategies(fake_install, fake_multi, project_file, selection, tmp_path) -> None:
    comparison = multicolor.compare_strategies(
        fake_install, project_file, selection, plate=1, work_dir=tmp_path
    )
    by_label = {v.label: v for v in comparison.variants}
    assert list(by_label) == ["presets as is", "no prime tower", "one object at a time"]
    assert by_label["no prime tower"].slice.prime_tower_g == 0
    assert by_label["no prime tower"].filament_delta_g < 0
    assert by_label["one object at a time"].slice.tool_changes == 3
    assert comparison.fastest == "one object at a time"
    assert any("SM_PRINT_PREEXTRUDE_FILAMENT; ooze" in n for n in comparison.notes)
    assert (comparison.work_dir / "strategies.json").is_file()
    # Plate 2 has one object: no "one object at a time".
    single = multicolor.compare_strategies(
        fake_install, project_file, selection, plate=2, work_dir=tmp_path
    )
    assert [v.label for v in single.variants] == ["presets as is", "no prime tower"]


def test_compare_strategies_reports_a_refused_variant(
    fake_install, monkeypatch, project_file, selection, tmp_path
) -> None:
    fake = FakeMultiOrca()

    def refuse_by_object(command, stdout, **kwargs):
        process = json.loads(
            Path(command[command.index("--load-settings") + 1].split(";")[1]).read_text()
        )
        if process.get("print_sequence") == "by object":
            stdout.write(
                b"[error] left cube is too close to others, and collisions may be caused.\n"
            )
            return subprocess.CompletedProcess(command, 0xFFFFFFC1)
        return fake(command, stdout, **kwargs)

    monkeypatch.setattr(subprocess, "run", refuse_by_object)
    comparison = multicolor.compare_strategies(
        fake_install,
        project_file,
        selection,
        plate=1,
        variants=[Variant({}, "base"), Variant({"print_sequence": "by object"}, "by object")],
        work_dir=tmp_path,
    )
    refused = comparison.variants[1]
    assert refused.slice is None and "too close to others" in refused.error
    assert comparison.fastest == "base"


def test_export_project(fake_install, project_file, selection, tmp_path) -> None:
    out = tmp_path / "out.3mf"
    exported = multicolor.export_project(
        fake_install, project_file, selection, out, plate=2, work_dir=tmp_path / "w"
    )
    assert exported.path == out
    with zipfile.ZipFile(out) as archive:
        root = archive.read("3D/3dmodel.model").decode()
        settings = json.loads(archive.read(PROJECT_CONFIG))
    assert '<metadata name="Application">Snapmaker_Orca-01.10.01.50</metadata>' in root
    assert settings["printer_settings_id"] == U1
    assert settings["filament_colour"] == ["#FFFFFF", "#000000", "#F4E2C1", "#ED1C24"]
    assert len(settings["filament_settings_id"]) == 4
    with pytest.raises(SliceError, match="already exists"):
        multicolor.export_project(fake_install, project_file, selection, out, plate=2)


def test_open_in_app(fake_install, monkeypatch, project_file) -> None:
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kw: started.append(command))
    command = multicolor.open_in_app(fake_install, project_file)
    assert started == [command]
    assert command[0] == str(fake_install.exe)
    with pytest.raises(SliceError, match="not found"):
        multicolor.open_in_app(fake_install, project_file.with_name("missing.3mf"))


def test_cli_project_commands(
    capsys, monkeypatch, fake_install, fake_multi, orca_data, project_file, tmp_path
) -> None:
    monkeypatch.setenv("PRINT_COPILOT_ORCA_EXE", str(fake_install.exe))
    common = ["--orca-data", str(orca_data), "--printer", U1]
    assert cli.main(["project", str(project_file), *common]) == 0
    out, _ = capsys.readouterr()
    assert "plate 1 'two cubes': 2 object(s)" in out
    assert "filament 1 (#FF0000 PLA) -> T3" in out

    work = ["--work-dir", str(tmp_path / "w")]
    args = ["slice-project", str(project_file), *common, *work, "--plate", "2", "--map", "5=T3"]
    assert cli.main(args) == 0
    out, _ = capsys.readouterr()
    assert "changes   : 5 tool changes" in out
    assert "filament 5 (#000000 PLA) -> T3" in out and "[given]" in out

    assert cli.main(["strategies", str(project_file), *common, *work, "--plate", "1"]) == 0
    out, _ = capsys.readouterr()
    assert "one object at a time" in out and "fastest:" in out

    target = tmp_path / "exported.3mf"
    args = [
        "export-project",
        str(project_file),
        *common,
        *work,
        "--plate",
        "2",
        "--out",
        str(target),
    ]
    assert cli.main(args) == 0
    assert target.is_file()
    assert cli.main([*args[:-1], str(tmp_path / "other.3mf"), "--map", "bad"]) == 1
    _, err = capsys.readouterr()
    assert "expected KEY=VALUE" in err
