import json
import re
import zipfile
from pathlib import Path

import numpy as np
import pytest

from print_copilot.errors import ModelError
from print_copilot.model.mesh import load_3mf_items
from print_copilot.orca.project import (
    MODEL_CONFIG,
    PROJECT_CONFIG,
    is_project,
    project_settings,
    read_project,
    write_app_project,
    write_slicing_copy,
)
from synthetic_project import COLOURS, build_project

BED = (270.0, 270.0, 270.0)


@pytest.fixture
def project_file(tmp_path: Path) -> Path:
    return build_project(tmp_path / "synthetic.3mf")


def _read(path: Path, name: str) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read(name).decode()


def test_read_project(project_file: Path) -> None:
    project = read_project(project_file)
    assert project.application == "BambuStudio-02.00.00.00"
    assert project.printer == "Bambu Lab X1 Carbon 0.4 nozzle"
    assert [(f.number, f.colour, f.type) for f in project.filaments] == [
        (i + 1, c, "PLA") for i, c in enumerate(COLOURS)
    ]
    assert [(p.number, p.name, p.object_ids) for p in project.plates] == [
        (1, "two cubes", ["10", "11"]),
        (2, None, ["12"]),
    ]
    tower = project.objects[1]
    assert (tower.name, tower.extruder) == ("tower", 1)
    assert [(p.id, p.extruder, p.painted) for p in tower.parts] == [("2", 0, []), ("3", 3, [4])]
    assert tower.parts[1].source == "3D/Objects/parts.model"
    assert project.plate_filaments(1) == [1, 2, 3, 4]
    assert project.plate_filaments(2) == [4, 5]
    assert any("object id(s) ['99']" in n for n in project.notes)
    with pytest.raises(ModelError, match="no plate 3"):
        project.plate(3)


def test_plain_3mf_is_one_plate(tmp_path: Path, project_file: Path) -> None:
    plain = tmp_path / "plain.3mf"
    with zipfile.ZipFile(project_file) as zin, zipfile.ZipFile(plain, "w") as zout:
        for name in zin.namelist():
            if not name.startswith("Metadata/"):
                data = zin.read(name)
                if name == "3D/3dmodel.model":
                    data = re.sub(rb'<metadata name="Application">[^<]*</metadata>', b"", data)
                zout.writestr(name, data)
    assert not is_project(plain)
    assert is_project(project_file)
    project = read_project(plain)
    assert [p.object_ids for p in project.plates] == [["10", "11", "12"]]
    assert all(o.extruder == 1 for o in project.objects)
    assert project.filaments == []


def test_slicing_copy_keeps_one_plate_and_remaps(tmp_path: Path, project_file: Path) -> None:
    project = read_project(project_file)
    mapping = {1: 4, 2: 1, 3: 2, 4: 3}
    copy = write_slicing_copy(project, 1, mapping, tmp_path / "cli.3mf", bed_mm=BED)
    with zipfile.ZipFile(copy.path) as archive:
        names = set(archive.namelist())
        root = archive.read("3D/3dmodel.model").decode()
        parts = archive.read("3D/Objects/parts.model").decode()
        config = archive.read(MODEL_CONFIG).decode()
    assert names == {
        "[Content_Types].xml",
        "_rels/.rels",
        "3D/3dmodel.model",
        "3D/_rels/3dmodel.model.rels",
        "3D/Objects/parts.model",
        MODEL_CONFIG,
    }
    assert "Application" not in root
    assert 'objectid="12"' not in root and '<object id="12"' not in root
    assert "<plate" not in config and "<assemble" not in config
    assert 'id="12"' not in config and 'id="99"' not in config
    assert 'key="extruder" value="1"' in config  # object 10: filament 2 -> head 1
    assert 'value="4"' in config  # object 11: filament 1 -> head 4
    assert 'key="extruder" value="0"' in config  # part 2 keeps "the object's"
    assert 'paint_color="0C"' in parts  # filament 4 -> head 3 on the kept part
    assert 'paint_color="2C"' in parts  # object 4 is on plate 2: untouched
    # Centred on the bed, on Z = 0, arrangement kept.
    items = load_3mf_items(copy.path)
    points = np.concatenate([t.reshape(-1, 3) for _, t in items])
    low, high = points.min(axis=0), points.max(axis=0)
    assert (low[:2] + high[:2]) / 2 == pytest.approx([135, 135])
    assert low[2] == pytest.approx(0)
    assert copy.offset_mm[:2] == pytest.approx([135 - 120, 135 - 120])  # X 90..150
    assert copy.object_ids == ["10", "11"]


def test_slicing_copy_errors(tmp_path: Path, project_file: Path) -> None:
    project = read_project(project_file)
    with pytest.raises(ModelError, match=r"Filament\(s\) \[5\]"):
        write_slicing_copy(project, 2, {4: 1}, tmp_path / "a.3mf", bed_mm=BED)
    with pytest.raises(ModelError, match="larger than the printable volume"):
        write_slicing_copy(
            project, 1, {i: 1 for i in range(1, 5)}, tmp_path / "b.3mf", bed_mm=(40, 40, 40)
        )


def test_app_project(tmp_path: Path, project_file: Path) -> None:
    project = read_project(project_file)
    copy = write_slicing_copy(project, 2, {4: 1, 5: 2}, tmp_path / "cli.3mf", bed_mm=BED)
    settings = project_settings(
        {"name": "U1", "printable_height": "270", "nozzle_diameter": ["0.4"] * 2},
        {"name": "P", "layer_height": "0.2", "inherits": "x"},
        [{"name": "F", "filament_type": ["PLA"]}, {"name": "F", "filament_type": ["PETG"]}],
        machine_name="U1",
        process_name="P",
        filament_names=["F", "F"],
        colours=["#FFFFFF", "#000000"],
    )
    assert settings["filament_type"] == ["PLA", "PETG"]
    assert settings["layer_height"] == "0.2"
    assert "inherits" not in settings or settings["inherits"] != "x"
    out = write_app_project(
        copy.path,
        tmp_path / "app.3mf",
        application="Snapmaker_Orca-1.2.3",
        object_ids=["12"],
        settings=settings,
    )
    root = _read(out, "3D/3dmodel.model")
    assert '<metadata name="Application">Snapmaker_Orca-1.2.3</metadata>' in root
    config = _read(out, MODEL_CONFIG)
    assert config.count("<plate>") == 1 and 'value="12"' in config
    stored = json.loads(_read(out, PROJECT_CONFIG))
    assert stored["printer_settings_id"] == "U1"
    assert stored["filament_colour"] == ["#FFFFFF", "#000000"]
    with pytest.raises(ModelError, match="already exists"):
        write_app_project(copy.path, out, application="x", object_ids=[], settings={})
