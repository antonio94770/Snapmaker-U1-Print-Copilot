"""Multi-colour projects on the U1: inspect a 3MF, assign its filaments to
tool heads, slice one plate and compare tool-change strategies.

Shared by the CLI and the MCP server, like `actions.py`. Every number in the
results comes from the slicer's G-code; head assignment and the notes about
print quality are heuristics and say so.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from print_copilot.actions import CONF_SOURCE, Selection, Variant, bed_size
from print_copilot.errors import GcodeParseError, PrintCopilotError, SliceError
from print_copilot.gcode.features import feature_extrusion
from print_copilot.gcode.header import read_config_block
from print_copilot.gcode.toolchanges import ToolChangeStats, grams_per_mm, to_grams, tool_changes
from print_copilot.model.footprint import free_rectangle, occupancy
from print_copilot.model.mesh import load_3mf_items, load_mesh
from print_copilot.orca.conf import current_setup, default_conf_path
from print_copilot.orca.heads import (
    CLEARLY_DIFFERENT_DELTA_E,
    COLOUR_RE,
    Head,
    HeadAssignment,
    Wanted,
    assign_heads,
    delta_e,
)
from print_copilot.orca.install import OrcaInstall, cli_path, to_windows_path
from print_copilot.orca.project import (
    ProjectInfo,
    is_project,
    project_settings,
    read_project,
    read_project_settings,
    write_app_project,
    write_model_project,
    write_slicing_copy,
)
from print_copilot.orca.slicer import (
    BED_TYPES,
    DEFAULT_TIMEOUT_S,
    OverrideValue,
    SettingsOverrides,
    SliceRun,
    default_export_dir,
    default_work_dir,
    prepare_cli_settings,
    slice_model,
)
from print_copilot.printer.moonraker import MoonrakerClient
from print_copilot.printer.status import HeadStatus, read_status
from print_copilot.serialize import to_jsonable

# `--arrange 0` keeps the build positions; without it the CLI re-arranges the
# objects (verified on 01.10.01.50).
PROJECT_CLI_ARGS: tuple[str, ...] = ("--arrange", "0")
# Colour for a head when neither Snapmaker_Orca.conf nor the project gives one.
# Only the number of colours matters to the slicer (see slicer.FILAMENT_COLOUR_KEY).
PLACEHOLDER_COLOUR = "#808080"

# Tool-change strategies compared by default. Values checked in the G-code
# config block of real slices on 01.10.01.50.
U1_VARIANTS: tuple[Variant, ...] = (
    Variant({}, "presets as is"),
    Variant({"enable_prime_tower": False}, "no prime tower"),
    Variant({"print_sequence": "by object"}, "one object at a time"),
)
NO_PRIME_TOWER_NOTE = (
    "Without a prime tower each new tool is only primed by the firmware macro "
    "SM_PRINT_PREEXTRUDE_FILAMENT; ooze and colour bleed on the part are not measured "
    "(heuristic risk, check a test print)."
)
BY_OBJECT_NOTE = (
    "One object at a time needs the print head to clear finished objects; the slicer "
    "checks the clearance (extruder_clearance_radius / _height) and fails if it is too small."
)


RFID_SOURCE = "RFID"


class HeadChoiceRequired(SliceError):
    """A one-colour plate and no head given: the user has to say which spool to use."""


def _preset_type(preset) -> str | None:
    types = preset.settings.get("filament_type")
    kind = types[0] if isinstance(types, list) and types else types
    return kind or None


def heads_for(selection: Selection) -> list[Head]:
    """What is loaded on each head: preset name, colour and type."""
    heads = []
    for index, preset in enumerate(selection.filaments):
        colour = selection.colours[index] if selection.colours else None
        heads.append(
            Head(index=index, filament=preset.name, colour=colour, type=_preset_type(preset))
        )
    return heads


def use_printer_spools(selection: Selection, heads: Sequence[HeadStatus]) -> Selection:
    """The colour of each head whose RFID reader identified a spool, in place
    of the colour stored in Snapmaker_Orca.conf: the printer knows what is
    loaded, the app only what was last selected. Heads without a tag keep the
    stored colour, and a note says so. RFID entry i is head T<i> (verified on
    the U1 on 2026-10-28: black spool read on T1, the T1 print came out black)."""
    count = len(selection.filaments)
    colours: list[str | None] = list(selection.colours or [None] * count)
    sources: list[str | None] = list(selection.colour_sources or [None] * count)
    notes = []
    for head in heads:
        spool = head.rfid_filament
        if head.index >= count or spool is None or not COLOUR_RE.match(spool.colour or ""):
            continue
        before = colours[head.index]
        if before and delta_e(before, spool.colour) > CLEARLY_DIFFERENT_DELTA_E:
            notes.append(
                f"T{head.index}: Snapmaker Orca has {before} selected but the RFID reader "
                f"identified {spool.colour} ({spool.type} {spool.sub_type}); using the RFID colour."
            )
        preset_type = _preset_type(selection.filaments[head.index])
        if preset_type and spool.type and preset_type.upper() != spool.type.upper():
            notes.append(
                f"T{head.index}: the filament preset is {preset_type} "
                f"({selection.filaments[head.index].name}) but the spool is {spool.type}."
            )
        colours[head.index], sources[head.index] = spool.colour, RFID_SOURCE
    untagged = [f"T{i}" for i in range(count) if sources[i] != RFID_SOURCE]
    if untagged:
        notes.append(
            f"{', '.join(untagged)}: no RFID tag read; their colours are the ones selected in "
            f"Snapmaker Orca ({CONF_SOURCE}), not confirmed by the printer."
        )
    known = any(colours)
    return dataclasses.replace(
        selection,
        colours=colours if known else None,  # type: ignore[arg-type]
        colour_sources=sources if known else None,  # type: ignore[arg-type]
        notes=list(selection.notes) + notes,
    )


def _printer_heads() -> list[HeadStatus]:
    return read_status(MoonrakerClient()).heads


def with_printer_spools(
    selection: Selection, read_heads: Callable[[], list[HeadStatus]] | None = None
) -> Selection:
    """`use_printer_spools` with the heads read from the U1. When the printer
    cannot be reached the stored colours are kept and a note says so."""
    try:
        heads = (read_heads or _printer_heads)()
    except PrintCopilotError as exc:
        note = (
            f"Printer not reachable ({exc}); the loaded colours are the ones selected in "
            f"Snapmaker Orca ({CONF_SOURCE}), not what the printer reports."
        )
        return dataclasses.replace(selection, notes=[*selection.notes, note])
    return use_printer_spools(selection, heads)


def loaded_heads_text(selection: Selection) -> str:
    """One line per head: colour, type, preset and where the colour came from."""
    sources = selection.colour_sources or [None] * len(selection.filaments)
    parts = []
    for head in heads_for(selection):
        kind = f" {head.type}" if head.type else ""
        parts.append(
            f"T{head.index} {head.colour or 'colour unknown'}{kind} "
            f"({head.filament}; colour from {sources[head.index] or 'nowhere'})"
        )
    return "; ".join(parts)


def wanted_filaments(project: ProjectInfo, plate: int) -> list[Wanted]:
    result = []
    for number in project.plate_filaments(plate):
        info = project.filament(number)
        colour = info.colour[:7] if info and info.colour else None
        result.append(Wanted(number, colour, info.type if info else None))
    return result


def object_settings(project: ProjectInfo, plate: int) -> dict[str, dict[str, dict[str, str]]]:
    """Process settings the project sets on the plate's objects and parts:
    object name -> "object" or part -> {key: value}."""
    found = {}
    for obj in project.plate_objects(plate):
        overrides = obj.setting_overrides()
        if overrides:
            found[obj.name or obj.id] = overrides
    return found


def object_settings_notes(settings: dict[str, dict[str, dict[str, str]]]) -> list[str]:
    """One note per object that overrides process settings: the slicer uses
    these values for that object, and the G-code header shows only the
    process values."""
    notes = []
    for name, places in settings.items():
        listed = "; ".join(
            f"{place}: " + ", ".join(f"{k}={v}" for k, v in sorted(values.items()))
            for place, values in places.items()
        )
        notes.append(
            f"Object {name!r} sets its own settings, which override the process preset for "
            f"it ({listed}). The G-code header lists only the process values."
        )
    return notes


# Process settings compared between the project and the preset it is sliced
# with: the ones that change how the print looks, holds or how long it takes.
COMPARED_SETTINGS: tuple[str, ...] = (
    "layer_height",
    "initial_layer_print_height",
    "wall_loops",
    "top_shell_layers",
    "bottom_shell_layers",
    "sparse_infill_density",
    "sparse_infill_pattern",
    "enable_support",
    "support_type",
    "support_threshold_angle",
    "support_on_build_plate_only",
    "brim_type",
    "seam_position",
    "ironing_type",
)


@dataclass
class SettingDifference:
    key: str
    project: str  # value saved in the project
    used: str  # value of the process preset, with overrides


def _setting_text(value: object) -> str:
    if isinstance(value, bool):  # an override; presets store "0" / "1"
        return "1" if value else "0"
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value)


def _same_setting(a: str, b: str) -> bool:
    try:
        return float(a.rstrip("%")) == float(b.rstrip("%")) and a.endswith("%") == b.endswith("%")
    except ValueError:
        return a == b


def setting_differences(
    source: str | os.PathLike[str],
    selection: Selection,
    overrides: dict[str, OverrideValue] | None = None,
) -> list[SettingDifference]:
    """`COMPARED_SETTINGS` whose value saved in the project differs from the
    process preset it is sliced with (after process overrides). Empty for a
    file that stores no settings, such as an STL."""
    saved = read_project_settings(source)
    used = dict(selection.process.settings)
    for key, value in (overrides or {}).items():
        if not key.startswith(("machine.", "filament.")):
            used[key.removeprefix("process.")] = value
    found = []
    for key in COMPARED_SETTINGS:
        if key not in saved or key not in used:
            continue
        project, mine = _setting_text(saved[key]), _setting_text(used[key])
        if not _same_setting(project, mine):
            found.append(SettingDifference(key, project, mine))
    return found


def setting_differences_note(differences: list[SettingDifference], process: str) -> str | None:
    if not differences:
        return None
    listed = ", ".join(f"{d.key} {d.project} -> {d.used}" for d in differences)
    return (
        f"The project was saved with settings that differ from the process {process!r} used "
        f"here (project -> used): {listed}. Tell the user before slicing; pass overrides to "
        "keep the project's values."
    )


def plate_sizes(
    project_file: str | os.PathLike[str], project: ProjectInfo, plate: int
) -> tuple[dict[str, list[float]], list[float] | None]:
    """Size in mm (X, Y, Z) of each object of the plate, as placed, and of
    everything on the plate together."""
    ids = set(project.plate(plate).object_ids)
    names = {o.id: o.name or o.id for o in project.objects}
    sizes: dict[str, list[float]] = {}
    lows, highs = [], []
    for object_id, triangles in load_3mf_items(project_file):
        if object_id not in ids or not len(triangles):
            continue
        points = triangles.reshape(-1, 3)
        low, high = points.min(axis=0), points.max(axis=0)
        sizes[names.get(object_id, object_id)] = [round(float(v), 1) for v in high - low]
        lows.append(low)
        highs.append(high)
    if not lows:
        return sizes, None
    total = np.max(highs, axis=0) - np.min(lows, axis=0)
    return sizes, [round(float(v), 1) for v in total]


@dataclass
class PlateSummary:
    number: int
    name: str | None
    objects: list[str]
    filaments: list[Wanted]
    assignment: HeadAssignment | None
    assignment_error: str | None
    # Process settings set on objects or parts: object -> place -> {key: value}.
    object_settings: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    # Measured from the mesh as placed in the project, X Y Z in mm.
    object_sizes_mm: dict[str, list[float]] = field(default_factory=dict)
    size_mm: list[float] | None = None
    fits_bed: bool | None = None  # None without a printer selection


@dataclass
class ProjectSummary:
    project: Path
    application: str | None
    printer: str | None  # printer the project was saved for
    filament_count: int
    plates: list[PlateSummary]
    heads: list[Head]
    notes: list[str] = field(default_factory=list)
    bed_mm: list[float] | None = None  # printable X Y Z of the selected printer
    setting_differences: list[SettingDifference] = field(default_factory=list)


def inspect_project(
    model: str | os.PathLike[str],
    selection: Selection | None = None,
    *,
    reduce_colours: bool = False,
) -> ProjectSummary:
    """Plates, objects and filaments of a 3MF project (or an STL, read as a
    one-object project) and, with a selection, the proposed head for every
    filament of each plate (no slicing)."""
    with tempfile.TemporaryDirectory(prefix="print-copilot-inspect-") as folder:
        source, wrap_notes = _as_project(model, Path(folder))
        summary = _inspect(source, selection, reduce_colours)
    summary.project = Path(model)
    summary.notes[:0] = wrap_notes
    return summary


def _inspect(model: Path, selection: Selection | None, reduce_colours: bool) -> ProjectSummary:
    project = read_project(model)
    heads = heads_for(selection) if selection else []
    bed = bed_size(selection.machine) if selection else None
    plates = []
    notes = list(project.notes)
    for plate in project.plates:
        settings = object_settings(project, plate.number)
        notes.extend(object_settings_notes(settings))
        sizes, size = plate_sizes(model, project, plate.number)
        fits = None
        if bed and size:
            fits = all(s <= b for s, b in zip(size, bed, strict=True))
            if not fits:
                notes.append(
                    f"Plate {plate.number} measures {size[0]} x {size[1]} x {size[2]} mm, larger "
                    f"than the printable {bed[0]:g} x {bed[1]:g} x {bed[2]:g} mm of "
                    f"{selection.machine.name!r} (as placed; rotating or scaling may help)."
                )
        wanted = wanted_filaments(project, plate.number)
        assignment, error = None, None
        if heads:
            try:
                assignment = assign_heads(wanted, heads, reduce_to_heads=reduce_colours)
            except SliceError as exc:
                error = str(exc)
        plates.append(
            PlateSummary(
                number=plate.number,
                name=plate.name,
                objects=[o.name or o.id for o in project.plate_objects(plate.number)],
                filaments=wanted,
                assignment=assignment,
                assignment_error=error,
                object_settings=settings,
                object_sizes_mm=sizes,
                size_mm=size,
                fits_bed=fits,
            )
        )
    differences = setting_differences(model, selection) if selection else []
    note = setting_differences_note(differences, selection.process.name) if selection else None
    if note:
        notes.append(note)
    if selection and not selection.colours:
        notes.append(
            "The loaded colours are unknown (neither Snapmaker_Orca.conf nor the printer gave "
            "them); heads are matched by filament type only."
        )
    single = [p.number for p in plates if len(p.filaments) == 1]
    if single:
        notes.append(
            f"Plate(s) {single} print in one colour: the proposed head is only the nearest "
            "colour; ask the user which loaded spool to use before slicing (head / --head)."
        )
    return ProjectSummary(
        project=project.path,
        application=project.application,
        printer=project.printer,
        filament_count=len(project.filaments),
        plates=plates,
        heads=heads,
        notes=notes,
        bed_mm=list(bed) if bed else None,
        setting_differences=differences,
    )


@dataclass
class SupportSummary:
    """How the plate is supported. Settings come from the process preset with
    overrides and from the objects' own settings; grams are measured from the
    `Support*` toolpaths and converted with the G-code's overall g/mm ratio."""

    enabled_in_process: bool
    support_type: str | None
    threshold_deg: str | None
    enabled_by_objects: list[str]  # objects that turn support on themselves
    support_g: float
    support_interface_g: float


@dataclass
class HeadUsage:
    """Measured per head. Grams use the head's own g/mm ratio from the header."""

    head: int
    filament: str
    colour: str | None
    project_filaments: list[int]
    total_g: float
    prime_tower_g: float
    purge_g: float


@dataclass
class ProjectSlice:
    project: Path
    plate: int
    assignment: HeadAssignment
    slicing_copy: Path
    machine: str
    process: str
    estimated_time_s: int
    total_filament_g: float
    tool_changes: int
    layers_with_changes: int
    prime_tower_g: float
    purge_g: float
    heads: list[HeadUsage]
    toolpath: ToolChangeStats
    overrides: dict[str, OverrideValue]
    gcode_path: Path
    run_dir: Path
    notes: list[str] = field(default_factory=list)
    support: SupportSummary | None = None


def _plate_number(project: ProjectInfo, plate: int | None) -> int:
    if plate is not None:
        project.plate(plate)
        return plate
    if len(project.plates) == 1:
        return project.plates[0].number
    raise SliceError(
        f"{project.path.name} has {len(project.plates)} plates "
        f"({[p.number for p in project.plates]}); choose one."
    )


def _head_colours(selection: Selection, assignment: HeadAssignment) -> list[str]:
    colours = list(selection.colours or [None] * len(selection.filaments))
    for choice in assignment.choices:
        if not colours[choice.head] and choice.project_colour:
            colours[choice.head] = choice.project_colour
    return [c or PLACEHOLDER_COLOUR for c in colours]


def _bed(selection: Selection) -> tuple[tuple[float, float, float] | None, tuple[float, float]]:
    size = bed_size(selection.machine)
    area = selection.machine.settings.get("printable_area")
    try:
        xs = [float(p.split("x")[0]) for p in area or []]
        ys = [float(p.split("x")[1]) for p in area or []]
        origin = (min(xs), min(ys))
    except (IndexError, ValueError):
        origin = (0.0, 0.0)
    return size, origin


@dataclass
class _Prepared:
    project: ProjectInfo
    plate: int
    assignment: HeadAssignment
    copy: Path
    colours: list[str]
    notes: list[str]
    bounds_mm: list[list[float]]
    bed: tuple[tuple[float, float, float] | None, tuple[float, float]]


# The slicer refuses a plate whose prime tower overlaps an object:
# "gcode path conflicts found between WipeTower and <object>" (01.10.01.50).
TOWER_CONFLICT = "conflicts found between WipeTower"
# Room kept free for the tower around its position: the tower width plus brim
# on each side and this margin, and twice that in Y because the depth grows
# with the purge volume. A heuristic; the slicer's own conflict check decides.
TOWER_MARGIN_MM = 5.0
TOWER_CELL_MM = 2.0


def _setting_mm(selection: Selection, key: str, default: float) -> float:
    try:
        return float(selection.process.settings.get(key, default))
    except (TypeError, ValueError):
        return default


def tower_position(prepared: _Prepared, selection: Selection) -> tuple[float, float] | None:
    """A spot, nearest a bed corner, where the prime tower stays clear of the
    footprint of the plate's objects (see `TOWER_MARGIN_MM`)."""
    size, origin = prepared.bed
    if size is None:
        return None
    brim = _setting_mm(selection, "prime_tower_brim_width", 5.0)
    width = _setting_mm(selection, "prime_tower_width", 30.0) + 2 * brim
    grid = occupancy(load_mesh(prepared.copy).triangles, origin, size[:2], TOWER_CELL_MM)
    spot = free_rectangle(
        grid,
        int(np.ceil(width / TOWER_CELL_MM)),
        int(np.ceil(2 * width / TOWER_CELL_MM)),
        int(np.ceil(TOWER_MARGIN_MM / TOWER_CELL_MM)),
    )
    if spot is None:
        return None
    # wipe_tower_x/y place the tower itself; the brim lies around it.
    return (
        round(origin[0] + spot[0] * TOWER_CELL_MM + brim, 1),
        round(origin[1] + spot[1] * TOWER_CELL_MM + brim, 1),
    )


def _as_project(model: str | os.PathLike[str], folder: Path) -> tuple[Path, list[str]]:
    """The model itself when it is a slicer project; otherwise (an STL, or a
    3MF without slicer metadata) a one-object project wrapping it."""
    path = Path(model)
    if path.suffix.lower() == ".3mf" and is_project(path):
        return path, []
    mesh = load_mesh(path)
    wrapped = write_model_project(
        mesh.triangles, folder / f"{path.stem}-project.3mf", name=path.stem, filament_type=None
    )
    return wrapped, [
        f"{path.name} is a plain model with no colour; it is handled as a one-object, "
        "one-filament project."
    ]


def _head_choice_message(wanted: Wanted, selection: Selection) -> str:
    hint = ""
    coloured = [h for h in heads_for(selection) if h.colour]
    if wanted.colour and coloured:
        near = min(coloured, key=lambda h: delta_e(wanted.colour, h.colour))  # type: ignore[arg-type]
        hint = (
            f" The model asks for {wanted.colour}; the nearest loaded colour is on "
            f"T{near.index} ({near.colour})."
        )
    return (
        "This plate prints in one colour, so the head is not chosen automatically: ask the "
        "user which loaded spool to print with and pass head (CLI: --head T<n>)."
        f"{hint} Loaded: {loaded_heads_text(selection)}."
    )


def _prepare(
    model: str | os.PathLike[str],
    selection: Selection,
    plate: int | None,
    mapping: dict[int, int] | None,
    folder: Path,
    reduce_colours: bool = False,
    head: int | None = None,
    overrides: dict[str, OverrideValue] | None = None,
) -> _Prepared:
    source, wrap_notes = _as_project(model, folder)
    project = read_project(source)
    number = _plate_number(project, plate)
    wanted = wanted_filaments(project, number)
    if head is not None:
        if mapping:
            raise SliceError("Give either head (one head for the whole plate) or mapping.")
        mapping = {w.number: head for w in wanted}
    elif not mapping and len(wanted) == 1:
        raise HeadChoiceRequired(_head_choice_message(wanted[0], selection))
    assignment = assign_heads(
        wanted,
        heads_for(selection),
        mapping,
        reduce_to_heads=reduce_colours,
    )
    size, origin = _bed(selection)
    copy = write_slicing_copy(
        project,
        number,
        assignment.mapping(),
        folder / f"plate{number}-cli.3mf",
        bed_mm=size,
        bed_origin_mm=origin,
    )
    notes = wrap_notes + list(project.notes) + assignment.notes + copy.notes
    notes += object_settings_notes(object_settings(project, number))
    differences = setting_differences(source, selection, overrides)
    note = setting_differences_note(differences, selection.process.name)
    if note:
        notes.append(note)
    if size is None:
        notes.append(f"{selection.machine.name!r} has no printable area; bed fit unchecked.")
    if project.printer and project.printer != selection.machine.name:
        notes.append(
            f"The project was saved for {project.printer!r}; it is sliced with "
            f"{selection.machine.name!r} and its own presets are not used."
        )
    colours = _head_colours(selection, assignment)
    return _Prepared(
        project, number, assignment, copy.path, colours, notes, copy.bounds_mm, (size, origin)
    )


def _measure(run: SliceRun, assignment: HeadAssignment, selection: Selection, colours) -> tuple:
    stats = tool_changes(run.gcode_path)
    ratios = grams_per_mm(run.result)
    heads = []
    for index, preset in enumerate(selection.filaments):
        used = [c.filament for c in assignment.choices if c.head == index]
        grams = run.result.filament_used_g[index] if index < len(run.result.filament_used_g) else 0
        if not used and grams == 0:
            continue
        heads.append(
            HeadUsage(
                head=index,
                filament=preset.name,
                colour=colours[index],
                project_filaments=used,
                total_g=grams,
                prime_tower_g=to_grams({index: stats.prime_tower_mm.get(index, 0.0)}, ratios),
                purge_g=to_grams({index: stats.purge_mm.get(index, 0.0)}, ratios),
            )
        )
    notes = []
    unexpected = [h.head for h in heads if not h.project_filaments and h.total_g > 0]
    if unexpected:
        notes.append(
            f"Head(s) {['T' + str(h) for h in unexpected]} extrude although no project filament "
            "is assigned to them (support or prime tower settings of the process preset)."
        )
    missing = [h.head for h in heads if h.project_filaments and h.total_g == 0]
    if missing:
        raise SliceError(
            f"Head(s) {['T' + str(h) for h in missing]} are assigned filaments but the G-code "
            f"uses none of them, so the slice ignored the assignment. Run dir: {run.run_dir}"
        )
    if stats.preextrude_macro_calls:
        notes.append(
            f"The G-code calls SM_PRINT_PREEXTRUDE_FILAMENT {stats.preextrude_macro_calls} times; "
            "what the firmware extrudes there is not included (TO VERIFY ON HARDWARE)."
        )
    return stats, ratios, heads, notes


def slice_project(
    install: OrcaInstall,
    model: str | os.PathLike[str],
    selection: Selection,
    *,
    plate: int | None = None,
    mapping: dict[int, int] | None = None,
    reduce_colours: bool = False,
    overrides: dict[str, OverrideValue] | None = None,
    work_dir: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    head: int | None = None,
    bed_type: str | None = None,
) -> ProjectSlice:
    """Slice one plate of a 3MF project on the selected printer.

    Filaments are assigned to heads with `assign_heads` (`mapping` fixes some
    or all: project filament number -> head index, 0 = T0; `reduce_colours`
    merges the nearest colours when there are more than heads; `head` puts the
    whole plate on one head). A one-colour plate needs `head` or `mapping`
    (`HeadChoiceRequired`). An STL or plain 3MF is handled as a one-object
    project. A copy the CLI can read is written into a fresh folder and sliced
    with `--arrange 0`; tool changes, prime tower and purge are measured from
    the G-code. `bed_type` is the plate to slice for (`slicer.BED_TYPES`); without
    it the slicer uses its own default, not the plate selected in the app.
    """
    base = to_windows_path(work_dir) if work_dir else default_work_dir()
    folder = base / f"project-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    folder.mkdir(parents=True)
    prepared = _prepare(model, selection, plate, mapping, folder, reduce_colours, head, overrides)
    return _slice_prepared(install, prepared, selection, overrides, folder, timeout_s, bed_type)


def _support_summary(run: SliceRun, project: ProjectInfo, plate: int) -> SupportSummary:
    features = feature_extrusion(run.gcode_path)
    total_mm = sum(run.result.filament_used_mm)
    ratio = run.result.total_filament_g / total_mm if total_mm > 0 else 0.0
    settings = run.result.settings
    return SupportSummary(
        enabled_in_process=settings.get("enable_support") == "1",
        support_type=settings.get("support_type"),
        threshold_deg=settings.get("support_threshold_angle"),
        enabled_by_objects=[
            o.name or o.id
            for o in project.plate_objects(plate)
            if o.overrides.get("enable_support") == "1"
        ],
        support_g=round(features.support_mm * ratio, 2),
        support_interface_g=round(features.feature_mm("Support interface") * ratio, 2),
    )


def support_notes(support: SupportSummary) -> list[str]:
    """Say where the supports come from when the process setting alone would
    mislead: printed while the process turns them off, or on without any."""
    printed = support.support_g > 0
    if printed and not support.enabled_in_process:
        source = (
            f"object(s) {support.enabled_by_objects} turn them on with their own settings"
            if support.enabled_by_objects
            else "support enforcers or painted support in the project"
        )
        return [
            f"Supports are printed ({support.support_g} g measured) although the process has "
            f"enable_support off: {source}."
        ]
    if not printed and (support.enabled_in_process or support.enabled_by_objects):
        return [
            "Support is on, but the slicer generated none: no overhang is flatter than the "
            f"support threshold ({support.threshold_deg} degrees from horizontal)."
        ]
    return []


def _slice_prepared(
    install: OrcaInstall,
    prepared: _Prepared,
    selection: Selection,
    overrides: dict[str, OverrideValue] | None,
    folder: Path,
    timeout_s: float,
    bed_type: str | None = None,
) -> ProjectSlice:
    def run_once(extra: dict[str, list[str]] | None) -> SliceRun:
        return slice_model(
            install,
            prepared.copy,
            selection.machine,
            selection.process,
            selection.filaments,
            work_dir=folder,
            timeout_s=timeout_s,
            overrides=SettingsOverrides.from_flat(overrides),
            filament_colours=prepared.colours,
            extra_args=list(PROJECT_CLI_ARGS),
            process_extra=extra,
            bed_type=bed_type,
        )

    tower_notes = []
    try:
        run = run_once(None)
    except SliceError as exc:
        position = tower_position(prepared, selection) if TOWER_CONFLICT in str(exc) else None
        if position is None:
            raise
        run = run_once({"wipe_tower_x": [str(position[0])], "wipe_tower_y": [str(position[1])]})
        tower_notes.append(
            f"The prime tower collided with an object at its default position, so it was "
            f"moved to X {position[0]:g}, Y {position[1]:g} (first error: {str(exc)[:200]})."
        )
    stats, ratios, heads, notes = _measure(run, prepared.assignment, selection, prepared.colours)
    try:
        support = _support_summary(run, prepared.project, prepared.plate)
        notes += support_notes(support)
    except GcodeParseError as exc:
        support = None
        notes.append(f"Supports not measured: {exc}")
    notes = tower_notes + notes
    if bed_type is None:
        notes.append(
            "No bed type was given: the slicer used its own default "
            f"({read_config_block(run.gcode_path).get('curr_bed_type')!r}), "
            "not the plate selected in the app. Pass bed_type for the plate on the printer."
        )
    result = ProjectSlice(
        project=prepared.project.path,
        plate=prepared.plate,
        assignment=prepared.assignment,
        slicing_copy=prepared.copy,
        machine=run.machine,
        process=run.process,
        estimated_time_s=run.result.estimated_time_s,
        total_filament_g=run.result.total_filament_g,
        tool_changes=stats.tool_changes,
        layers_with_changes=stats.layers_with_changes,
        prime_tower_g=to_grams(stats.prime_tower_mm, ratios),
        purge_g=to_grams(stats.purge_mm, ratios),
        heads=heads,
        toolpath=stats,
        overrides=run.overrides,
        gcode_path=run.gcode_path,
        run_dir=run.run_dir,
        notes=prepared.notes + notes,
        support=support,
    )
    (run.run_dir / "project_slice.json").write_text(
        json.dumps(to_jsonable(result), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


@dataclass
class StrategyResult:
    label: str
    overrides: dict[str, OverrideValue]
    slice: ProjectSlice | None
    error: str | None  # why the slicer refused this variant
    time_delta_s: int | None  # against the first variant that sliced
    filament_delta_g: float | None


@dataclass
class StrategyComparison:
    project: Path
    plate: int
    assignment: HeadAssignment
    variants: list[StrategyResult]
    fastest: str | None
    least_filament: str | None
    work_dir: Path
    notes: list[str] = field(default_factory=list)


def compare_strategies(
    install: OrcaInstall,
    model: str | os.PathLike[str],
    selection: Selection,
    *,
    plate: int | None = None,
    mapping: dict[int, int] | None = None,
    reduce_colours: bool = False,
    variants: list[Variant] | None = None,
    work_dir: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    head: int | None = None,
) -> StrategyComparison:
    """Slice one plate once per variant (default `U1_VARIANTS`) with the same
    head assignment and compare time, filament, tool changes and prime tower.

    Overrides are checked before the first slice. A variant the slicer refuses
    (for example one object at a time without enough clearance) is reported
    with its error instead of stopping the comparison.
    """
    variants = list(variants) if variants else list(U1_VARIANTS)
    for variant in variants:
        try:
            prepare_cli_settings(
                selection.machine,
                selection.process,
                selection.filaments,
                SettingsOverrides.from_flat(variant.overrides),
            )
        except SliceError as exc:
            raise SliceError(f"Variant {variant.display_label()!r}: {exc}") from exc

    base = to_windows_path(work_dir) if work_dir else default_work_dir()
    folder = base / f"strategies-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    folder.mkdir(parents=True)
    prepared = _prepare(model, selection, plate, mapping, folder, reduce_colours, head)
    if len(prepared.project.plate_objects(prepared.plate)) < 2:
        variants = [v for v in variants if v.overrides.get("print_sequence") != "by object"]

    results: list[StrategyResult] = []
    for variant in variants:
        try:
            sliced = _slice_prepared(
                install, prepared, selection, variant.overrides, folder, timeout_s
            )
        except SliceError as exc:
            results.append(
                StrategyResult(
                    variant.display_label(), variant.overrides, None, str(exc), None, None
                )
            )
            continue
        base_slice = next((r.slice for r in results if r.slice), None)
        results.append(
            StrategyResult(
                label=variant.display_label(),
                overrides=sliced.overrides,
                slice=sliced,
                error=None,
                time_delta_s=sliced.estimated_time_s - base_slice.estimated_time_s
                if base_slice
                else 0,
                filament_delta_g=round(sliced.total_filament_g - base_slice.total_filament_g, 2)
                if base_slice
                else 0.0,
            )
        )
    done = [r for r in results if r.slice]
    if not done:
        raise SliceError(
            "Every variant failed:\n" + "\n".join(f"- {r.label}: {r.error}" for r in results)
        )
    notes = list(prepared.notes)
    if any(v.overrides.get("enable_prime_tower") is False for v in variants):
        notes.append(NO_PRIME_TOWER_NOTE)
    if any(v.overrides.get("print_sequence") == "by object" for v in variants):
        notes.append(BY_OBJECT_NOTE)
    comparison = StrategyComparison(
        project=prepared.project.path,
        plate=prepared.plate,
        assignment=prepared.assignment,
        variants=results,
        fastest=min(done, key=lambda r: r.slice.estimated_time_s).label,  # type: ignore[union-attr]
        least_filament=min(done, key=lambda r: r.slice.total_filament_g).label,  # type: ignore[union-attr]
        work_dir=folder,
        notes=notes,
    )
    (folder / "strategies.json").write_text(
        json.dumps(to_jsonable(comparison), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return comparison


# Written into the project's `Application` metadata when the app version is
# unknown. The loader treats `Snapmaker_Orca-<version>` as its own project
# format (bbs_3mf.cpp, v2.4.0).
APPLICATION_PREFIX = "Snapmaker_Orca-"


@dataclass
class ExportedProject:
    path: Path
    source: Path
    plate: int
    assignment: HeadAssignment
    machine: str
    process: str
    filaments: list[str]
    overrides: dict[str, OverrideValue]
    notes: list[str] = field(default_factory=list)
    # With `slice_gcode`: the G-code sliced from the written project, copied
    # next to it, and that slice's measured figures.
    gcode_path: Path | None = None
    slice: ProjectSlice | None = None
    # The plate (`curr_bed_type`) the G-code was sliced for, read back from its config
    # block; it sets the bed temperature the printer will use.
    gcode_bed_type: str | None = None


def export_project(
    install: OrcaInstall,
    model: str | os.PathLike[str],
    selection: Selection,
    out_path: str | os.PathLike[str] | None = None,
    *,
    plate: int | None = None,
    mapping: dict[int, int] | None = None,
    reduce_colours: bool = False,
    overrides: dict[str, OverrideValue] | None = None,
    work_dir: str | os.PathLike[str] | None = None,
    head: int | None = None,
    slice_gcode: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    bed_type: str | None = None,
) -> ExportedProject:
    """Write one plate of a project (or an STL) as a new 3MF for Snapmaker
    Orca: objects centred on the bed, filaments assigned to heads like
    `slice_project`, and the selected presets (with `overrides`) as the
    project settings.

    With `slice_gcode` the written project itself is sliced and the G-code is
    copied next to it (same name, `.gcode`), so the file to reopen in the app
    and the file to print come from the same project.

    `out_path` (and with `slice_gcode` the `.gcode` next to it) must not
    exist; the source file is never changed. Without `out_path` the project
    goes into a new folder under `default_export_dir()`.
    """
    source = to_windows_path(model)
    if out_path is None:
        folder = default_export_dir() / f"{source.stem}-{datetime.now():%Y%m%d-%H%M%S}"
        folder.mkdir(parents=True, exist_ok=True)
        out_path = folder / f"{source.stem}.3mf"
    out = to_windows_path(out_path)
    gcode_out = out.with_suffix(".gcode") if slice_gcode else None
    if not out.parent.is_dir():
        raise SliceError(f"Output folder {out.parent} does not exist; create it or choose another.")
    for target in (out, gcode_out):
        if target is not None and target.exists():
            raise SliceError(f"{target} already exists; choose a new file name.")
    if out.resolve() == source.resolve():
        raise SliceError("The output must be a new file, not the source project.")
    base = to_windows_path(work_dir) if work_dir else default_work_dir()
    folder = base / f"export-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    folder.mkdir(parents=True)
    prepared = _prepare(source, selection, plate, mapping, folder, reduce_colours, head, overrides)
    machine_s, process_s, filament_s = prepare_cli_settings(
        selection.machine,
        selection.process,
        selection.filaments,
        SettingsOverrides.from_flat(overrides),
    )
    settings = project_settings(
        machine_s,
        process_s,
        filament_s,
        machine_name=selection.machine.name,
        process_name=selection.process.name,
        filament_names=[f.name for f in selection.filaments],
        colours=prepared.colours,
        bases=[
            selection.process.settings,
            *(f.settings for f in selection.filaments),
            selection.machine.settings,
        ],
    )
    version = None
    with contextlib.suppress(PrintCopilotError):
        version = current_setup(default_conf_path(install.data_dir)).app_version
    notes = list(prepared.notes)
    if not version:
        notes.append("App version unknown; the project is tagged without a version number.")
    write_app_project(
        prepared.copy,
        out,
        application=f"{APPLICATION_PREFIX}{version or '0.0.0'}",
        object_ids=[o.id for o in prepared.project.plate_objects(prepared.plate)],
        settings=settings,
    )
    sliced = None
    gcode_bed_type = None
    if gcode_out is not None:
        # In the written project filament N is printed by head N-1.
        heads_used = {c.head for c in prepared.assignment.choices}
        sliced = slice_project(
            install,
            out,
            selection,
            mapping={h + 1: h for h in heads_used},
            overrides=overrides,
            work_dir=folder,
            timeout_s=timeout_s,
            bed_type=bed_type,
        )
        shutil.copy2(sliced.gcode_path, gcode_out)
        gcode_bed_type = read_config_block(gcode_out).get("curr_bed_type")
        if bed_type is None:
            notes.append(
                f"No bed_type was given: {gcode_out.name} was sliced for "
                f"{gcode_bed_type or 'an unknown plate'}, the slicer's own default, not the plate "
                "selected in the app. The plate sets the bed "
                "temperature on the printer: ask the user which plate is on it and export again "
                f"with bed_type (one of {', '.join(BED_TYPES)})."
            )
        if sliced.support is not None:
            notes += support_notes(sliced.support)
        notes.append(
            f"{gcode_out.name} was sliced from {out.name}: open {out} in Snapmaker Orca to see "
            "or change exactly what this G-code prints."
        )
    return ExportedProject(
        path=out,
        source=source,
        plate=prepared.plate,
        assignment=prepared.assignment,
        machine=selection.machine.name,
        process=selection.process.name,
        filaments=[f.name for f in selection.filaments],
        overrides=SettingsOverrides.from_flat(overrides).to_flat(),
        notes=notes,
        gcode_path=gcode_out,
        slice=sliced,
        gcode_bed_type=gcode_bed_type,
    )


def open_in_app(install: OrcaInstall, project: str | os.PathLike[str]) -> list[str]:
    """Start Snapmaker Orca with `project` and return the command. The app
    runs on its own; nothing waits for it."""
    path = to_windows_path(project)
    if not path.is_file():
        raise SliceError(f"Project file not found: {path}")
    command = [str(install.exe), cli_path(path)]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except OSError as exc:
        raise SliceError(f"Could not start Snapmaker Orca ({install.exe}): {exc}") from exc
    return command
