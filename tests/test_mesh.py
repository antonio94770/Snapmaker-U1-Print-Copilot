import zipfile
from pathlib import Path

import numpy as np
import pytest

from print_copilot.errors import ModelError
from print_copilot.model.mesh import Mesh, load_3mf, load_mesh, load_stl, write_stl

FIXTURES = Path(__file__).parent / "fixtures"
CUBE = Path(__file__).parents[1] / "research" / "cli-probe" / "cube.stl"


def test_ascii_stl_cube() -> None:
    mesh = load_stl(CUBE)
    assert mesh.face_count == 12
    assert mesh.volume_mm3 == pytest.approx(8000.0)
    assert mesh.open_edge_count() == 0
    assert mesh.bounds_mm.tolist() == [[0, 0, 0], [20, 20, 20]]


def test_binary_stl_tee() -> None:
    mesh = load_mesh(FIXTURES / "tee.stl")
    assert mesh.face_count == 28
    assert mesh.volume_mm3 == pytest.approx(5250.0)
    assert mesh.open_edge_count() == 0


def test_write_stl_round_trip(tmp_path) -> None:
    mesh = load_stl(CUBE)
    path = write_stl(mesh, tmp_path / "out.stl")
    assert path.stat().st_size == 84 + 50 * 12
    again = load_stl(path)
    assert np.allclose(again.triangles, mesh.triangles)


def test_normals_point_outward() -> None:
    mesh = load_stl(CUBE)
    centre = mesh.bounds_mm.mean(axis=0)
    centroids = mesh.triangles.mean(axis=1)
    assert ((centroids - centre) * mesh.normals).sum(axis=1).min() > 0


def test_placed_on_bed() -> None:
    mesh = Mesh(load_stl(CUBE).triangles + 50).placed_on_bed()
    assert mesh.bounds_mm.tolist() == [[-10, -10, 0], [10, 10, 20]]


def test_open_mesh_is_detected() -> None:
    mesh = Mesh(load_stl(CUBE).triangles[:-1])
    assert mesh.open_edge_count() == 3


def _write_3mf(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return path


TRIANGLE_OBJECT = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="{unit}" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources>
  <object id="1" type="model"><mesh>
   <vertices>
    <vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>
   </vertices>
   <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
  </mesh></object>
 </resources>
 <build>{build}</build>
</model>"""

RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0"
  Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""


def test_3mf_build_items_with_transforms(tmp_path) -> None:
    build = '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 10 20 5"/><item objectid="1"/>'
    path = _write_3mf(
        tmp_path / "plate.3mf",
        {
            "_rels/.rels": RELS,
            "3D/3dmodel.model": TRIANGLE_OBJECT.format(unit="centimeter", build=build),
        },
    )
    mesh = load_3mf(path)
    assert mesh.face_count == 2
    assert mesh.object_count == 2
    assert "combined" in mesh.notes[0]
    # Units are scaled to mm before the item transform (which is in model units).
    assert mesh.triangles[0].tolist() == [[10, 20, 5], [20, 20, 5], [10, 30, 5]]
    assert mesh.triangles[1].tolist() == [[0, 0, 0], [10, 0, 0], [0, 10, 0]]


def test_3mf_production_components(tmp_path) -> None:
    root = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
 xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">
 <resources>
  <object id="2" type="model"><components>
   <component p:path="/3D/Objects/object_1.model" objectid="1"
    transform="0 1 0 -1 0 0 0 0 1 0 0 0"/>
  </components></object>
 </resources>
 <build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 100 100 0"/></build>
</model>"""
    path = _write_3mf(
        tmp_path / "project.3mf",
        {
            "_rels/.rels": RELS,
            "3D/3dmodel.model": root,
            "3D/Objects/object_1.model": TRIANGLE_OBJECT.format(unit="millimeter", build=""),
        },
    )
    mesh = load_mesh(path)
    # Component rotates 90 degrees about Z (row-vector convention), item moves to 100, 100.
    assert np.allclose(mesh.triangles[0], [[100, 100, 0], [100, 101, 0], [99, 100, 0]])


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("model.obj", b"v 0 0 0", "Unsupported model format"),
        ("empty.stl", b"solid x\nendsolid x\n", "no triangles"),
        ("junk.stl", b"\x00" * 10, "neither a binary STL nor an ASCII STL"),
        ("junk.3mf", b"not a zip", "Cannot open"),
    ],
)
def test_bad_files_fail(tmp_path, name, content, message) -> None:
    path = tmp_path / name
    path.write_bytes(content)
    with pytest.raises(ModelError, match=message):
        load_mesh(path)


def test_missing_file_fails(tmp_path) -> None:
    with pytest.raises(ModelError, match="not found"):
        load_mesh(tmp_path / "nope.stl")


def test_3mf_without_build_items_fails(tmp_path) -> None:
    model = TRIANGLE_OBJECT.format(unit="millimeter", build="")
    path = _write_3mf(tmp_path / "x.3mf", {"3D/3dmodel.model": model})
    with pytest.raises(ModelError, match="lists no objects"):
        load_3mf(path)
