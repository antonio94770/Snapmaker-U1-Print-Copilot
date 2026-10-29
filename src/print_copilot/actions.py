"""Features shared by the CLI and the MCP server.

Both front ends are thin: they parse their input, call one function here and
format the returned dataclass.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from print_copilot.errors import ModelError, PrintCopilotError, SliceError
from print_copilot.gcode.features import FeatureExtrusion, feature_extrusion
from print_copilot.model.mesh import load_mesh, write_stl
from print_copilot.model.orientation import (
    DEFAULT_THRESHOLD_DEG,
    Goal,
    OrientationAnalysis,
    Region,
    analyse_orientations,
    classify_faces,
    oriented_mesh,
    regions_mask,
    rotation_to_bed,
)
from print_copilot.model.render import (
    BASE_COLOR,
    BED_COLOR,
    CONFLICT_COLOR,
    DEFAULT_VIEWS,
    OVERHANG_COLOR,
    PROTECTED_COLOR,
    ViewInfo,
    render_sheet,
)
from print_copilot.orca.conf import CurrentSetup, current_setup, default_conf_path
from print_copilot.orca.install import OrcaInstall, to_windows_path
from print_copilot.orca.presets import KINDS, Preset, PresetKind, PresetLibrary
from print_copilot.orca.project import is_project
from print_copilot.orca.slicer import (
    DEFAULT_TIMEOUT_S,
    OverrideValue,
    SettingsOverrides,
    SliceRun,
    default_work_dir,
    prepare_cli_settings,
    slice_model,
    tool_count,
)
from print_copilot.serialize import to_jsonable

CONF_SOURCE = "Snapmaker_Orca.conf"


@dataclass
class PresetEntry:
    name: str
    source: str
    vendor: str | None
    inherits: str | None
    path: Path


@dataclass
class PresetCatalog:
    presets: dict[str, list[PresetEntry]]
    broken: list[str] = field(default_factory=list)


def list_presets(
    library: PresetLibrary,
    kind: PresetKind | None = None,
    *,
    printer: str | None = None,
    include_abstract: bool = False,
    all_vendors: bool = False,
) -> PresetCatalog:
    """Presets per kind. Without `all_vendors`, only user presets and the
    library's vendor are listed, except for process/filament presets filtered
    by `printer`, which may live in other vendor folders."""
    catalog = PresetCatalog(presets={})
    for k in [kind] if kind else list(KINDS):
        found = library.list_presets(k, printer=printer, include_abstract=include_abstract)
        catalog.broken.extend(found.broken)
        presets = found.presets
        if not all_vendors and not (printer and k != "machine"):
            presets = [
                p for p in presets if p.ref.source == "user" or p.ref.vendor == library.vendor
            ]
        catalog.presets[k] = [
            PresetEntry(
                name=p.name,
                source=p.ref.source,
                vendor=p.ref.vendor,
                inherits=p.ref.inherits,
                path=p.ref.path,
            )
            for p in presets
        ]
    return catalog


def get_current_setup(data_dir: Path, machine: str | None = None) -> CurrentSetup:
    return current_setup(default_conf_path(data_dir), machine)


@dataclass
class Selection:
    """Resolved presets for a slice, and where each choice came from."""

    machine: Preset
    process: Preset
    filaments: list[Preset]
    notes: list[str] = field(default_factory=list)
    # Colour loaded on each head (None when unknown) and where each came from:
    # Snapmaker_Orca.conf, or the head's RFID reader (`multicolor.use_printer_spools`).
    colours: list[str] | None = None
    colour_sources: list[str] | None = None


def select_presets(
    library: PresetLibrary,
    *,
    printer: str | None = None,
    process: str | None = None,
    filaments: list[str] | None = None,
) -> Selection:
    """Resolve the presets for a slice.

    Anything not given is taken from the selection stored in
    `Snapmaker_Orca.conf` for that printer (the one the app shows), and a note
    says so. One filament given for a multi-head machine is used on every head.
    """
    notes: list[str] = []
    setup: CurrentSetup | None = None
    if printer is None or process is None or not filaments:
        setup = get_current_setup(library.data_dir, printer)
        if printer is None:
            notes.append(f"printer {setup.machine!r} from {CONF_SOURCE}")
        printer = setup.machine
    else:
        # Only for the colours loaded on the heads; a printer the app never
        # stored leaves them unknown.
        with contextlib.suppress(PrintCopilotError):
            setup = get_current_setup(library.data_dir, printer)
    if process is None:
        assert setup is not None
        if not setup.process:
            raise SliceError(f"{CONF_SOURCE} has no process selected for {printer!r}.")
        process = setup.process
        notes.append(f"process {process!r} from {CONF_SOURCE}")
    if not filaments:
        assert setup is not None
        if not setup.filaments:
            raise SliceError(f"{CONF_SOURCE} has no filaments selected for {printer!r}.")
        filaments = setup.filaments
        notes.append(f"filaments {filaments} from {CONF_SOURCE}")

    machine_p = library.resolve("machine", printer)
    process_p = library.resolve("process", process)
    filament_p = [library.resolve("filament", name) for name in filaments]
    heads = tool_count(machine_p)
    if len(filament_p) == 1 and heads > 1:
        # The slicer needs one filament per head (docs/verified-facts.md section 2, gotcha 7).
        filament_p = filament_p * heads
        notes.append(f"using {filaments[0]!r} on all {heads} tool heads")
    # The colours stored for the printer describe what is loaded on each head,
    # whichever filament presets this slice uses.
    colours = sources = None
    if setup and setup.machine == machine_p.name and len(setup.filament_colors) == heads:
        colours = list(setup.filament_colors)
        sources = [CONF_SOURCE] * heads
    return Selection(
        machine=machine_p,
        process=process_p,
        filaments=filament_p,
        notes=notes,
        colours=colours,
        colour_sources=sources,
    )


def run_slice(
    install: OrcaInstall,
    model: str | os.PathLike[str],
    selection: Selection,
    *,
    overrides: dict[str, OverrideValue] | None = None,
    work_dir: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> SliceRun:
    path = Path(model)
    if path.suffix.lower() == ".3mf" and path.is_file() and is_project(path):
        raise ModelError(
            f"{path.name} is a slicer project; the Snapmaker Orca CLI crashes when given it "
            "directly (see orca/project.py). Use slice-project (MCP: slice_project), which "
            "slices one plate from a copy the CLI can read."
        )
    return slice_model(
        install,
        model,
        selection.machine,
        selection.process,
        selection.filaments,
        work_dir=work_dir,
        timeout_s=timeout_s,
        overrides=SettingsOverrides.from_flat(overrides),
    )


@dataclass
class Variant:
    overrides: dict[str, OverrideValue]
    label: str | None = None

    def display_label(self) -> str:
        if self.label:
            return self.label
        if not self.overrides:
            return "presets as is"
        return ", ".join(f"{k}={v}" for k, v in self.overrides.items())


@dataclass
class VariantResult:
    """Measured numbers of one variant. Deltas are against the first variant."""

    label: str
    overrides: dict[str, OverrideValue]
    estimated_time_s: int
    total_filament_g: float
    layer_count: int
    filament_used_g: list[float]  # index i is tool head T<i>
    time_delta_s: int
    filament_delta_g: float
    run_dir: Path


@dataclass
class Comparison:
    model: Path
    machine: str
    process: str
    filaments: list[str]
    variants: list[VariantResult]
    notes: list[str] = field(default_factory=list)


def compare_variants(
    install: OrcaInstall,
    model: str | os.PathLike[str],
    selection: Selection,
    variants: list[Variant],
    *,
    work_dir: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Comparison:
    """Slice one model once per variant and tabulate the measured results.

    Every variant's overrides are checked before the first slice, so a typo in
    the last variant does not waste the earlier runs. Any failed slice aborts
    the comparison with the variant's label in the error.
    """
    if not variants:
        raise SliceError("compare needs at least one variant.")
    parsed = []
    for variant in variants:
        try:
            overrides = SettingsOverrides.from_flat(variant.overrides)
            prepare_cli_settings(
                selection.machine, selection.process, selection.filaments, overrides
            )
        except SliceError as exc:
            raise SliceError(f"Variant {variant.display_label()!r}: {exc}") from exc
        parsed.append(overrides)

    results: list[VariantResult] = []
    for variant, overrides in zip(variants, parsed, strict=True):
        try:
            run = slice_model(
                install,
                model,
                selection.machine,
                selection.process,
                selection.filaments,
                work_dir=work_dir,
                timeout_s=timeout_s,
                overrides=overrides,
            )
        except SliceError as exc:
            raise SliceError(f"Variant {variant.display_label()!r}: {exc}") from exc
        r = run.result
        base = results[0] if results else None
        results.append(
            VariantResult(
                label=variant.display_label(),
                overrides=run.overrides,
                estimated_time_s=r.estimated_time_s,
                total_filament_g=r.total_filament_g,
                layer_count=r.layer_count,
                filament_used_g=r.filament_used_g,
                time_delta_s=r.estimated_time_s - base.estimated_time_s if base else 0,
                filament_delta_g=round(r.total_filament_g - base.total_filament_g, 2)
                if base
                else 0.0,
                run_dir=run.run_dir,
            )
        )
    return Comparison(
        model=Path(model),
        machine=selection.machine.name,
        process=selection.process.name,
        filaments=[f.name for f in selection.filaments],
        variants=results,
        notes=list(selection.notes),
    )


# Process overrides per support mode. The `support_type` values are the ones
# the U1 process presets use and were checked in the G-code config block of
# real slices on 01.10.01.50 (an unknown value falls back to normal(auto)).
SUPPORT_MODES: dict[str, dict[str, OverrideValue]] = {
    "none": {"enable_support": False},
    "normal": {"enable_support": True, "support_type": "normal(auto)"},
    "tree": {"enable_support": True, "support_type": "tree(auto)"},
}
DEFAULT_SUPPORT_MODES: tuple[str, ...] = ("none", "tree")
DEFAULT_TOP_CANDIDATES = 3
RECOMMENDATION_RULES: dict[str, str] = {
    "speed": "lowest slicer time among the slices with supports enabled, then lowest filament",
    "min_support": "least support filament measured in the G-code among the slices with "
    "supports enabled, then lowest slicer time",
    "quality": "best heuristic quality rank (see the analysis ranking) among the slices with "
    "supports enabled, then lowest slicer time",
}
NO_SUPPORT_NOTE = (
    "Only slices without supports were requested; the recommendation ignores overhangs."
)


def bed_size(machine: Preset) -> tuple[float, float, float] | None:
    """Printable X, Y, Z in mm from `printable_area` ("XxY" corners) and
    `printable_height`; None if the preset lacks them."""
    area = machine.settings.get("printable_area")
    height = machine.settings.get("printable_height")
    try:
        points = [tuple(float(v) for v in p.split("x")) for p in area or []]
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return (max(xs) - min(xs), max(ys) - min(ys), float(height))
    except (TypeError, ValueError):
        return None


def support_threshold(process: Preset) -> tuple[float, str | None]:
    """The process' `support_threshold_angle` and a note when it cannot be used."""
    raw = process.settings.get("support_threshold_angle")
    try:
        value = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        value = 0.0
    if 0 < value < 90:
        return value, None
    # 0 means automatic detection in OrcaSlicer; the heuristic needs an angle.
    return DEFAULT_THRESHOLD_DEG, (
        f"support_threshold_angle of {process.name!r} is {raw!r}; the heuristic uses "
        f"{DEFAULT_THRESHOLD_DEG:g} degrees."
    )


def _threshold_and_bed(
    selection: Selection | None, threshold_deg: float | None
) -> tuple[float, tuple[float, float, float] | None, list[str]]:
    notes: list[str] = []
    bed = None
    if selection is not None:
        bed = bed_size(selection.machine)
        if bed is None:
            notes.append(f"{selection.machine.name!r} has no printable area; bed fit unchecked.")
        if threshold_deg is None:
            threshold_deg, note = support_threshold(selection.process)
            if note:
                notes.append(note)
    if threshold_deg is None:
        threshold_deg = DEFAULT_THRESHOLD_DEG
    if not 0 < threshold_deg < 90:
        raise ModelError(
            f"The support threshold must be between 0 and 90 degrees, got {threshold_deg}."
        )
    return threshold_deg, bed, notes


def analyze_model(
    model: str | os.PathLike[str],
    *,
    selection: Selection | None = None,
    threshold_deg: float | None = None,
    regions: list[Region] | None = None,
    goal: Goal = "speed",
) -> OrientationAnalysis:
    """Load a model and rank candidate orientations with the heuristic.

    The threshold comes from `threshold_deg`, else the selected process preset,
    else 30 degrees. The printable volume comes from the selected machine.
    `regions` are surfaces to keep clean; `goal` picks the ranking.
    """
    mesh = load_mesh(model)
    threshold_deg, bed, notes = _threshold_and_bed(selection, threshold_deg)
    try:
        analysis = analyse_orientations(
            mesh, threshold_deg=threshold_deg, bed_size_mm=bed, regions=regions, goal=goal
        )
    except ValueError as exc:
        raise ModelError(str(exc)) from exc
    analysis.notes = notes + analysis.notes
    return analysis


def _file_slug(label: str) -> str:
    text = label.replace("+", "pos").replace("-", "neg")
    return re.sub(r"[^A-Za-z0-9.]+", "_", text).strip("_")


@dataclass
class OrientationSlice:
    """Measured result of one orientation sliced with one support mode.

    Lengths are mm of filament from the G-code toolpaths (`;TYPE:` features).
    """

    candidate: str
    rank: int  # heuristic rank of the candidate
    support_mode: str
    estimated_time_s: int
    total_filament_g: float
    support_filament_mm: float  # every `Support*` feature
    support_filament_g: float  # converted with this G-code's own g/mm ratio
    support_interface_mm: float  # the part of the support that touches the model
    bridge_mm: float  # `Bridge` + `Internal Bridge`
    overhang_wall_mm: float
    layer_count: int
    down_vector: list[float]
    model_path: Path  # rotated model that was sliced
    run_dir: Path


@dataclass
class OrientationReport:
    analysis: OrientationAnalysis
    machine: str
    process: str
    filaments: list[str]
    goal: str
    slices: list[OrientationSlice]
    recommended: OrientationSlice
    recommendation_rule: str
    work_dir: Path
    notes: list[str] = field(default_factory=list)


def _measure_features(
    run: SliceRun,
) -> tuple[FeatureExtrusion, float, str | None]:
    """Per-feature extrusion, the G-code's g/mm ratio, and a note if the
    toolpath total disagrees with the slicer's filament length by more than 1%."""
    features = feature_extrusion(run.gcode_path)
    r = run.result
    total_mm = sum(r.filament_used_mm)
    note = None
    if total_mm > 0 and abs(features.total_mm - total_mm) > 0.01 * total_mm:
        note = (
            f"{run.gcode_path}: toolpath extrusion {features.total_mm:.1f} mm differs from "
            f"the slicer's filament total {total_mm:.1f} mm by more than 1%."
        )
    grams_per_mm = r.total_filament_g / total_mm if total_mm > 0 else 0.0
    return features, grams_per_mm, note


def optimize_orientation(
    install: OrcaInstall,
    model: str | os.PathLike[str],
    selection: Selection,
    *,
    top: int = DEFAULT_TOP_CANDIDATES,
    support_modes: list[str] | tuple[str, ...] = DEFAULT_SUPPORT_MODES,
    goal: Goal = "speed",
    regions: list[Region] | None = None,
    threshold_deg: float | None = None,
    work_dir: str | os.PathLike[str] | None = None,
    include_as_loaded: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> OrientationReport:
    """Rank orientations with the heuristic for `goal`, slice the best `top`
    that fit the bed with every support mode and recommend one (see
    `RECOMMENDATION_RULES`).

    With `include_as_loaded`, the orientation the model was loaded in is also
    sliced (if it fits) so the result can be compared with doing nothing.

    Each rotated model is written as an STL into a fresh folder under the work
    dir; every slice gets its own run dir inside it.
    """
    unknown = [m for m in support_modes if m not in SUPPORT_MODES]
    if unknown or not support_modes:
        raise SliceError(
            f"Unknown support mode(s) {unknown}; choose from {', '.join(SUPPORT_MODES)}."
        )
    if top < 1:
        raise SliceError("top must be at least 1.")
    for mode in support_modes:
        prepare_cli_settings(
            selection.machine,
            selection.process,
            selection.filaments,
            SettingsOverrides.from_flat(SUPPORT_MODES[mode]),
        )

    mesh = load_mesh(model)
    analysis = analyze_model(
        model, selection=selection, threshold_deg=threshold_deg, regions=regions, goal=goal
    )
    fitting = [c for c in analysis.candidates if c.fits_bed is not False]
    chosen = fitting[:top]
    as_loaded = [c for c in fitting if c.origin == "as loaded"]
    if include_as_loaded and as_loaded and as_loaded[0] not in chosen:
        chosen.append(as_loaded[0])
    if not chosen:
        raise SliceError(f"No orientation of {model} fits the printable volume.")

    base = to_windows_path(work_dir) if work_dir else default_work_dir()
    orient_dir = base / f"orient-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    orient_dir.mkdir(parents=True)
    (orient_dir / "analysis.json").write_text(
        json.dumps(to_jsonable(analysis), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    notes: list[str] = []
    slices: list[OrientationSlice] = []
    for candidate in chosen:
        name = f"{candidate.rank}-{_file_slug(candidate.label)}.stl"
        stl = write_stl(oriented_mesh(mesh, candidate), orient_dir / name)
        for mode in support_modes:
            try:
                run = slice_model(
                    install,
                    stl,
                    selection.machine,
                    selection.process,
                    selection.filaments,
                    work_dir=orient_dir,
                    timeout_s=timeout_s,
                    overrides=SettingsOverrides.from_flat(SUPPORT_MODES[mode]),
                )
            except SliceError as exc:
                raise SliceError(f"Orientation {candidate.label!r}, {mode}: {exc}") from exc
            features, grams_per_mm, note = _measure_features(run)
            if note:
                notes.append(note)
            slices.append(
                OrientationSlice(
                    candidate=candidate.label,
                    rank=candidate.rank,
                    support_mode=mode,
                    estimated_time_s=run.result.estimated_time_s,
                    total_filament_g=run.result.total_filament_g,
                    support_filament_mm=features.support_mm,
                    support_filament_g=round(features.support_mm * grams_per_mm, 2),
                    support_interface_mm=features.feature_mm("Support interface"),
                    bridge_mm=round(
                        features.feature_mm("Bridge") + features.feature_mm("Internal Bridge"), 3
                    ),
                    overhang_wall_mm=features.feature_mm("Overhang wall"),
                    layer_count=run.result.layer_count,
                    down_vector=candidate.down_vector,
                    model_path=stl,
                    run_dir=run.run_dir,
                )
            )

    supported = [s for s in slices if s.support_mode != "none"]
    if goal == "quality":
        key = lambda s: (s.rank, s.estimated_time_s, s.total_filament_g)  # noqa: E731
    elif goal == "min_support":
        key = lambda s: (s.support_filament_mm, s.estimated_time_s, s.rank)  # noqa: E731
    else:
        key = lambda s: (s.estimated_time_s, s.total_filament_g, s.rank)  # noqa: E731
    recommended = min(supported or slices, key=key)
    if not supported:
        notes.append(NO_SUPPORT_NOTE)
    report = OrientationReport(
        analysis=analysis,
        machine=selection.machine.name,
        process=selection.process.name,
        filaments=[f.name for f in selection.filaments],
        goal=goal,
        slices=slices,
        recommended=recommended,
        recommendation_rule=RECOMMENDATION_RULES[goal],
        work_dir=orient_dir,
        notes=notes,
    )
    (orient_dir / "report.json").write_text(
        json.dumps(to_jsonable(report), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


@dataclass
class RenderResult:
    """A rendered contact sheet of a model, and how to read it."""

    png_path: Path
    width_px: int
    height_px: int
    frame: str  # which coordinates the grid shows
    down_vector: list[float] | None
    views: list[ViewInfo]
    legend: dict[str, str]
    face_areas_mm2: dict[str, float]
    notes: list[str] = field(default_factory=list)


def render_model(
    model: str | os.PathLike[str],
    *,
    down_vector: list[float] | None = None,
    regions: list[Region] | None = None,
    threshold_deg: float | None = None,
    selection: Selection | None = None,
    views: list[str] | tuple[str, ...] = DEFAULT_VIEWS,
    highlight: bool = True,
    out_path: str | os.PathLike[str] | None = None,
) -> RenderResult:
    """Render views of a model to a PNG, as loaded or in a print orientation.

    With `down_vector`, the model is rotated like an orientation candidate (that
    direction faces the bed) and placed on the bed. With `highlight`, faces are
    coloured: resting on the bed, needing support, inside a protected region,
    or protected and needing support. Regions use the coordinates as loaded.
    """
    mesh = load_mesh(model)
    threshold_deg, _, notes = _threshold_and_bed(selection, threshold_deg)
    try:
        protected = regions_mask(mesh, regions)
    except ValueError as exc:
        raise ModelError(str(exc)) from exc
    if protected is not None and not protected.any():
        raise ModelError("The protected regions select no surface of the model.")

    rotation = np.eye(3)
    shown = mesh
    frame = "model coordinates as loaded (mm)"
    if down_vector is not None:
        if len(down_vector) != 3 or not np.linalg.norm(down_vector) > 0:
            raise ModelError("down_vector must be a non-zero 3-vector.")
        rotation = rotation_to_bed(np.array(down_vector, dtype=float))
        shown = mesh.transformed(rotation).placed_on_bed()
        frame = (
            "print orientation: model rotated so down_vector faces the bed, centred on "
            "X/Y 0 with the bed at Z 0 (mm); regions are still given in the coordinates as loaded"
        )

    colors = np.tile(BASE_COLOR, (mesh.face_count, 1))
    legend: list[tuple[np.ndarray, str]] = []
    areas: dict[str, float] = {}
    if highlight:
        classes = classify_faces(mesh, rotation, threshold_deg=threshold_deg)
        face_areas = mesh.areas_mm2
        colors[classes.on_bed] = BED_COLOR
        colors[classes.needs_support] = OVERHANG_COLOR
        legend = [(BED_COLOR, "ON BED"), (OVERHANG_COLOR, f"NEEDS SUPPORT ({threshold_deg:g})")]
        areas["on_bed"] = round(float(face_areas[classes.on_bed].sum()), 2)
        areas["needs_support"] = round(float(face_areas[classes.needs_support].sum()), 2)
        if protected is not None:
            conflict = protected & classes.needs_support
            colors[protected] = PROTECTED_COLOR
            colors[conflict] = CONFLICT_COLOR
            legend += [(PROTECTED_COLOR, "PROTECTED"), (CONFLICT_COLOR, "PROTECTED + SUPPORT")]
            areas["protected"] = round(float(face_areas[protected].sum()), 2)
            areas["protected_needs_support"] = round(float(face_areas[conflict].sum()), 2)
    elif protected is not None:
        colors[protected] = PROTECTED_COLOR
        legend = [(PROTECTED_COLOR, "PROTECTED")]

    try:
        sheet = render_sheet(shown, views=views, face_colors=colors, legend=legend or None)
    except ValueError as exc:
        raise ModelError(str(exc)) from exc
    if out_path is None:
        folder = default_work_dir() / "renders"
        folder.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(model).stem)
        out_path = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}-{stem}.png"
    path = Path(out_path)
    if path.exists():
        raise ModelError(f"{path} already exists; choose another output path.")
    path.write_bytes(sheet.png)
    return RenderResult(
        png_path=path,
        width_px=sheet.width_px,
        height_px=sheet.height_px,
        frame=frame,
        down_vector=None if down_vector is None else [float(v) for v in down_vector],
        views=sheet.views,
        legend=sheet.legend,
        face_areas_mm2=areas,
        notes=notes,
    )
