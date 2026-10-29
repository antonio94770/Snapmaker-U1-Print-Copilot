"""Write an explicitly labelled selected-layer excerpt when full HTML is too large.

Usage: python selected_preview.py GCODE NEW_OUTPUT_DIRECTORY
This does not audit the file or replace the failed complete quality preview.
"""

import dataclasses
import json
import sys
from pathlib import Path

from print_copilot.gcode.paths import read_paths
from print_copilot.quality_preview import preview_layers, write_preview
from print_copilot.serialize import to_jsonable

source, output = Path(sys.argv[1]), Path(sys.argv[2])
output.mkdir(parents=True, exist_ok=False)
paths = read_paths(source, max_segments=6_000_000)
indices = preview_layers(paths)
selected = dataclasses.replace(
    paths,
    layers=[paths.layers[i] for i in indices],
    notes=[
        *paths.notes,
        f"SELECTED-LAYER EXCERPT: {len(indices)} of {len(paths.layers)} layers from {source}. "
        "Original full preview failed the 100 MB limit; this is not a complete quality review.",
    ],
)
preview = write_preview(selected, output)
record = to_jsonable(preview)
record["original_layer_count"] = len(paths.layers)
record["original_gcode"] = str(source)
(output / "excerpt.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
print(json.dumps(record, indent=2))
