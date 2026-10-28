"""A small multi-colour 3MF project in the layout Bambu Studio and Snapmaker
Orca save (meshes in `3D/Objects/*.model`, `Metadata/model_settings.config`
with plates, `Metadata/project_settings.config` with filament colours).

Plate 1:
- object 10 "left cube" (20 mm), object filament 2;
- object 11 "tower", filament 1: part 2 is a 20 mm cube with no filament of
  its own (extruder 0), part 3 is a 10 mm cube on top with filament 3 whose
  top face is painted with filament 4.
Plate 2:
- object 12 "far cube", filament 4, one face painted with filament 5.
`model_settings.config` also holds settings for object 99, which no build
item uses (seen in a downloaded project).

Five filaments: red, green, blue, white, black PLA.

Run as a script to write the file: `python tests/synthetic_project.py out.3mf`.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

COLOURS = ["#FF0000", "#00FF00", "#0000FF", "#FFFFFF", "#000000"]
# paint_color values (see print_copilot/orca/paint.py): one leaf each.
PAINT_4 = "1C"  # nibbles C, 1 read from the end: extended state 1 + 3 = 4
PAINT_5 = "2C"
CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"

# Corners and outward-wound faces of a unit cube; face 10/11 is the top (+Z).
_CORNERS = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
_FACES = [
    (0, 2, 1), (0, 3, 2),  # bottom
    (0, 1, 5), (0, 5, 4),  # front
    (1, 2, 6), (1, 6, 5),  # right
    (2, 3, 7), (2, 7, 6),  # back
    (3, 0, 4), (3, 4, 7),  # left
    (4, 5, 6), (4, 6, 7),  # top
]  # fmt: skip


def cube_object(object_id: int, size: float, z0: float = 0.0, paint: dict | None = None) -> str:
    """A mesh object; `paint` maps face index to a paint_color value."""
    vertices = "".join(
        f'<vertex x="{x * size - size / 2:g}" y="{y * size - size / 2:g}" z="{z * size + z0:g}"/>'
        for x, y, z in _CORNERS
    )
    triangles = "".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"'
        + (f' paint_color="{paint[i]}"' if paint and i in paint else "")
        + "/>"
        for i, (a, b, c) in enumerate(_FACES)
    )
    return (
        f'<object id="{object_id}" type="model"><mesh><vertices>{vertices}</vertices>'
        f"<triangles>{triangles}</triangles></mesh></object>"
    )


def _wrapper(object_id: int, components: list[int]) -> str:
    comps = "".join(
        f'<component p:path="/3D/Objects/parts.model" objectid="{c}" '
        'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        for c in components
    )
    return f'<object id="{object_id}" type="model"><components>{comps}</components></object>'


def _item(object_id: int, x: float, y: float) -> str:
    return f'<item objectid="{object_id}" transform="1 0 0 0 1 0 0 0 1 {x:g} {y:g} 0"/>'


def _meta(key: str, value: str) -> str:
    return f'<metadata key="{key}" value="{value}"/>'


def build_project(path: str | Path, *, application: str = "BambuStudio-02.00.00.00") -> Path:
    parts = (
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{CORE}" '
        f'xmlns:p="{PROD}" requiredextensions="p"><resources>'
        + cube_object(1, 20)
        + cube_object(2, 20)
        + cube_object(3, 10, z0=19.5, paint={10: PAINT_4, 11: PAINT_4})
        + cube_object(4, 15, paint={0: PAINT_5})
        + "</resources></model>"
    )
    root = (
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{CORE}" '
        f'xmlns:p="{PROD}" requiredextensions="p">'
        f'<metadata name="Application">{application}</metadata><resources>'
        + _wrapper(10, [1])
        + _wrapper(11, [2, 3])
        + _wrapper(12, [4])
        + "</resources><build>"
        + _item(10, 100, 120)
        + _item(11, 140, 120)
        + _item(12, 430, 120)
        + "</build></model>"
    )
    config = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>'
        f'<object id="10">{_meta("name", "left cube")}{_meta("extruder", "2")}'
        f'<part id="1" subtype="normal_part">{_meta("name", "cube")}</part></object>'
        f'<object id="11">{_meta("name", "tower")}{_meta("extruder", "1")}'
        f'<part id="2" subtype="normal_part">{_meta("extruder", "0")}</part>'
        f'<part id="3" subtype="normal_part">{_meta("extruder", "3")}</part></object>'
        f'<object id="12">{_meta("name", "far cube")}{_meta("extruder", "4")}</object>'
        f'<object id="99">{_meta("name", "ghost")}{_meta("extruder", "5")}</object>'
        f"<plate>{_meta('plater_id', '1')}{_meta('plater_name', 'two cubes')}"
        f"<model_instance>{_meta('object_id', '10')}{_meta('instance_id', '0')}</model_instance>"
        f"<model_instance>{_meta('object_id', '11')}{_meta('instance_id', '0')}</model_instance>"
        f"</plate><plate>{_meta('plater_id', '2')}"
        f"<model_instance>{_meta('object_id', '12')}{_meta('instance_id', '0')}</model_instance>"
        "</plate>"
        '<assemble><assemble_item object_id="10" instance_id="0" transform="1 0 0 0 1 0 0 0 1 '
        '0 0 0" offset="0 0 0"/></assemble></config>\n'
    )
    settings = (
        '{"printer_settings_id": "Bambu Lab X1 Carbon 0.4 nozzle", '
        f'"filament_colour": {COLOURS!r}, "filament_type": ["PLA", "PLA", "PLA", "PLA", "PLA"], '
        '"filament_settings_id": ["Generic PLA", "Generic PLA", "Generic PLA", "Generic PLA", '
        '"Generic PLA"], "layer_height": "0.16", "wall_loops": "2", "enable_support": "0"}'
    ).replace("'", '"')
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?><Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    )
    model_rels = rels.replace("/3D/3dmodel.model", "/3D/Objects/parts.model")
    types = (
        '<?xml version="1.0" encoding="UTF-8"?><Types '
        'xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" '
        'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
    )
    out = Path(path)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("3D/3dmodel.model", root)
        archive.writestr("3D/_rels/3dmodel.model.rels", model_rels)
        archive.writestr("3D/Objects/parts.model", parts)
        archive.writestr("Metadata/model_settings.config", config)
        archive.writestr("Metadata/project_settings.config", settings)
        archive.writestr("Metadata/plate_1.png", b"not a real png")
    return out


def with_replaced(source: Path, target: Path, member: str, old: str, new: str) -> Path:
    """Copy of a 3MF with one text replacement in one of its files."""
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info)
            if info.filename == member:
                text = data.decode()
                assert old in text
                data = text.replace(old, new, 1).encode()
            zout.writestr(info, data)
    return target


def with_left_cube_settings(source: Path, target: Path, settings: str) -> Path:
    """Copy of the synthetic project whose 'left cube' object carries
    `settings` (metadata elements) in its model settings."""
    name = _meta("name", "left cube")
    return with_replaced(source, target, "Metadata/model_settings.config", name, name + settings)


if __name__ == "__main__":
    build_project(sys.argv[1] if len(sys.argv) > 1 else "synthetic_project.3mf")
