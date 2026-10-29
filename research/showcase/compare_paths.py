"""Verify model/support extrusion geometry across Haku optimization candidates.

Coordinates and E are rounded to 0.00001 mm to tolerate floating-point
representation. This compares commanded paths, not achieved quality.
"""

from __future__ import annotations

import gc
import hashlib
import json
import math
import struct
import sys
from collections import defaultdict
from pathlib import Path

from print_copilot.gcode.header import read_config_block
from print_copilot.gcode.paths import read_paths

folder = Path(sys.argv[1]).resolve()
comparison = json.loads((folder / "haku-comparison.json").read_text(encoding="utf-8"))
results = []
for variant in comparison["variants"][:2]:
    if variant["slice"] is None:
        continue
    path = Path(variant["slice"]["gcode_path"])
    print(f"Comparing {variant['label']}", flush=True)
    paths = read_paths(path, max_segments=6_000_000)
    # Multiset digest ignores seam/start ordering but includes every endpoint,
    # assigned tool, feature and layer number. Extrusion totals are checked
    # separately because E accumulation can change its final rounding.
    sums = defaultdict(int)
    counts = defaultdict(int)
    totals = defaultdict(float)
    for layer in paths.layers:
        for segment in layer.segments:
            feature = segment.feature
            group = (
                "support"
                if "support" in feature.lower()
                else "other"
                if feature in {"Prime tower", "Custom", "Brim", "Skirt"}
                else "model"
            )
            payload = struct.pack(
                "<ii6d",
                layer.number,
                segment.tool,
                *[round(v, 5) for v in (*segment.start, *segment.end)],
            )
            digest = hashlib.sha256(feature.encode() + payload).digest()
            sums[group] = (sums[group] + int.from_bytes(digest, "big")) % (1 << 256)
            counts[group] += 1
            totals[group] += segment.extrusion_mm
    config = read_config_block(path)
    result = {
        "label": variant["label"],
        "gcode": str(path),
        "path_layers": paths.tagged_layer_count,
        "segments": paths.segment_count,
        "groups": {
            g: {
                "segments": counts[g],
                "extrusion_mm": totals[g],
                "multiset_sha256_sum": f"{sums[g]:064x}",
            }
            for g in counts
        },
        "config": config,
    }
    results.append(result)
    del paths
    gc.collect()
base = results[0]
base_config = base["config"].copy()


def same_group(result, group):
    actual, expected = result["groups"].get(group), base["groups"].get(group)
    if actual is None or expected is None:
        return actual == expected
    return (
        actual["segments"] == expected["segments"]
        and actual["multiset_sha256_sum"] == expected["multiset_sha256_sum"]
        and math.isclose(
            actual["extrusion_mm"], expected["extrusion_mm"], rel_tol=1e-5, abs_tol=0.01
        )
    )


for result in results:
    result["model_paths_equal_baseline"] = same_group(result, "model")
    result["support_paths_equal_baseline"] = same_group(result, "support")
    result["config_changes"] = {
        k: {"baseline": base_config.get(k), "candidate": v}
        for k, v in result["config"].items()
        if v != base_config.get(k)
    }
    del result["config"]
(folder / "path-comparison.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print(
    json.dumps(
        [
            {k: v for k, v in r.items() if k not in {"gcode", "groups", "config_changes"}}
            for r in results
        ],
        indent=2,
    )
)
