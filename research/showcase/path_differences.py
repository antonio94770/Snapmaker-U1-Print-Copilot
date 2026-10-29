"""Retain exact model-path differences without treating near equality as identity."""

import gc
import json
import sys
from collections import Counter
from pathlib import Path

from print_copilot.gcode.paths import read_paths

folder = Path(sys.argv[1]).resolve()
variants = json.loads((folder / "haku-comparison.json").read_text(encoding="utf-8"))["variants"]
counters = []
for variant in variants[:2]:
    print(variant["label"], flush=True)
    paths = read_paths(variant["slice"]["gcode_path"], max_segments=6_000_000)
    counter = Counter()
    for layer in paths.layers:
        for s in layer.segments:
            if "support" in s.feature.lower() or s.feature in {
                "Prime tower",
                "Custom",
                "Brim",
                "Skirt",
            }:
                continue
            counter[
                (layer.number, s.tool, s.feature, *[round(v, 5) for v in (*s.start, *s.end)])
            ] += 1
    counters.append(counter)
    del paths
    gc.collect()
only_baseline, only_candidate = counters[0] - counters[1], counters[1] - counters[0]
result = {
    "baseline_only_count": sum(only_baseline.values()),
    "candidate_only_count": sum(only_candidate.values()),
    "baseline_only": list(only_baseline.items())[:100],
    "candidate_only": list(only_candidate.items())[:100],
    "coordinate_rounding_mm": 0.00001,
}
(folder / "geometry-differences.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
