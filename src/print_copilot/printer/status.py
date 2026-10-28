"""Read-only printer status: state, temperatures, the four heads and their spools.

Object and field names below were read from a U1 on firmware 2.0.0. The order of
`filament_detect.info` follows the heads (entry 0 = T0 = `extruder`; verified
2026-10-28: the black spool read on entry 1 printed from T1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from print_copilot.errors import PrinterError
from print_copilot.printer.moonraker import MoonrakerClient

HEAD_OBJECTS = ("extruder", "extruder1", "extruder2", "extruder3")
HEAD_FIELDS = ["temperature", "target", "state", "nozzle_diameter"]
# print_stats.state values in which nothing is printing or paused.
IDLE_PRINT_STATES = ("standby", "complete", "cancelled")


@dataclass
class LoadedFilament:
    """A spool as identified by the head's RFID reader (empty tags are left out)."""

    vendor: str
    manufacturer: str
    type: str
    sub_type: str
    colour: str
    official: bool
    hotend_min_c: int | None
    hotend_max_c: int | None


@dataclass
class HeadStatus:
    index: int  # 0-based, as in the G-code (T0..T3); the U1 labels heads 1..4
    object_name: str
    temperature_c: float
    target_c: float
    state: str | None  # ACTIVATE for the head on the carriage, PARKED otherwise
    nozzle_diameter_mm: float | None
    rfid_filament: LoadedFilament | None


@dataclass
class PrinterStatus:
    base_url: str
    machine_type: str | None
    firmware_version: str | None
    klippy_state: str
    state_message: str
    print_state: str
    print_filename: str | None
    progress: float
    print_duration_s: float
    homed_axes: str
    active_head: str | None
    bed_temperature_c: float | None
    bed_target_c: float | None
    heads: list[HeadStatus] = field(default_factory=list)

    @property
    def ready_to_start(self) -> bool:
        return self.klippy_state == "ready" and self.print_state in IDLE_PRINT_STATES


def parse_filament(info: dict[str, Any]) -> LoadedFilament | None:
    vendor = str(info.get("VENDOR", "NONE"))
    if vendor == "NONE" and str(info.get("MAIN_TYPE", "NONE")) == "NONE":
        return None
    rgb = info.get("RGB_1")
    return LoadedFilament(
        vendor=vendor,
        manufacturer=str(info.get("MANUFACTURER", "")),
        type=str(info.get("MAIN_TYPE", "")),
        sub_type=str(info.get("SUB_TYPE", "")),
        colour=f"#{int(rgb):06X}" if isinstance(rgb, int) else "",
        official=bool(info.get("OFFICIAL", False)),
        hotend_min_c=info.get("HOTEND_MIN_TEMP") or None,
        hotend_max_c=info.get("HOTEND_MAX_TEMP") or None,
    )


def read_status(client: MoonrakerClient) -> PrinterStatus:
    info = client.printer_info()
    product = client.system_info().get("product_info", {})
    objects: dict[str, list[str] | None] = {
        "print_stats": ["state", "filename", "print_duration"],
        "virtual_sdcard": ["progress"],
        "toolhead": ["homed_axes", "extruder"],
        "heater_bed": ["temperature", "target"],
        "filament_detect": ["info"],
    }
    objects.update({name: HEAD_FIELDS for name in HEAD_OBJECTS})
    status = client.query(objects)
    if "print_stats" not in status:
        raise PrinterError("The printer did not report print_stats; is Klipper running?")
    stats = status["print_stats"]
    tags = status.get("filament_detect", {}).get("info") or []
    heads = []
    for index, name in enumerate(HEAD_OBJECTS):
        head = status.get(name)
        if head is None:
            continue
        tag = tags[index] if index < len(tags) and isinstance(tags[index], dict) else None
        heads.append(
            HeadStatus(
                index=index,
                object_name=name,
                temperature_c=float(head.get("temperature", 0.0)),
                target_c=float(head.get("target", 0.0)),
                state=head.get("state"),
                nozzle_diameter_mm=head.get("nozzle_diameter"),
                rfid_filament=parse_filament(tag) if tag else None,
            )
        )
    bed = status.get("heater_bed", {})
    toolhead = status.get("toolhead", {})
    return PrinterStatus(
        base_url=client.base_url,
        machine_type=product.get("machine_type"),
        firmware_version=product.get("firmware_version"),
        klippy_state=str(info.get("state", "unknown")),
        state_message=str(info.get("state_message", "")),
        print_state=str(stats.get("state", "unknown")),
        print_filename=stats.get("filename") or None,
        progress=float(status.get("virtual_sdcard", {}).get("progress", 0.0)),
        print_duration_s=float(stats.get("print_duration", 0.0)),
        homed_axes=str(toolhead.get("homed_axes", "")),
        active_head=toolhead.get("extruder"),
        bed_temperature_c=bed.get("temperature"),
        bed_target_c=bed.get("target"),
        heads=heads,
    )
