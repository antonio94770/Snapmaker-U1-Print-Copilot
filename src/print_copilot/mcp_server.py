"""MCP server (stdio) exposing Print Copilot to AI agents.

Every tool is a thin wrapper over `print_copilot.actions`, the same code the
`print-copilot` CLI uses. Tools return JSON-friendly dicts; any Print Copilot
error is passed to the client as a tool error carrying the full reason.

Locations come from the same environment variables as the CLI:
`PRINT_COPILOT_ORCA_EXE`, `PRINT_COPILOT_ORCA_DATA`, `PRINT_COPILOT_WORK_DIR`.
Printer tools find the U1 through mDNS unless `PRINT_COPILOT_PRINTER_URL` is
set (`PRINT_COPILOT_PRINTER_NAME` picks one of several U1 printers).

Printer tools read status freely. start_print is gated: it needs the token of a
plan from plan_print_start, and asks the user through MCP elicitation when the
client supports it. cancel_print and heaters_off are never gated.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import NoBackChannelError
from mcp.types import (
    ClientCapabilities,
    ElicitationCapability,
    FormElicitationCapability,
    ToolAnnotations,
)
from pydantic import BaseModel, Field

from print_copilot import __version__, actions, multicolor, quality, settings_advice
from print_copilot.errors import ConfirmationError, PrintCopilotError
from print_copilot.model.orientation import Region
from print_copilot.orca.install import locate, locate_data_dir
from print_copilot.orca.presets import PresetLibrary
from print_copilot.orca.slicer import OverrideValue
from print_copilot.printer import actions as printer
from print_copilot.printer import discovery
from print_copilot.printer.moonraker import MoonrakerClient
from print_copilot.serialize import to_jsonable

P = ParamSpec("P")
R = TypeVar("R")

INSTRUCTIONS = """\
Prepares Snapmaker U1 prints with the Snapmaker Orca slicer on this computer.
Rules first:
- Never start a print on your own initiative. Call plan_print_start, show the
  user the plan (file, request, per-head checks) and call start_print with its
  token only after the user explicitly says to start that file. A quality
  review, an upload or a reachable printer is not consent. cancel_print and
  heaters_off are safety actions and need no confirmation.
- Times, grams, tool changes and supports from slicing tools are the slicer's
  own figures; analyze_model scores and finer-layer candidates are heuristics.
  Tell the user which is which.
- A one-colour plate or STL is never put on a head automatically: show the
  loaded spools and ask which one to print with, then pass head.
Usual flow:
1. get_current_setup (printer, process, per-head filaments in the app;
   list_presets for other names) and printer_status (state and the spool each
   head's RFID reader identified; the U1 is found on the network).
2. inspect_project with the process you will use (3MF from Snapmaker Orca,
   Bambu Studio, MakerWorld or Printables, or an STL): plates, size and fit,
   filaments and proposed heads from the loaded colours (RFID, else the app;
   the notes say which). Before slicing, tell the user the plate size, the
   saved settings that differ from that process and the settings objects set
   themselves; ask which to keep when they matter (layer height, supports). If
   the plate's colours are not loaded, say which filament goes on which head.
3. Optional: slice_project / compare_strategies (tool changes, prime tower,
   supports; on the U1 the purge matrix has no effect), analyze_model,
   render_model and optimize_orientation for orientation (protect regions with
   goal="quality" for surfaces that must print cleanly). Then ask whether the
   print is visual or functional and call suggest_settings on the model as it
   will print; its variants are heuristics to slice in review_print.
4. Check quality: review_print (effective-settings audit and toolpath images)
   and preview_gcode on critical layers; an inventoried setting is not an
   automatically validated one. If the user skips it, at least
   preview the layers where supports and overhangs start and say the full
   review was not run. record_quality_review is a visual-review attestation,
   never permission to print or a physical guarantee.
5. export_project writes the project, slices it, puts the .gcode next to the
   .3mf and opens it in Snapmaker Orca for the user to see.
6. upload_to_printer (never starts), then plan_print_start / start_print as
   above. Tell the user the .3mf path: open_in_orca reopens what was printed.
slice / compare_variants take STL files with T0 and preset JSON key overrides
(sparse_infill_density="30%"; prefix machine. or filament.); unknown or
silently replaced values are rejected."""

server = MCPServer(name="snapmaker-print-copilot", version=__version__, instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Slicing writes only a new run folder under the work dir.
SLICING = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
PRINTER_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
PRINTER_UPLOAD = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
PRINTER_ACTION = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


def _preset_library() -> PresetLibrary:
    data = locate_data_dir()
    if (data / "system").is_dir():
        return PresetLibrary(data)
    install = locate(data_dir=data)
    return PresetLibrary(data, system_dir=install.system_dir)


def _tool_errors(fn: Callable[P, R]) -> Callable[P, R]:
    """Turn Print Copilot errors into MCP tool errors with the same message,
    and any other exception into one that names its type, so the client never
    sees only "Error executing tool"."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except PrintCopilotError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            raise ToolError(f"Unexpected {type(exc).__name__}: {exc}") from exc

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
    The model prints with T0; for another head use slice_project or
    export_project with head.

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
    It prints with T0 and ignores a project's plates: for a 3MF project or
    another head, use compare_strategies with variants, which also measures
    support grams.

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


@server.tool(annotations=READ_ONLY)
@_tool_errors
def suggest_settings(
    model: str,
    purpose: Literal["visual", "functional"] | None = None,
    printer: str | None = None,
    process: str | None = None,
) -> dict[str, Any]:
    """Propose the few process settings this model's shape and purpose bear on
    (thin features -> wall generator; tall on a small base -> brim; visual ->
    layer height; functional -> walls and infill), with the evidence for each.
    Heuristic, no slicing. In a project, objects' own settings win over these.

    model: absolute path of the STL/3MF as it will be printed (Z up); after
        optimize_orientation, pass the rotated STL it wrote.
    purpose: visual (looks matter) or functional (strength matters). Ask the
        user; without it only the geometry checks run.
    printer, process: presets the proposals start from; omitted ones come from
        the selection in Snapmaker Orca.
    Returns geometry facts, proposals (current value, proposed value, reason,
    basis) and variants: pass them to review_print or compare_variants to get
    measured time and grams and to inspect the toolpaths before choosing.
    """
    selection = actions.select_presets(_preset_library(), printer=printer, process=process)
    suggestion = settings_advice.suggest_settings(model, selection, purpose=purpose)
    suggestion.notes = selection.notes + suggestion.notes
    return to_jsonable(suggestion)


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
    printer: str | None,
    process: str | None,
    filaments: list[str] | None,
    use_printer: bool = False,
) -> tuple[Any, actions.Selection]:
    install = locate()
    selection = actions.select_presets(
        PresetLibrary(install.data_dir, system_dir=install.system_dir),
        printer=printer,
        process=process,
        filaments=filaments,
    )
    if use_printer:
        selection = multicolor.with_printer_spools(selection)
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
    use_printer: bool = True,
    process: str | None = None,
) -> dict[str, Any]:
    """Read a 3MF project without slicing: who saved it, for which printer, its
    plates, the objects and filaments (number, colour, type) each plate uses,
    and for each plate the proposed U1 tool head per filament (nearest loaded
    colour, CIE76 delta E heuristic; identical colours share a head).
    Also: each object's size in mm as placed and whether the plate fits the
    bed, settings the objects set themselves (object_settings), and the saved
    settings that differ from the process preset (setting_differences). Tell
    the user the size and those differences before slicing.

    model: absolute path of the .3mf.
    reduce_colours: merge the nearest colours when a plate needs more filaments
        than there are heads (heuristic; otherwise such a plate gets an error
        with the closest colour pairs).
    use_printer: as for slice_project.
    process: process preset to compare with; default the one selected in the app.
    """
    _, selection = _project_selection(printer, process, filaments, use_printer)
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
    head: int | None = None,
    use_printer: bool = True,
) -> dict[str, Any]:
    """Slice one plate of a 3MF project (or an STL) on the U1 and return measured time,
    grams per head, tool changes, layers with changes, prime tower and purge
    grams, plus the head assignment used.

    plate: 1-based; required when the project has several plates.
    mapping: project filament number -> head index (0 = T0), e.g. {"5": 1};
        several filaments may share a head. Unmapped filaments get the nearest
        loaded colour.
    head: one head (0 = T0) for the whole plate; required for a one-colour
        plate or an STL: ask the user which loaded spool to use.
    use_printer: read the loaded colours from the U1's RFID readers (falls
        back to the app's selection, with a note, when the printer is away).
    overrides, printer, process, filaments: as for slice.
    """
    install, selection = _project_selection(printer, process, filaments, use_printer)
    result = multicolor.slice_project(
        install,
        model,
        selection,
        plate=plate,
        mapping=_mapping(mapping),
        reduce_colours=reduce_colours,
        overrides=overrides,
        head=head,
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
    head: int | None = None,
    use_printer: bool = True,
) -> dict[str, Any]:
    """Slice one plate of a project once per tool-change strategy and compare
    measured time, grams, tool changes and prime tower grams. Default
    strategies: presets as is, no prime tower, one object at a time (only with
    two or more objects). A strategy the slicer refuses is listed with its
    error. Quality effects (ooze without a tower) are not measured: say so.

    variants: custom strategies instead, same format as compare_variants; any
        settings, e.g. support_threshold_angle 25 vs 30 on one head. Each slice
        reports measured support grams under support.
    Other arguments: as for slice_project.
    """
    install, selection = _project_selection(printer, process, filaments, use_printer)
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
        head=head,
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
    out_path: str | None = None,
    plate: int | None = None,
    mapping: dict[str, int] | None = None,
    reduce_colours: bool = False,
    overrides: dict[str, OverrideValue] | None = None,
    open_in_app: bool = True,
    printer: str | None = None,
    process: str | None = None,
    filaments: list[str] | None = None,
    head: int | None = None,
    use_printer: bool = True,
    slice: bool = True,
) -> dict[str, Any]:
    """Write one plate of a project (or an STL) as a NEW 3MF for Snapmaker
    Orca: objects centred on the U1 bed, filaments on the chosen heads, the
    selected presets (with overrides) as project settings. With slice, that
    same 3MF is sliced and the G-code is written next to it (same name,
    .gcode): upload that file, and tell the user the .3mf is the project to
    reopen for what was printed. open_in_app (default on) starts Snapmaker
    Orca with it so the user sees and can adjust it before anything is
    uploaded; turn it off only for intermediate exports. Never overwrites a
    file.

    out_path: absolute path of the new .3mf (must not exist, nor its .gcode);
        default: a new folder under PRINT_COPILOT_EXPORT_DIR
        (%LOCALAPPDATA%\\print-copilot\\exports).
    Other arguments: as for slice_project.
    """
    install, selection = _project_selection(printer, process, filaments, use_printer)
    exported = multicolor.export_project(
        install,
        model,
        selection,
        out_path,
        plate=plate,
        mapping=_mapping(mapping),
        reduce_colours=reduce_colours,
        overrides=overrides,
        head=head,
        slice_gcode=slice,
    )
    data = to_jsonable(exported)
    data["notes"] = selection.notes + data["notes"]
    data["opened_with"] = multicolor.open_in_app(install, exported.path) if open_in_app else None
    return data


@server.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    )
)
@_tool_errors
def open_in_orca(project: str) -> dict[str, Any]:
    """Open an existing 3MF project in Snapmaker Orca so the user sees it
    (for example the .3mf next to a G-code that was printed). Starts the app
    and returns at once; changes nothing on disk.

    project: absolute path of the .3mf.
    """
    install = locate()
    return {"project": project, "opened_with": multicolor.open_in_app(install, project)}


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
    head: int | None = None,
    use_printer: bool = True,
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
    head, use_printer: as for slice_project (an STL with head is sliced on that head).
    """
    install = selection = None
    if Path(model).suffix.lower() != ".gcode":
        install, selection = _project_selection(printer, process, filaments, use_printer)
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
        head=head,
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


@server.tool(annotations=PRINTER_READ)
@_tool_errors
def discover_printers() -> list[dict[str, Any]]:
    """List Snapmaker printers that answer on the LAN (mDNS), with their current address.

    The printer tools use the single U1 found this way unless
    PRINT_COPILOT_PRINTER_URL is set.
    """
    return [{**to_jsonable(p), "url": p.url} for p in discovery.discover()]


@server.tool(annotations=PRINTER_READ)
@_tool_errors
def printer_status() -> dict[str, Any]:
    """Read the U1's state: Klipper and print state, bed and head temperatures,
    which head is active, and the spool each head's RFID reader identified
    (heads without a tag show none; that does not mean they are empty).
    While printing: current layer, Z in mm, and remaining_estimate_s (the
    slicer's estimate minus the time printed; an estimate, say so)."""
    status = printer.printer_status(MoonrakerClient())
    data = to_jsonable(status)
    data["ready_to_start"] = status.ready_to_start
    return data


@server.tool(annotations=PRINTER_READ)
@_tool_errors
def list_printer_files() -> list[dict[str, Any]]:
    """List the G-code files stored on the printer, newest first."""
    return to_jsonable(printer.list_files(MoonrakerClient()))


@server.tool(annotations=PRINTER_UPLOAD)
@_tool_errors
def upload_to_printer(
    gcode: str, name: str | None = None, overwrite: bool = False
) -> dict[str, Any]:
    """Copy a sliced G-code file into the printer's storage. Never starts a print.

    gcode: absolute path of a .gcode sliced by Snapmaker Orca for the U1
    (checked through its printer_model). name: file name on the printer
    (default: the local name). overwrite: replace a file of the same name;
    only when the user asked for it.
    """
    return to_jsonable(printer.upload_gcode(MoonrakerClient(), gcode, name, overwrite=overwrite))


@server.tool(annotations=PRINTER_READ)
@_tool_errors
def plan_print_start(filename: str) -> dict[str, Any]:
    """Prepare starting a file already on the printer. Sends nothing.

    Returns the exact request start_print would send, the slicer's figures
    stored in the file, the filament each used head needs next to the RFID
    spool it holds, and checks (errors block the start). Show all of it to the
    user. The token works once, for 10 minutes, and only if neither the file
    nor the printer state changes.
    """
    plan = printer.plan_start(MoonrakerClient(), filename)
    data = to_jsonable(plan)
    data["blocked"] = plan.blocked
    if plan.blocked:
        data["token"] = None
        data["note"] = "Blocked by the errors in checks; nothing can be started."
    return data


class StartConfirmation(BaseModel):
    start: bool = Field(description="Start this print on the printer now")


def _can_elicit(ctx: Context) -> bool:
    wanted = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))
    try:
        return ctx.request_context.session.check_client_capability(wanted)
    except (AttributeError, LookupError, ValueError):
        return False


def _confirmation_message(plan: printer.StartPlan) -> str:
    lines = [f"Start {plan.filename} on {plan.base_url}?", f"Request: {plan.request}"]
    if plan.estimated_time_s is not None:
        lines.append(f"Slicer estimate: {plan.estimated_time_s} s, {plan.filament_total_g} g")
    for head in plan.heads:
        lines.append(
            f"T{head.index}: needs {head.filament_type} {head.colour or ''} "
            f"({head.weight_g} g), RFID spool: {head.loaded_type or 'none read'}"
        )
    lines += [f"{c.level.upper()}: {c.message}" for c in plan.checks]
    return "\n".join(lines)


@server.tool(annotations=PRINTER_ACTION)
async def start_print(confirmation_token: str, ctx: Context) -> dict[str, Any]:
    """Start a print planned by plan_print_start. PHYSICAL ACTION: the printer heats and moves.

    Call only after the user explicitly confirmed starting that file. When the
    client supports elicitation the user is asked again here; a decline sends
    nothing. The file and printer state are re-checked before sending.
    """
    try:
        plan = printer.take_plan(confirmation_token)
        confirmed_by = "plan token (client without elicitation)"
        if _can_elicit(ctx):
            try:
                answer = await ctx.elicit(_confirmation_message(plan), StartConfirmation)
            except NoBackChannelError:
                # Claude Code advertises form elicitation but its transport cannot
                # send the request (seen 2026-10-28). The client cannot ask the
                # user, so it is a client without elicitation: the token decides.
                answer = None
                confirmed_by = "plan token (client elicitation unavailable)"
            if answer is not None:
                if answer.action != "accept" or not answer.data.start:
                    raise ConfirmationError("The user did not confirm the start; nothing was sent.")
                confirmed_by = "user via MCP elicitation"
        return to_jsonable(
            printer.execute_start(MoonrakerClient(), plan, confirmed_by=confirmed_by)
        )
    except PrintCopilotError as exc:
        raise ToolError(str(exc)) from exc


@server.tool(annotations=PRINTER_ACTION)
@_tool_errors
def cancel_print() -> dict[str, Any]:
    """Cancel the running print (Klipper CANCEL_PRINT). Safety action, never gated."""
    return to_jsonable(printer.cancel_print(MoonrakerClient()))


@server.tool(annotations=PRINTER_ACTION)
@_tool_errors
def heaters_off() -> dict[str, Any]:
    """Turn every heater off (TURN_OFF_HEATERS). Safety action, never gated."""
    return to_jsonable(printer.heaters_off(MoonrakerClient()))


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
