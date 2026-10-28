"""Printer actions shared by the MCP server and the CLI.

Read-only actions (status, files) need no confirmation. Upload only copies a
file into the printer's storage and refuses to overwrite one. Starting a print
goes through a two-step gate (AGENTS.md rule 2): `plan_start` reads the file's
metadata and the printer state, checks the heads, and returns a `StartPlan`
listing the exact request; `execute_start` sends it only for a plan that is
still pending, unexpired, unchanged on the printer and free of errors. Cancel
and heaters-off are never gated.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from print_copilot.errors import ConfirmationError, PrinterError
from print_copilot.gcode.header import read_config_block
from print_copilot.orca.heads import CLEARLY_DIFFERENT_DELTA_E, COLOUR_RE, delta_e
from print_copilot.printer.moonraker import (
    MoonrakerClient,
    Request,
    cancel_request,
    check_remote_name,
    heaters_off_request,
    start_request,
)
from print_copilot.printer.status import PrinterStatus, read_status

U1_MACHINE_TYPE = "Snapmaker U1"
PLAN_TTL_S = 600.0
# Nozzle diameters equal within this tolerance.
NOZZLE_TOLERANCE_MM = 0.01


@dataclass
class Check:
    level: Literal["error", "warning", "info"]
    message: str


@dataclass
class PrinterFile:
    name: str
    size_bytes: int
    modified: float


@dataclass
class UploadResult:
    name: str
    size_bytes: int
    request: str
    printer_model: str | None
    started: bool = False  # upload never starts a print


@dataclass
class HeadUse:
    """What the file asks of one head, from Moonraker's metadata of the file."""

    index: int
    filament_type: str | None
    colour: str | None
    weight_g: float
    nozzle_diameter_mm: float | None
    loaded_type: str | None
    loaded_colour: str | None


@dataclass
class StartPlan:
    token: str
    expires_at: float
    base_url: str
    filename: str
    size_bytes: int
    modified: float
    request: str
    estimated_time_s: int | None  # the slicer's estimate, stored in the file
    filament_total_g: float | None
    slicer: str | None
    heads: list[HeadUse]
    checks: list[Check]
    printer_state: str
    note: str = (
        "Nothing has been sent. Show this plan to the user; start only after the user "
        "explicitly confirms this file on this printer."
    )

    @property
    def blocked(self) -> bool:
        return any(c.level == "error" for c in self.checks)


@dataclass
class SentAction:
    action: str
    request: str
    result: Any
    confirmed_by: str | None = None
    notes: list[str] = field(default_factory=list)


_pending: dict[str, StartPlan] = {}


def printer_status(client: MoonrakerClient) -> PrinterStatus:
    return read_status(client)


def list_files(client: MoonrakerClient) -> list[PrinterFile]:
    files = [
        PrinterFile(str(f["path"]), int(f.get("size", 0)), float(f.get("modified", 0.0)))
        for f in client.list_files()
    ]
    return sorted(files, key=lambda f: f.modified, reverse=True)


def upload_gcode(
    client: MoonrakerClient,
    gcode: str | Path,
    remote_name: str | None = None,
    *,
    overwrite: bool = False,
) -> UploadResult:
    """Copy a sliced G-code file into the printer's `gcodes` folder. Never starts it."""
    local = Path(gcode)
    if not local.is_file():
        raise PrinterError(f"G-code file not found: {local}")
    name = check_remote_name(remote_name or local.name)
    model = read_config_block(local).get("printer_model")
    if model is None:
        raise PrinterError(
            f"{local.name} has no printer_model in its config block; "
            "only G-code sliced by Snapmaker Orca for the U1 is sent."
        )
    if model != U1_MACHINE_TYPE:
        raise PrinterError(f"{local.name} was sliced for {model!r}, not {U1_MACHINE_TYPE!r}.")
    if not overwrite and any(f.name == name for f in list_files(client)):
        raise PrinterError(
            f"{name} already exists on the printer. Pick another name or allow overwrite."
        )
    client.upload(local, name)
    uploaded = client.file_metadata(name)
    size = int(uploaded.get("size", -1))
    if size != local.stat().st_size:
        raise PrinterError(
            f"Upload of {name} reports {size} bytes on the printer, {local.stat().st_size} locally."
        )
    return UploadResult(
        name=name,
        size_bytes=size,
        request=f"POST {client.base_url}/server/files/upload (root=gcodes)",
        printer_model=model,
    )


def _split(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if not value:
        return []
    return [part.strip().strip('"') for part in str(value).split(";")]


def _head_uses(meta: dict[str, Any], status: PrinterStatus) -> list[HeadUse]:
    weights = meta.get("filament_weight") or []
    types = _split(meta.get("filament_type"))
    colours = _split(meta.get("filament_colour"))
    nozzles = meta.get("nozzle_diameter_list") or []
    heads = {h.index: h for h in status.heads}
    uses = []
    for index, weight in enumerate(weights):
        if not weight:
            continue
        loaded = heads[index].rfid_filament if index in heads else None
        uses.append(
            HeadUse(
                index=index,
                filament_type=types[index] if index < len(types) else None,
                colour=colours[index] if index < len(colours) else None,
                weight_g=float(weight),
                nozzle_diameter_mm=float(nozzles[index]) if index < len(nozzles) else None,
                loaded_type=loaded.type if loaded else None,
                loaded_colour=loaded.colour if loaded else None,
            )
        )
    return uses


def _checks(meta: dict[str, Any], status: PrinterStatus, uses: list[HeadUse]) -> list[Check]:
    checks: list[Check] = []
    if status.machine_type != U1_MACHINE_TYPE:
        checks.append(Check("error", f"Printer reports {status.machine_type!r}, not a U1."))
    if not status.ready_to_start:
        checks.append(
            Check(
                "error",
                f"Printer is not idle (Klipper {status.klippy_state}, print {status.print_state}).",
            )
        )
    if not uses:
        checks.append(Check("error", "The file's metadata lists no filament per head."))
    heads = {h.index: h for h in status.heads}
    for use in uses:
        label = f"T{use.index}"
        head = heads.get(use.index)
        if head is None:
            checks.append(Check("error", f"{label} is used by the file but not reported."))
            continue
        if (
            use.nozzle_diameter_mm is not None
            and head.nozzle_diameter_mm is not None
            and abs(use.nozzle_diameter_mm - head.nozzle_diameter_mm) > NOZZLE_TOLERANCE_MM
        ):
            checks.append(
                Check(
                    "error",
                    f"{label}: file expects a {use.nozzle_diameter_mm} mm nozzle, "
                    f"printer reports {head.nozzle_diameter_mm} mm.",
                )
            )
        if use.loaded_type is None:
            checks.append(
                Check(
                    "warning",
                    f"{label}: no RFID spool read; check by hand that "
                    f"{use.filament_type or 'the right filament'} {use.colour or ''} is loaded.",
                )
            )
        elif use.filament_type and use.loaded_type.upper() != use.filament_type.upper():
            checks.append(
                Check(
                    "warning",
                    f"{label}: file uses {use.filament_type}, RFID spool is {use.loaded_type}.",
                )
            )
        if use.loaded_colour and use.colour and COLOUR_RE.match(use.colour):
            difference = delta_e(use.colour, use.loaded_colour)
            if difference >= CLEARLY_DIFFERENT_DELTA_E:
                checks.append(
                    Check(
                        "warning",
                        f"{label}: file colour {use.colour}, RFID spool colour "
                        f"{use.loaded_colour} (delta E {difference}).",
                    )
                )
    if meta.get("slicer") and "snapmaker" not in str(meta["slicer"]).lower():
        checks.append(Check("warning", f"File was sliced by {meta['slicer']}, not Snapmaker Orca."))
    return checks


def plan_start(client: MoonrakerClient, filename: str, *, now: float | None = None) -> StartPlan:
    """Build the confirmation plan for starting `filename`. Sends nothing."""
    name = check_remote_name(filename)
    meta = client.file_metadata(name)
    status = read_status(client)
    uses = _head_uses(meta, status)
    request = start_request(name)
    created = time.time() if now is None else now
    plan = StartPlan(
        token=secrets.token_urlsafe(16),
        expires_at=created + PLAN_TTL_S,
        base_url=client.base_url,
        filename=name,
        size_bytes=int(meta.get("size", 0)),
        modified=float(meta.get("modified", 0.0)),
        request=request.describe(client.base_url),
        estimated_time_s=int(meta["estimated_time"]) if meta.get("estimated_time") else None,
        filament_total_g=meta.get("filament_weight_total"),
        slicer=meta.get("slicer"),
        heads=uses,
        checks=_checks(meta, status, uses),
        printer_state=status.print_state,
    )
    if not plan.blocked:
        _pending[plan.token] = plan
    return plan


def take_plan(token: str, *, now: float | None = None) -> StartPlan:
    """Remove and return a pending plan; every token works once."""
    plan = _pending.pop(token, None)
    if plan is None:
        raise ConfirmationError(
            "Unknown or already used confirmation token. Call the start planning step again."
        )
    if (time.time() if now is None else now) > plan.expires_at:
        raise ConfirmationError("The start plan expired. Plan it again and confirm again.")
    return plan


def execute_start(client: MoonrakerClient, plan: StartPlan, *, confirmed_by: str) -> SentAction:
    """Send the planned start after re-checking that nothing changed since the plan."""
    if plan.blocked:
        raise ConfirmationError("This plan has errors and cannot be started.")
    if client.base_url != plan.base_url:
        raise ConfirmationError(f"The plan is for {plan.base_url}, not {client.base_url}.")
    meta = client.file_metadata(plan.filename)
    if int(meta.get("size", 0)) != plan.size_bytes or float(meta.get("modified", 0.0)) != (
        plan.modified
    ):
        raise ConfirmationError(f"{plan.filename} changed on the printer since the plan.")
    status = read_status(client)
    if not status.ready_to_start:
        raise ConfirmationError(
            f"The printer is no longer idle (print {status.print_state}); nothing was sent."
        )
    request = start_request(plan.filename)
    result = client.post(request)
    return SentAction(
        "start_print", request.describe(client.base_url), result, confirmed_by=confirmed_by
    )


def _send(client: MoonrakerClient, action: str, request: Request) -> SentAction:
    result = client.post(request)
    return SentAction(action, request.describe(client.base_url), result)


def cancel_print(client: MoonrakerClient) -> SentAction:
    """Never gated. TO VERIFY ON HARDWARE: not yet sent to a U1 during a print."""
    return _send(client, "cancel_print", cancel_request())


def heaters_off(client: MoonrakerClient) -> SentAction:
    """Never gated. TO VERIFY ON HARDWARE: not yet sent to a U1."""
    return _send(client, "heaters_off", heaters_off_request())
