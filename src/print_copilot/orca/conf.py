"""Read the current printer / preset selection from `Snapmaker_Orca.conf`.

Structure (read from the real file on Snapmaker Orca 01.10.01.50,
docs/verified-facts.md section 3):

    {
      "app": {"version": "01.10.01.50", "curr_bed_type": "4", ...},
      "presets": {"machine": "Snapmaker U1 (0.4 nozzle)", "filaments": null},
      "orca_presets": [
        {"machine": "...", "process": "...",
         "filament": "...", "filament_01": "...", "filament_02": "...", ...,
         "filament_colors": "#FFFFFF,#000000,...",
         "flush_volumes_matrix": "0.000000|223.000000|...",
         "curr_bed_type": "4"},
        ...
      ],
      ...
    }
    # MD5 checksum <hex>

`orca_presets` holds one entry per machine the user has used; `presets.machine`
is the selected one. The trailing checksum line makes the file invalid JSON,
so only the leading JSON object is parsed.

This module only reads. It never writes the conf file.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from print_copilot.errors import ConfError

CONF_NAME = "Snapmaker_Orca.conf"

_FILAMENT_SLOT = re.compile(r"^filament_(\d{2})$")


@dataclass
class CurrentSetup:
    """The machine, process and per-head filaments selected in Snapmaker Orca."""

    conf_path: Path
    app_version: str | None
    machine: str
    process: str | None
    # Index i is tool head T<i>. `filament` is T0 and `filament_NN` is T<NN>
    # (four colours and filament + filament_01..03 on the U1).
    filaments: list[str]
    filament_colors: list[str]
    # Purge volume in mm³ when switching tools: row = from T<i>, column = to T<j>.
    # Orientation follows upstream OrcaSlicer: TO VERIFY IN SNAPMAKER ORCA.
    flush_volumes_matrix_mm3: list[list[float]] | None
    # Raw value from the conf; the meaning of the numbers is TO VERIFY IN SNAPMAKER ORCA.
    curr_bed_type: str | None
    # Machines that have a stored selection, so another one can be requested.
    known_machines: list[str] = field(default_factory=list)


def default_conf_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / CONF_NAME


def load_conf(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Parse the JSON object at the start of the conf file, ignoring the trailer."""
    conf_path = Path(path)
    try:
        text = conf_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfError(f"Cannot read Snapmaker Orca config {conf_path}: {exc}") from exc
    try:
        data, end = json.JSONDecoder().raw_decode(text.lstrip("﻿"))
    except ValueError as exc:
        raise ConfError(f"{conf_path} does not start with a JSON object: {exc}") from exc
    rest = text[end:].strip()
    if rest and not all(line.startswith("#") for line in rest.splitlines()):
        raise ConfError(f"{conf_path} has unexpected content after the JSON object.")
    if not isinstance(data, dict):
        raise ConfError(f"{conf_path} does not hold a JSON object.")
    return data


def _split(text: Any, sep: str) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return []
    return [part.strip() for part in text.split(sep)]


def parse_flush_matrix(text: Any) -> list[list[float]] | None:
    """`a|b|c|...` (n*n numbers, row-major) to an n-by-n matrix; None if absent."""
    parts = _split(text, "|")
    if not parts:
        return None
    try:
        values = [float(p) for p in parts]
    except ValueError as exc:
        raise ConfError(f"flush_volumes_matrix holds a non-number: {text!r}") from exc
    size = math.isqrt(len(values))
    if size * size != len(values):
        raise ConfError(
            f"flush_volumes_matrix has {len(values)} values, which is not a square number."
        )
    if size == 1:
        # A single-extruder machine stores one placeholder value.
        return None
    return [values[row * size : (row + 1) * size] for row in range(size)]


def _filaments(entry: dict[str, Any]) -> list[str]:
    slots: dict[int, str] = {}
    if isinstance(entry.get("filament"), str):
        slots[0] = entry["filament"]
    for key, value in entry.items():
        match = _FILAMENT_SLOT.match(key)
        if match and isinstance(value, str):
            slots[int(match.group(1))] = value
    if not slots:
        return []
    missing = [i for i in range(max(slots) + 1) if i not in slots]
    if missing:
        raise ConfError(f"Filament selection has gaps at tool heads {missing}: {sorted(slots)}")
    return [slots[i] for i in range(len(slots))]


def current_setup(conf_path: str | os.PathLike[str], machine: str | None = None) -> CurrentSetup:
    """The stored selection for `machine`, or for the selected machine if None."""
    path = Path(conf_path)
    data = load_conf(path)
    app = data.get("app") if isinstance(data.get("app"), dict) else {}
    entries = [e for e in data.get("orca_presets") or [] if isinstance(e, dict)]
    known = [e["machine"] for e in entries if isinstance(e.get("machine"), str)]

    if machine is None:
        selected = (data.get("presets") or {}).get("machine")
        if not isinstance(selected, str) or not selected:
            raise ConfError(f"{path} has no selected machine (presets.machine).")
        machine = selected

    entry = next((e for e in entries if e.get("machine") == machine), None)
    if entry is None:
        raise ConfError(
            f"{path} has no stored preset selection for machine {machine!r}. "
            f"Known machines: {known or 'none'}."
        )

    filaments = _filaments(entry)
    colors = _split(entry.get("filament_colors"), ",")
    bed = entry.get("curr_bed_type", app.get("curr_bed_type"))
    return CurrentSetup(
        conf_path=path,
        app_version=app.get("version"),
        machine=machine,
        process=entry.get("process") or None,
        filaments=filaments,
        filament_colors=colors,
        flush_volumes_matrix_mm3=parse_flush_matrix(entry.get("flush_volumes_matrix")),
        curr_bed_type=str(bed) if bed is not None else None,
        known_machines=known,
    )
