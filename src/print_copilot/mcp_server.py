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
from collections.abc import Callable
from typing import Any, Literal, ParamSpec, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from print_copilot import __version__, actions
from print_copilot.errors import PrintCopilotError
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
for those presets. Unknown keys are rejected."""

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


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
