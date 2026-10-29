"""Write the two-colour bust project used in the README demo.

The synthetic bust from `tests/synthetic_bust.py` as a one-plate 3MF in the
Bambu Studio / Snapmaker Orca layout: white body, hair and base painted with
filament 2 (black). Saved "for" a Bambu printer, like a downloaded project.

Run from the repository root:
`python research/demo/make_demo_bust.py runs/demo/bust-2c.3mf`
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from synthetic_bust import bust_mesh

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PAINT_2 = "8"  # leaf with state 2 (see print_copilot/orca/paint.py)


def painted(centroid: np.ndarray) -> bool:
    _, y, z = centroid
    hair = z > 30.5 or (z > 24.0 and y > -2.0)
    base = z < 3.0
    return bool(hair or base)


def model_xml() -> str:
    tris = bust_mesh().triangles
    points, index = np.unique(tris.reshape(-1, 3).round(5), axis=0, return_inverse=True)
    index = index.reshape(-1, 3)
    vertices = "".join(f'<vertex x="{x:g}" y="{y:g}" z="{z:g}"/>' for x, y, z in points)
    triangles = "".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"'
        + (f' paint_color="{PAINT_2}"' if painted(t.mean(axis=0)) else "")
        + "/>"
        for (a, b, c), t in zip(index, tris, strict=True)
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{CORE}">'
        '<metadata name="Application">BambuStudio-02.00.00.00</metadata><resources>'
        f'<object id="1" type="model"><mesh><vertices>{vertices}</vertices>'
        f"<triangles>{triangles}</triangles></mesh></object></resources>"
        '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 128 128 0"/></build></model>'
    )


def build(path: Path) -> Path:
    meta = '<metadata key="{}" value="{}"/>'.format
    config = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>'
        f'<object id="1">{meta("name", "bust")}{meta("extruder", "1")}</object>'
        f"<plate>{meta('plater_id', '1')}{meta('plater_name', 'bust')}"
        f"<model_instance>{meta('object_id', '1')}{meta('instance_id', '0')}</model_instance>"
        "</plate></config>\n"
    )
    settings = (
        '{"printer_settings_id": "Bambu Lab X1 Carbon 0.4 nozzle", '
        '"filament_colour": ["#FFFFFF", "#000000"], "filament_type": ["PLA", "PLA"], '
        '"filament_settings_id": ["Generic PLA", "Generic PLA"], "layer_height": "0.16"}'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?><Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    )
    types = (
        '<?xml version="1.0" encoding="UTF-8"?><Types '
        'xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" '
        'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("3D/3dmodel.model", model_xml())
        archive.writestr("Metadata/model_settings.config", config)
        archive.writestr("Metadata/project_settings.config", settings)
    return path


if __name__ == "__main__":
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "bust-2c.3mf")))
