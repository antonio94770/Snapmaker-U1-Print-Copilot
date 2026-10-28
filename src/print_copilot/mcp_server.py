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
from typing import Any, Literal, ParamSpec, TypeVar

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from print_copilot import __version__, actions
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
numbers are measured by the slicer and which are heuristic."""

server = MCPServer(name="snapmaker-print-copilot", version=__version__, instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Slicing writes only a new run folder under the work dir.
SLICING = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


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
    library = PresetLibrary(locate_data_dir())
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
        PresetLibrary(install.data_dir), printer=printer, process=process, filaments=filaments
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
        PresetLibrary(install.data_dir), printer=printer, process=process, filaments=filaments
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
    selection = actions.select_presets(
        PresetLibrary(locate_data_dir()), printer=printer, process=process
    )
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
        PresetLibrary(install.data_dir), printer=printer, process=process, filaments=filaments
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
    selection = actions.select_presets(
        PresetLibrary(locate_data_dir()), printer=printer, process=process
    )
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


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
