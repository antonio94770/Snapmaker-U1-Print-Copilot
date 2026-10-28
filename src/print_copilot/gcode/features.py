"""Measure filament extruded per feature type from the G-code body.

Snapmaker Orca marks every section of the toolpath with a `;TYPE:<feature>`
comment (`Outer wall`, `Sparse infill`, `Support`, `Support interface`, ...;
verified on 01.10.01.50). Summing the positive E of moves that travel in X or Y
gives the filament length each feature uses. Retractions and their matching
unretractions (E moves without X/Y) are left out, so the totals are the
material laid down, not the filament pushed back and forth.

Both relative (`M83`, what the U1 presets use) and absolute (`M82`) extrusion
are handled; `G92 E` resets the absolute position.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from print_copilot.errors import GcodeParseError

_MOVE = re.compile(r"^G[0-3](?:\s|$)")
_AXIS = re.compile(r"([XYEZ])(-?\d*\.?\d+)")
_TYPE_PREFIX = ";TYPE:"
SUPPORT_PREFIX = "Support"


@dataclass
class FeatureExtrusion:
    """Filament length per feature type, in mm of filament."""

    by_feature_mm: dict[str, float] = field(default_factory=dict)

    @property
    def total_mm(self) -> float:
        return round(sum(self.by_feature_mm.values()), 3)

    def feature_mm(self, name: str) -> float:
        return self.by_feature_mm.get(name, 0.0)

    @property
    def support_mm(self) -> float:
        """Every feature whose name starts with `Support` (support, interface, ...)."""
        return round(
            sum(v for k, v in self.by_feature_mm.items() if k.startswith(SUPPORT_PREFIX)), 3
        )


def feature_extrusion_lines(lines) -> FeatureExtrusion:
    totals: dict[str, float] = {}
    feature = "Unmarked"
    relative = False
    last_e = 0.0
    for raw in lines:
        if raw.startswith(";"):
            if raw.startswith(_TYPE_PREFIX):
                feature = raw[len(_TYPE_PREFIX) :].strip() or "Unmarked"
            continue
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("M83"):
            relative = True
            continue
        if line.startswith("M82"):
            relative = False
            continue
        if line.startswith("G92"):
            match = re.search(r"E(-?\d*\.?\d+)", line)
            if match:
                last_e = float(match.group(1))
            continue
        if not _MOVE.match(line):
            continue
        axes = dict(_AXIS.findall(line))
        if "E" not in axes:
            continue
        value = float(axes["E"])
        delta = value if relative else value - last_e
        if not relative:
            last_e = value
        if delta > 0 and ("X" in axes or "Y" in axes):
            totals[feature] = totals.get(feature, 0.0) + delta
    return FeatureExtrusion({k: round(v, 3) for k, v in totals.items()})


def feature_extrusion(path: str | os.PathLike[str]) -> FeatureExtrusion:
    """Parse a G-code file. Raises `GcodeParseError` if it holds no extrusion."""
    gcode_path = Path(path)
    try:
        with gcode_path.open(encoding="utf-8", errors="replace") as handle:
            result = feature_extrusion_lines(handle)
    except OSError as exc:
        raise GcodeParseError(f"Cannot read G-code {gcode_path}: {exc}") from exc
    if not result.by_feature_mm:
        raise GcodeParseError(f"{gcode_path}: no extruding moves found.")
    return result
