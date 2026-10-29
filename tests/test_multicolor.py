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
from print_copilot.printer.status import HeadStatus, LoadedFilament
from synthetic_project import build_project, with_left_cube_settings

TEE = Path(__file__).parent / "fixtures" / "tee.stl"

U1 = "Snapmaker U1 (0.4 nozzle)"


class FakeMultiOrca:
    """Writes G-code like the U1 slices: the heads the 3MF assigns (object and
    part extruders, painting) print 3 layers; with the prime tower each change
    purges 1 mm. `print_sequence = by object` changes tools once per head.
    Support (on in the process or on an object) adds 2 mm of `Support`.
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
        support = process.get("enable_support") == "1" or any(
            o.overrides.get("enable_support") == "1" for o in project.objects
        )
        if support:
            lines += [";TYPE:Support", "G1 X2 Y2 E2"]
            used[current] += 2
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
        for key in (
            "enable_prime_tower",
            "print_sequence",
            "wipe_tower_x",
            "wipe_tower_y",
            "enable_support",
            "support_type",
            "support_threshold_angle",
        ):
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
    # Sizes measured from the meshes as placed; the U1 bed is 270 x 270 x 270.05 mm.
    assert summary.bed_mm == [270.0, 270.0, 270.05]
    assert set(plate1.object_sizes_mm) == {"left cube", "tower"}
    assert plate1.fits_bed is True and plate1.size_mm is not None
    # The project was saved at 0.16 mm; the U1 preset prints 0.2 mm. Equal
    # values (wall_loops 2, enable_support 0) are not listed.
    assert summary.setting_differences == [
        multicolor.SettingDifference("layer_height", "0.16", "0.2")
    ]
    assert any("layer_height 0.16 -> 0.2" in n for n in summary.notes)


def test_inspect_plain_stl(selection) -> None:
    summary = multicolor.inspect_project(TEE, selection)
    assert summary.project == TEE
    (plate,) = summary.plates
    assert plate.objects == ["tee"]
    assert [w.number for w in plate.filaments] == [1]
    assert plate.fits_bed is True and plate.size_mm is not None
    assert summary.setting_differences == []
    assert any("tee.stl is a plain model" in n for n in summary.notes)
    assert any("print in one colour" in n for n in summary.notes)


def test_setting_differences_follow_overrides(project_file, selection) -> None:
    assert multicolor.setting_differences(project_file, selection, {"layer_height": 0.16}) == []
    differences = multicolor.setting_differences(
        project_file, selection, {"enable_support": True, "filament.layer_height": 1}
    )
    assert [(d.key, d.project, d.used) for d in differences] == [
        ("layer_height", "0.16", "0.2"),
        ("enable_support", "0", "1"),
    ]
    assert multicolor.setting_differences(TEE, selection) == []  # an STL stores none


def test_plate_too_big_for_the_bed(project_file, selection) -> None:
    selection.machine.settings["printable_height"] = "1"
    summary = multicolor.inspect_project(project_file, selection)
    assert summary.plates[0].fits_bed is False
    assert any("Plate 1 measures" in n and "larger than the printable" in n for n in summary.notes)


def test_slice_reports_supports(fake_install, fake_multi, project_file, selection, tmp_path):
    result = multicolor.slice_project(
        fake_install, project_file, selection, plate=2, work_dir=tmp_path / "a"
    )
    assert result.support.support_g == 0 and result.support.enabled_in_process is False
    assert not any("Support" in n for n in result.notes)

    on = multicolor.slice_project(
        fake_install,
        project_file,
        selection,
        plate=2,
        overrides={"enable_support": True},
        work_dir=tmp_path / "b",
    )
    assert on.support.enabled_in_process is True
    # 2 mm of support at the header's g/mm ratio (its grams are rounded to 0.01).
    assert on.support.support_g == pytest.approx(2 * 0.003, abs=0.005)
    assert on.support.threshold_deg == "30"
    assert not any("Supports are printed" in n for n in on.notes)

    # Support turned on by the object only, as MakerWorld projects do.
    model = with_left_cube_settings(
        project_file, tmp_path / "object.3mf", '<metadata key="enable_support" value="1"/>'
    )
    by_object = multicolor.slice_project(
        fake_install, model, selection, plate=1, work_dir=tmp_path / "c"
    )
    assert by_object.support.enabled_by_objects == ["left cube"]
    assert by_object.support.support_g > 0
    assert any(
        "Supports are printed" in n and "object(s) ['left cube']" in n for n in by_object.notes
    )


def test_support_on_without_supports_is_reported() -> None:
    support = multicolor.SupportSummary(True, "tree(auto)", "30", [], 0.0, 0.0)
    assert multicolor.support_notes(support) == [
        "Support is on, but the slicer generated none: no overhang is flatter than the "
        "support threshold (30 degrees from horizontal)."
    ]


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
    missing = tmp_path / "missing" / "out.3mf"
    with pytest.raises(SliceError, match="does not exist"):
        multicolor.export_project(fake_install, project_file, selection, missing, plate=2)


def test_export_without_out_path_uses_the_export_dir(
    fake_install, monkeypatch, project_file, selection, tmp_path
) -> None:
    monkeypatch.setenv("PRINT_COPILOT_EXPORT_DIR", str(tmp_path / "exports"))
    exported = multicolor.export_project(
        fake_install, project_file, selection, plate=2, work_dir=tmp_path / "w"
    )
    assert exported.path.name == "synthetic.3mf"
    assert exported.path.parent.parent == tmp_path / "exports"
    assert exported.path.parent.name.startswith("synthetic-")
    assert exported.path.is_file()


def test_open_in_app(fake_install, monkeypatch, project_file) -> None:
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kw: started.append(command))
    command = multicolor.open_in_app(fake_install, project_file)
    assert started == [command]
    assert command[0] == str(fake_install.exe)
    with pytest.raises(SliceError, match="not found"):
        multicolor.open_in_app(fake_install, project_file.with_name("missing.3mf"))
    args = ["--orca-exe", str(fake_install.exe), "--orca-data", str(fake_install.data_dir)]
    assert cli.main(["open", str(project_file), *args]) == 0
    assert started[-1] == [str(fake_install.exe), started[-1][1]]
    assert started[-1][1].endswith("synthetic.3mf")


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


# Loaded colours from the printer, one-colour plates, STL models, export + slice


def _head(index: int, colour: str | None, kind: str = "PLA") -> HeadStatus:
    spool = (
        LoadedFilament("Snapmaker", "Polymaker", kind, "SnapSpeed", colour, True, 190, 230)
        if colour
        else None
    )
    return HeadStatus(index, f"extruder{index or ''}", 25, 0, "PARKED", 0.4, spool)


def test_given_filaments_keep_the_stored_colours(library) -> None:
    chosen = select_presets(library, printer=U1, process=None, filaments=["Generic PLA"])
    assert chosen.colours == ["#FFFFFF", "#000000", "#F4E2C1", "#ED1C24"]
    assert chosen.colour_sources == ["Snapmaker_Orca.conf"] * 4


def test_printer_spools_replace_the_stored_colours(selection) -> None:
    heads = [_head(0, "#E2DEDB"), _head(1, "#FF0000", "PETG"), _head(2, None), _head(3, None)]
    updated = multicolor.use_printer_spools(selection, heads)
    assert updated.colours == ["#E2DEDB", "#FF0000", "#F4E2C1", "#ED1C24"]
    assert updated.colour_sources[:2] == ["RFID", "RFID"]
    assert any("T1: Snapmaker Orca has #000000 selected" in n for n in updated.notes)
    assert any("T1: the filament preset is PLA" in n for n in updated.notes)
    assert any(n.startswith("T2, T3: no RFID tag read") for n in updated.notes)
    assert selection.colours[1] == "#000000"  # the input is not changed


def test_unreachable_printer_keeps_the_stored_colours(selection) -> None:
    updated = multicolor.with_printer_spools(selection)  # conftest: no printer
    assert updated.colours == selection.colours
    assert any("Printer not reachable" in n for n in updated.notes)


def test_one_colour_plate_needs_a_head(fake_install, fake_multi, selection, tmp_path) -> None:
    model = Path(__file__).parent / "fixtures" / "tee.stl"
    with pytest.raises(multicolor.HeadChoiceRequired, match="ask the user") as error:
        multicolor.slice_project(fake_install, model, selection, work_dir=tmp_path)
    assert "T1 #000000 PLA" in str(error.value)
    assert "colour from Snapmaker_Orca.conf" in str(error.value)
    with pytest.raises(SliceError, match="either head"):
        multicolor.slice_project(
            fake_install, model, selection, head=1, mapping={1: 1}, work_dir=tmp_path
        )


def test_stl_prints_on_the_chosen_head(fake_install, fake_multi, selection, tmp_path) -> None:
    model = Path(__file__).parent / "fixtures" / "tee.stl"
    result = multicolor.slice_project(fake_install, model, selection, head=1, work_dir=tmp_path)
    assert [h.head for h in result.heads] == [1]
    assert result.heads[0].colour == "#000000"
    assert result.assignment.choices[0].explicit
    assert any("tee.stl is a plain model" in n for n in result.notes)


def test_export_slices_the_written_project(fake_install, fake_multi, selection, tmp_path) -> None:
    model = Path(__file__).parent / "fixtures" / "tee.stl"
    out = tmp_path / "tee_black.3mf"
    exported = multicolor.export_project(
        fake_install, model, selection, out, head=1, slice_gcode=True, work_dir=tmp_path / "w"
    )
    assert exported.gcode_path == tmp_path / "tee_black.gcode"
    assert exported.gcode_path.read_text(encoding="utf-8").startswith("T1")
    # One slice only, of the written project (filament 2 = T1 there).
    assert len(fake_multi.commands) == 1
    assert exported.slice.project == out
    assert read_project(fake_multi.commands[0][-1]).plate_filaments(1) == [2]
    assert exported.slice.heads[0].head == 1
    assert any(f"open {out}" in n for n in exported.notes)
    (tmp_path / "taken.gcode").write_text("x")
    with pytest.raises(SliceError, match=r"taken\.gcode already exists"):
        multicolor.export_project(
            fake_install, model, selection, tmp_path / "taken.3mf", head=1, slice_gcode=True
        )
    assert not (tmp_path / "taken.3mf").exists()


def test_cli_head_option(
    capsys, monkeypatch, fake_install, fake_multi, orca_data, tmp_path
) -> None:
    monkeypatch.setenv("PRINT_COPILOT_ORCA_EXE", str(fake_install.exe))
    model = str(Path(__file__).parent / "fixtures" / "tee.stl")
    common = ["--orca-data", str(orca_data), "--printer", U1, "--work-dir", str(tmp_path)]
    assert cli.main(["slice-project", model, *common]) == 1
    _, err = capsys.readouterr()
    assert "--head T<n>" in err
    out_path = tmp_path / "tee.3mf"
    assert cli.main(["export-project", model, *common, "--head", "T1", "--out", str(out_path)]) == 0
    out, err = capsys.readouterr()
    assert f"To reopen what this G-code prints: {out_path}" in out
    assert "Printer not reachable" in err
    assert cli.parse_head("t2") == 2
    with pytest.raises(SliceError, match="expected T<n>"):
        cli.parse_head("black")
