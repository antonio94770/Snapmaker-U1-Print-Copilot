"""MCP server (stdio) exposing Print Copilot to AI agents.

Every tool is a thin wrapper over `print_copilot.actions`, the same code the
`print-copilot` CLI uses. Tools return JSON-friendly dicts; any Print Copilot
error is passed to the client as a tool error carrying the full reason.

Locations come from the same environment variables as the CLI:
`PRINT_COPILOT_ORCA_EXE`, `PRINT_COPILOT_ORCA_DATA`, `PRINT_COPILOT_WORK_DIR`.

None of these tools touch the printer.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from print_copilot import __version__, actions, multicolor, quality
from print_copilot.errors import PrintCopilotError
from print_copilot.model.orientation import Region
from print_copilot.orca.install import locate, locate_data_dir
from print_copilot.orca.presets import PresetLibrary
from print_copilot.orca.slicer import OverrideValue
from print_copilot.serialize import to_jsonable

P = ParamSpec("P")
R = TypeVar("R")

INSTRUCTIONS = """\
Prepares Snapmaker U1 prints with the Snapmaker Orca slicer on this computer.
Before calling a setup reviewed, use review_print for the effective-settings
audit and actual toolpath images. Inspect model views and critical layers with
preview_gcode, and read warning/unverified findings. record_quality_review is
an explicit visual-review attestation, not permission to print or a physical
quality guarantee. Finer layers are a heuristic candidate, not a calibrated
winner. Never claim all inventoried parameters were automatically validated.
Start with get_current_setup to see the printer, process and per-head filaments
selected in the app, and list_presets to find other preset names. slice and
compare_variants run the real slicer headlessly: their times and grams are the
slicer's own figures, never estimates. Overrides use preset JSON keys
(e.g. sparse_infill_density="30%", wall_loops=3); prefix machine. or filament.
for those presets. Unknown keys are rejected, and so is a value the slicer
silently replaced. analyze_model ranks orientations with a fast geometric
heuristic; optimize_orientation confirms the best ones with real slices
(support none / normal / tree) and writes the rotated models as STL files.
Look before you decide: render_model shows the model as an image. If some
surface must print cleanly (a miniature's face, text, the side that will be
seen), pass it as a protect region with goal="quality", then render the
recommended down_vector with the same regions to check it. Tell the user which
numbers are measured by the slicer and which are heuristic.
Multi-colour 3MF projects (from Snapmaker Orca, Bambu Studio, MakerWorld or
Printables): inspect_project shows plates, the filaments each plate uses and the
tool head proposed for each from the colours loaded on the U1. slice_project
slices one plate and measures tool changes, prime tower and purge;
compare_strategies tries the prime tower off and one object at a time;
export_project writes the plate as a new project for the app and can open it.
On the U1 the purge matrix has no effect: what costs time and material is the
number of tool changes and the prime tower. If the plate's colours are not the
loaded ones, tell the user which filament to load on which head."""

server = MCPServer(name="snapmaker-print-copilot", version=__version__, instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Slicing writes only a new run folder under the work dir.
SLICING = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def _preset_library() -> PresetLibrary:
    data = locate_data_dir()
    if (data / "system").is_dir():
        return PresetLibrary(data)
    install = locate(data_dir=data)
    return PresetLibrary(data, system_dir=install.system_dir)


def _tool_errors(fn: Callable[P, R]) -> Callable[P, R]:
    """Turn Print Copilot errors into MCP tool errors with the same message."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except PrintCopilotError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


class VariantSpec(BaseModel):
    """One variant for compare_variants."""

    label: str | None = Field(default=None, description="Short name shown in the table")
    overrides: dict[str, OverrideValue] = Field(
        default_factory=dict,
        description="Setting overrides; empty slices the presets as they are",
    )


@server.tool(annotations=READ_ONLY)
@_tool_errors
def list_presets(
    kind: Literal["machine", "process", "filament"] | None = None,
    printer: str | None = None,
    include_abstract: bool = False,
    all_vendors: bool = False,
) -> dict[str, Any]:
    """List Snapmaker Orca presets (Snapmaker system presets plus the user's own).

    kind: only machine, process or filament presets.
    printer: only process/filament presets compatible with this machine preset,
        e.g. "Snapmaker U1 (0.4 nozzle)".
    include_abstract: also list base presets the app hides.
    all_vendors: include presets of every vendor folder, not only Snapmaker.
    """
    library = _preset_library()
    catalog = actions.list_presets(
        library,
        kind,
        printer=printer,
        include_abstract=include_abstract,
        all_vendors=all_vendors,
    )
    presets = {
        k: [{"name": e.name, "source": e.source, "inherits": e.inherits} for e in entries]
        for k, entries in catalog.presets.items()
    }
    return {"presets": presets, "skipped_broken": catalog.broken}


@server.tool(annotations=READ_ONLY)
@_tool_errors
def get_current_setup(printer: str | None = None) -> dict[str, Any]:
    """The printer, process, per-head filaments and colours selected in Snapmaker
    Orca, plus the purge volume matrix, read from Snapmaker_Orca.conf.

    printer: read the stored selection of this machine instead of the current one.
    filaments[i] and filament_colors[i] belong to tool head T<i>.
    flush_volumes_matrix_mm3[i][j] is the purge volume from T<i> to T<j>.
    """
    return to_jsonable(actions.get_current_setup(locate_data_dir(), printer))


@server.tool(annotations=SLICING)
@_tool_errors
def slice(
    model: str,
    printer: str | None = None,
    process: str | None = None,
    filaments: list[str] | None = None,
    overrides: dict[str, OverrideValue] | None = None,
) -> dict[str, Any]:
    """Slice an STL/3MF model headlessly with Snapmaker Orca and return the
    measured print time, filament per tool head, layer count and key settings.

    model: absolute path of the model file.
    printer, process, filaments: preset names; anything omitted is taken from
        the selection in Snapmaker Orca (see get_current_setup). One filament is
        used on every tool head.
    overrides: settings for this slice only, e.g. {"sparse_infill_density": "30%",
        "filament.nozzle_temperature": 215}. Keys without a prefix are process keys.
    """
    install = locate()
    selection = actions.select_presets(
        PresetLibrary(install.data_dir, system_dir=install.system_dir),
        printer=printer,
        process=process,
        filaments=filaments,
    )
    run = actions.run_slice(install, model, selection, overrides=overrides)
    data = to_jsonable(run)
    data["notes"] = selection.notes
    return data


@server.tool(annotations=SLICING)
@_tool_errors
def compare_variants(
    model: str,
    variants: list[VariantSpec],
    printer: str | None = None,
    process: str | None = None,
    filaments: list[str] | None = None,
) -> dict[str, Any]:
    """Slice one model once per variant (set of overrides) and return a table of
    measured time, total grams, layers and grams per tool head, with deltas
    against the first variant. Put the baseline (empty overrides) first.

    model, printer, process, filaments: as for slice.
    variants: e.g. [{"label": "15%", "overrides": {}},
        {"label": "30%", "overrides": {"sparse_infill_density": "30%"}}].
    """
    install = locate()
    selection = actions.select_presets(
        PresetLibrary(install.data_dir, system_dir=install.system_dir),
        printer=printer,
        process=process,
        filaments=filaments,
    )
    specs = [actions.Variant(overrides=dict(v.overrides), label=v.label) for v in variants]
    return to_jsonable(actions.compare_variants(install, model, selection, specs))


class RegionSpec(BaseModel):
    """A surface to keep clean (a miniature's face, a logo, the visible side).

    Coordinates are the model's as loaded, in mm: read them off the grid of a
    render_model sheet made without down_vector. A face belongs to the region
    when its outward normal is within angle_deg of direction (if given) and its
    centroid is inside the box (if given).
    """

    label: str = Field(default="protected", description="Short name, e.g. 'face'")
    direction: list[float] | None = Field(
        default=None, description="Outward direction the surface faces, e.g. [0, -1, 0]"
    )
    angle_deg: float = Field(default=60.0, description="Cone half-angle around direction")
    box_min_mm: list[float] | None = Field(default=None, description="[x, y, z] lower corner")
    box_max_mm: list[float] | None = Field(default=None, description="[x, y, z] upper corner")

    def to_region(self) -> Region:
        return Region(**self.model_dump())


def _regions(protect: list[RegionSpec] | None) -> list[Region] | None:
    return [spec.to_region() for spec in protect] if protect else None


@server.tool(annotations=READ_ONLY)
@_tool_errors
def analyze_model(
    model: str,
    goal: Literal["speed", "quality", "min_support"] = "speed",
    protect: list[RegionSpec] | None = None,
    printer: str | None = None,
    process: str | None = None,
    threshold_deg: float | None = None,
) -> dict[str, Any]:
    """Rank candidate print orientations of an STL/3MF model with a geometric
    heuristic. Fast, no slicing; the numbers are a heuristic, not measurements
    (use optimize_orientation for slicer figures).

    model: absolute path of the model file.
    goal: speed (least support), quality (protected surfaces clean, then fewest
        support scars and layer steps), min_support.
    protect: surfaces that must print cleanly; adds candidates that turn them
        to several elevations, and quality ranks by them.
    printer, process: presets for the bed-fit check and the support threshold
        angle; omitted ones come from the selection in Snapmaker Orca.
    threshold_deg: support threshold in degrees from horizontal.
    Each candidate gives down_vector (the model-frame direction facing the bed),
    rotation_matrix, height, bed contact, overhang, support volume, stair area
    and the protected-surface versions of those areas.
    """
    selection = actions.select_presets(_preset_library(), printer=printer, process=process)
    analysis = actions.analyze_model(
        model,
        selection=selection,
        threshold_deg=threshold_deg,
        regions=_regions(protect),
        goal=goal,
    )
    analysis.notes = selection.notes + analysis.notes
    return to_jsonable(analysis)


@server.tool(annotations=SLICING)
@_tool_errors
def optimize_orientation(
    model: str,
    goal: Literal["speed", "quality", "min_support"] = "speed",
    protect: list[RegionSpec] | None = None,
    top: int = actions.DEFAULT_TOP_CANDIDATES,
    support_modes: list[Literal["none", "normal", "tree"]] | None = None,
    printer: str | None = None,
    process: str | None = None,
    filaments: list[str] | None = None,
    threshold_deg: float | None = None,
) -> dict[str, Any]:
    """Find a good print orientation: rank candidates with the heuristic for
    goal, slice the best `top` ones (plus the orientation as loaded) with each
    support mode, and recommend one by the rule given in recommendation_rule.

    model, printer, process, filaments: as for slice.
    goal, protect: as for analyze_model. For a miniature, protect the face and
        use goal "quality".
    top: how many candidate orientations to slice (each costs one slice per mode).
    support_modes: subset of none, normal, tree (default none and tree).
    Returns the analysis and one row per slice: slicer time and grams, plus
    support, support-interface, bridge and overhang-wall filament measured from
    the G-code toolpaths, the down_vector and the rotated STL path. Check the
    recommendation visually with render_model(down_vector=..., protect=...).
    """
    install = locate()
    selection = actions.select_presets(
        PresetLibrary(install.data_dir, system_dir=install.system_dir),
        printer=printer,
        process=process,
        filaments=filaments,
    )
    report = actions.optimize_orientation(
        install,
        model,
        selection,
        top=top,
        support_modes=support_modes or list(actions.DEFAULT_SUPPORT_MODES),
        goal=goal,
        regions=_regions(protect),
        threshold_deg=threshold_deg,
    )
    report.notes = selection.notes + report.notes
    return to_jsonable(report)


ViewName = Literal["front", "back", "left", "right", "top", "bottom", "iso", "iso_back"]


@server.tool(annotations=READ_ONLY)
@_tool_errors
def render_model(
    model: str,
    down_vector: list[float] | None = None,
    protect: list[RegionSpec] | None = None,
    views: list[ViewName] | None = None,
    highlight: bool = True,
    printer: str | None = None,
    process: str | None = None,
    threshold_deg: float | None = None,
) -> list[str | Image]:
    """Render a model to one PNG sheet of labelled views so you can look at it:
    find the important surfaces (a face, text, a visible side) before choosing
    protect regions, and check a recommended orientation afterwards.

    model: absolute path of the STL/3MF file.
    down_vector: show the model in that print orientation (the direction that
        faces the bed, as returned by analyze_model / optimize_orientation),
        placed on the bed. Omit it to see the model as loaded; use that sheet's
        grid to read coordinates for protect boxes.
    protect: regions to colour, same format as analyze_model.
    views: default front, back, left, right, top, bottom, iso. "front" looks
        from -Y towards +Y with +Z up.
    highlight: colour faces resting on the bed (green), needing support at the
        support threshold of the process (red), protected (blue), protected
        and needing support (magenta).
    Returns a JSON description (pixel to mm mapping of every view, legend,
    highlighted areas, PNG path) followed by the image.
    """
    selection = actions.select_presets(_preset_library(), printer=printer, process=process)
    result = actions.render_model(
        model,
        down_vector=down_vector,
        regions=_regions(protect),
        threshold_deg=threshold_deg,
        selection=selection,
        views=views or actions.DEFAULT_VIEWS,
        highlight=highlight,
    )
    result.notes = selection.notes + result.notes
    text = json.dumps(to_jsonable(result), indent=1, ensure_ascii=False)
    return [text, Image(data=result.png_path.read_bytes(), format="png")]


def _project_selection(
    printer: str | None, process: str | None, filaments: list[str] | None
) -> tuple[Any, actions.Selection]:
    install = locate()
    selection = actions.select_presets(
        PresetLibrary(install.data_dir, system_dir=install.system_dir),
        printer=printer,
        process=process,
        filaments=filaments,
    )
    return install, selection


def _mapping(mapping: dict[str, int] | None) -> dict[int, int] | None:
    if not mapping:
        return None
    try:
        return {int(k): int(v) for k, v in mapping.items()}
    except ValueError as exc:
        raise ToolError(
            'mapping keys are project filament numbers and values head indexes, e.g. {"5": 1}.'
        ) from exc


@server.tool(annotations=READ_ONLY)
@_tool_errors
def inspect_project(
    model: str,
    printer: str | None = None,
    filaments: list[str] | None = None,
    reduce_colours: bool = False,
) -> dict[str, Any]:
    """Read a 3MF project without slicing: who saved it, for which printer, its
    plates, the objects and filaments (number, colour, type) each plate uses,
    and for each plate the proposed U1 tool head per filament (nearest loaded
    colour, CIE76 delta E heuristic; identical colours share a head).

    model: absolute path of the .3mf.
    reduce_colours: merge the nearest colours when a plate needs more filaments
        than there are heads (heuristic; otherwise such a plate gets an error
        with the closest colour pairs).
    """
    _, selection = _project_selection(printer, None, filaments)
    summary = multicolor.inspect_project(model, selection, reduce_colours=reduce_colours)
    data = to_jsonable(summary)
    data["notes"] = selection.notes + data["notes"]
    return data


@server.tool(annotations=SLICING)
@_tool_errors
def slice_project(
    model: str,
    plate: int | None = None,
    mapping: dict[str, int] | None = None,
    reduce_colours: bool = False,
    overrides: dict[str, OverrideValue] | None = None,
    printer: str | None = None,
    process: str | None = None,
    filaments: list[str] | None = None,
) -> dict[str, Any]:
    """Slice one plate of a 3MF project on the U1 and return measured time,
    grams per head, tool changes, layers with changes, prime tower and purge
    grams, plus the head assignment used.

    plate: 1-based; required when the project has several plates.
    mapping: project filament number -> head index (0 = T0), e.g. {"5": 1};
        several filaments may share a head. Unmapped filaments get the nearest
        loaded colour.
    overrides, printer, process, filaments: as for slice.
    """
    install, selection = _project_selection(printer, process, filaments)
    result = multicolor.slice_project(
        install,
        model,
        selection,
        plate=plate,
        mapping=_mapping(mapping),
        reduce_colours=reduce_colours,
        overrides=overrides,
    )
    data = to_jsonable(result)
    data["notes"] = selection.notes + data["notes"]
    return data


@server.tool(annotations=SLICING)
@_tool_errors
def compare_strategies(
    model: str,
    plate: int | None = None,
    mapping: dict[str, int] | None = None,
    reduce_colours: bool = False,
    variants: list[VariantSpec] | None = None,
    printer: str | None = None,
    process: str | None = None,
    filaments: list[str] | None = None,
) -> dict[str, Any]:
    """Slice one plate of a project once per tool-change strategy and compare
    measured time, grams, tool changes and prime tower grams. Default
    strategies: presets as is, no prime tower, one object at a time (only with
    two or more objects). A strategy the slicer refuses is listed with its
    error. Quality effects (ooze without a tower) are not measured: say so.

    variants: custom strategies instead, same format as compare_variants.
    Other arguments: as for slice_project.
    """
    install, selection = _project_selection(printer, process, filaments)
    specs = (
        [actions.Variant(overrides=dict(v.overrides), label=v.label) for v in variants]
        if variants
        else None
    )
    result = multicolor.compare_strategies(
        install,
        model,
        selection,
        plate=plate,
        mapping=_mapping(mapping),
        reduce_colours=reduce_colours,
        variants=specs,
    )
    data = to_jsonable(result)
    data["notes"] = selection.notes + data["notes"]
    return data


@server.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    )
)
@_tool_errors
def export_project(
    model: str,
    out_path: str,
    plate: int | None = None,
    mapping: dict[str, int] | None = None,
    reduce_colours: bool = False,
    overrides: dict[str, OverrideValue] | None = None,
    open_in_app: bool = False,
    printer: str | None = None,
    process: str | None = None,
    filaments: list[str] | None = None,
) -> dict[str, Any]:
    """Write one plate of a project as a NEW 3MF for Snapmaker Orca: objects
    centred on the U1 bed, filaments on the chosen heads, the selected presets
    (with overrides) as project settings. With open_in_app, start Snapmaker Orca
    with it so the user can check and adjust it. Never overwrites a file.

    out_path: absolute path of the new .3mf (must not exist).
    Other arguments: as for slice_project.
    """
    install, selection = _project_selection(printer, process, filaments)
    exported = multicolor.export_project(
        install,
        model,
        selection,
        out_path,
        plate=plate,
        mapping=_mapping(mapping),
        reduce_colours=reduce_colours,
        overrides=overrides,
    )
    data = to_jsonable(exported)
    data["notes"] = selection.notes + data["notes"]
    data["opened_with"] = multicolor.open_in_app(install, exported.path) if open_in_app else None
    return data


@server.tool(annotations=SLICING)
@_tool_errors
def review_print(
    model: str,
    printer: str | None = None,
    process: str | None = None,
    filaments: list[str] | None = None,
    plate: int | None = None,
    mapping: dict[str, int] | None = None,
    reduce_colours: bool = False,
    compare: bool = True,
    variants: list[VariantSpec] | None = None,
    max_segments: int = 1_000_000,
) -> list[str | Image]:
    """Review STL/one 3MF plate or existing G-code before preparing a print.

    Inventory every effective setting, flag detected errors and unverified
    conditions, and generate model views and actual G-code layer previews.
    For models, compare baseline with a finer-layer heuristic candidate by
    default. Custom variants use the same override schema as compare_variants.
    Existing G-code needs no Orca installation and is only audited.
    Images show selected layers; use preview_gcode for additional critical layers
    and open the offline HTML for every layer. Black dots are outer-wall path
    starts (seam candidates), not verified seams. No physical roughness is predicted.
    Read findings and actually inspect images before record_quality_review.
    No physical print is authorized, and calibration/hardware remain unverified.
    """
    install = selection = None
    if Path(model).suffix.lower() != ".gcode":
        install, selection = _project_selection(printer, process, filaments)
    elif printer or process or filaments:
        raise quality.QualityError(
            "Existing G-code uses its own effective config; omit preset choices."
        )
    specs = (
        [actions.Variant(dict(v.overrides), label=v.label) for v in variants] if variants else None
    )
    report = quality.review_print(
        model,
        install=install,
        selection=selection,
        plate=plate,
        mapping=_mapping(mapping),
        reduce_colours=reduce_colours,
        compare=compare,
        variants=specs,
        max_segments=max_segments,
    )
    result: list[str | Image] = [json.dumps(to_jsonable(report), ensure_ascii=False)]
    for index, variant in enumerate(report.variants):
        if variant.error:
            continue
        if index == 0 and variant.model_preview:
            result.append(Image(data=variant.model_preview.read_bytes(), format="png"))
        if variant.preview:
            result.append(f"{variant.label}: layers {variant.preview.layers_shown}")
            result.append(Image(data=variant.preview.png_path.read_bytes(), format="png"))
    return result


@server.tool(annotations=SLICING)
@_tool_errors
def preview_gcode(
    gcode: str, layers: list[int] | None = None, max_segments: int = 1_000_000
) -> list[str | Image]:
    """Inspect one to six actual G-code layers (1-based), or representative ones.

    Returns a PNG plus the path to an offline interactive viewer of all layers.
    No installation needed. This is commanded toolpath geometry, not a physical
    finish simulation. Startup extrusion, travels and firmware macros are excluded.
    """
    preview = quality.preview_gcode(gcode, layers=layers, max_segments=max_segments)
    return [
        json.dumps(to_jsonable(preview)),
        Image(data=preview.png_path.read_bytes(), format="png"),
    ]


@server.tool(annotations=SLICING)
@_tool_errors
def record_quality_review(
    report_path: str,
    variant: str,
    decision: Literal["accept_with_limitations", "revise"],
    observations: str,
    accepted_findings: list[str] | None = None,
) -> dict[str, Any]:
    """Record an attestation only after actually viewing the chosen variant's images.

    Describe the inspected surfaces/layers in observations. Acceptance requires
    explicitly acknowledging every warning and unverified finding code. Detected
    errors cannot be waived. Changed inputs or previews invalidate the review.
    This does not certify physical quality or confirm starting a print.
    """
    return to_jsonable(
        quality.record_quality_review(
            report_path,
            variant=variant,
            decision=decision,
            observations=observations,
            accepted_findings=accepted_findings,
        )
    )


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
