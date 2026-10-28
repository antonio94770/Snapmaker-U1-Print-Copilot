"""Measure tool changes, prime tower and purge from the G-code body.

What a U1 slice looks like (verified on Snapmaker Orca 01.10.01.50 with two
objects on T0 and T2):

- every tool selection is a bare `T<n>` line; the start G-code selects the
  first tool once or more, and each later `T<n>` with a different n is a tool
  change (249 changes counted this way matched the header's
  `; total filament change = 249`);
- the prime tower is printed as `;TYPE:Prime tower`, and the purge after each
  change sits between `; CP TOOLCHANGE WIPE` and `; CP TOOLCHANGE END`;
- after `T<n>` the start of the change G-code calls the firmware macro
  `SM_PRINT_PREEXTRUDE_FILAMENT INDEX=<n>`. What that macro extrudes is not in
  the G-code, so it is missing from every number here: TO VERIFY ON HARDWARE.

Only lengths of filament are measured (positive E on moves in X or Y, like
`features.py`); grams use each head's own g/mm ratio from the header.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from print_copilot.errors import GcodeParseError
from print_copilot.gcode.header import SliceResult

_TOOL = re.compile(r"^T(\d+)\s*(?:;|$)")
_MOVE = re.compile(r"^G[0-3](?:\s|$)")
_AXIS = re.compile(r"([XYEZ])(-?\d*\.?\d+)")
PRIME_TOWER = "Prime tower"
WIPE_START = "; CP TOOLCHANGE WIPE"
WIPE_END = "; CP TOOLCHANGE END"
LAYER_CHANGE = ";LAYER_CHANGE"
PREEXTRUDE_MACRO = "SM_PRINT_PREEXTRUDE_FILAMENT"


@dataclass
class ToolChangeStats:
    """Measured from the toolpath. Keys of the per-tool dicts are tool indexes."""

    tool_changes: int
    transitions: dict[str, int]  # "T0>T2": count
    first_tool: int | None
    tools_used: list[int]  # tools that extrude, in order of first extrusion
    layers_with_changes: int
    extrusion_mm: dict[int, float] = field(default_factory=dict)
    prime_tower_mm: dict[int, float] = field(default_factory=dict)
    purge_mm: dict[int, float] = field(default_factory=dict)  # inside the purge blocks
    preextrude_macro_calls: int = 0

    @property
    def prime_tower_total_mm(self) -> float:
        return round(sum(self.prime_tower_mm.values()), 3)

    @property
    def purge_total_mm(self) -> float:
        return round(sum(self.purge_mm.values()), 3)


def tool_change_lines(lines) -> ToolChangeStats:
    tool: int | None = None
    first: int | None = None
    changes = 0
    transitions: dict[str, int] = {}
    change_layers: set[int] = set()
    layer = 0
    feature = ""
    in_wipe = False
    relative = False
    last_e = 0.0
    extrusion: dict[int, float] = {}
    tower: dict[int, float] = {}
    purge: dict[int, float] = {}
    order: list[int] = []
    macro_calls = 0
    for raw in lines:
        if raw.startswith(";"):
            if raw.startswith(";TYPE:"):
                feature = raw[6:].strip()
            elif raw.startswith(LAYER_CHANGE):
                layer += 1
            elif raw.startswith(WIPE_START):
                in_wipe = True
            elif raw.startswith(WIPE_END):
                in_wipe = False
            continue
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        match = _TOOL.match(line)
        if match:
            new = int(match.group(1))
            if tool is not None and new != tool:
                changes += 1
                key = f"T{tool}>T{new}"
                transitions[key] = transitions.get(key, 0) + 1
                change_layers.add(layer)
            if first is None:
                first = new
            tool = new
            continue
        if line.startswith(PREEXTRUDE_MACRO):
            macro_calls += 1
            continue
        if line.startswith("M83"):
            relative = True
            continue
        if line.startswith("M82"):
            relative = False
            continue
        if line.startswith("G92"):
            reset = re.search(r"E(-?\d*\.?\d+)", line)
            if reset:
                last_e = float(reset.group(1))
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
        if delta <= 0 or not ("X" in axes or "Y" in axes) or tool is None:
            continue
        if tool not in extrusion:
            order.append(tool)
        extrusion[tool] = extrusion.get(tool, 0.0) + delta
        if feature == PRIME_TOWER:
            tower[tool] = tower.get(tool, 0.0) + delta
        if in_wipe:
            purge[tool] = purge.get(tool, 0.0) + delta

    def rounded(values: dict[int, float]) -> dict[int, float]:
        return {k: round(v, 3) for k, v in sorted(values.items())}

    return ToolChangeStats(
        tool_changes=changes,
        transitions=dict(sorted(transitions.items())),
        first_tool=first,
        tools_used=order,
        layers_with_changes=len(change_layers),
        extrusion_mm=rounded(extrusion),
        prime_tower_mm=rounded(tower),
        purge_mm=rounded(purge),
        preextrude_macro_calls=macro_calls,
    )


def tool_changes(path: str | os.PathLike[str]) -> ToolChangeStats:
    """Parse a G-code file. Raises `GcodeParseError` if it selects no tool."""
    gcode_path = Path(path)
    try:
        with gcode_path.open(encoding="utf-8", errors="replace") as handle:
            stats = tool_change_lines(handle)
    except OSError as exc:
        raise GcodeParseError(f"Cannot read G-code {gcode_path}: {exc}") from exc
    if stats.first_tool is None:
        raise GcodeParseError(f"{gcode_path}: no T<n> tool selection found.")
    return stats


def grams_per_mm(result: SliceResult) -> list[float]:
    """Each head's g/mm ratio from the header (0 for an unused head)."""
    ratios = []
    for index, grams in enumerate(result.filament_used_g):
        length = result.filament_used_mm[index] if index < len(result.filament_used_mm) else 0.0
        ratios.append(grams / length if length > 0 else 0.0)
    return ratios


def to_grams(values_mm: dict[int, float], ratios: list[float]) -> float:
    total = sum(mm * (ratios[t] if t < len(ratios) else 0.0) for t, mm in values_mm.items())
    return round(total, 2)
