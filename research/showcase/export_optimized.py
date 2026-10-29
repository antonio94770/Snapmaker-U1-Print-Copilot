"""Export review candidates as fresh 3MF/G-code pairs, never to the printer."""

import json
import sys
from pathlib import Path

from print_copilot.actions import select_presets
from print_copilot.multicolor import export_project
from print_copilot.orca.install import locate
from print_copilot.orca.presets import PresetLibrary
from print_copilot.serialize import to_jsonable

haku_run, housing_run = (Path(p).resolve() for p in sys.argv[1:3])
root = Path(__file__).resolve().parents[2]
install = locate()
library = PresetLibrary(install.data_dir, system_dir=install.system_dir)
selection = select_presets(library)
variants = json.loads((haku_run / "haku-variants.json").read_text(encoding="utf-8"))
cases = [
    (
        "haku-prime36-review",
        haku_run,
        root / "research/showcase/models/Haku_Dragon_Diorama_(Spirited_Away)_SM.3mf",
        {"plate": 1, "mapping": {1: 0, 2: 1, 3: 2, 4: 3}, "overrides": variants[1]["overrides"]},
    ),
    (
        "housing-flipped-review",
        housing_run,
        housing_run / "housing-flipped.stl",
        {
            "head": 0,
            "overrides": {
                "wall_loops": 3,
                "enable_support": True,
                "support_type": "tree(auto)",
                "support_on_build_plate_only": True,
            },
        },
    ),
]
if len(sys.argv) > 3 and sys.argv[3] == "baseline":
    cases = [
        (
            "haku-reference-review",
            haku_run,
            root / "research/showcase/models/Haku_Dragon_Diorama_(Spirited_Away)_SM.3mf",
            {
                "plate": 1,
                "mapping": {1: 0, 2: 1, 3: 2, 4: 3},
                "overrides": variants[0]["overrides"],
            },
        )
    ]
for name, directory, source, kwargs in cases:
    print(f"Exporting {name}", flush=True)
    output = directory / "exports"
    output.mkdir(exist_ok=True)
    result = export_project(
        install,
        source,
        selection,
        output / f"{name}.3mf",
        work_dir=directory / "export-slices",
        slice_gcode=True,
        **kwargs,
    )
    record = "export-baseline.json" if name == "haku-reference-review" else "export.json"
    (directory / record).write_text(json.dumps(to_jsonable(result), indent=2), encoding="utf-8")
    print(result.path, flush=True)
