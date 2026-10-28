"""A fake Moonraker that answers like a Snapmaker U1, for tests and demos without a printer.

Response shapes follow what a U1 on firmware 2.0.0 returned (identifiers such
as serial numbers are left out). Uploaded files are kept in memory; their
metadata is derived from the G-code header and config block in the same field
names the U1 uses. Starting a print only flips `print_stats.state` to
`printing`: nothing is simulated beyond that. Every POST is recorded in
`FakeMoonraker.sent` so tests can assert what would have reached a printer.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from print_copilot.gcode.header import parse_gcode_text

HEADS = ("extruder", "extruder1", "extruder2", "extruder3")


def _tag(vendor: str, kind: str, sub: str, rgb: int) -> dict[str, Any]:
    official = vendor != "NONE"
    return {
        "VENDOR": vendor,
        "MANUFACTURER": "Polymaker" if official else "NONE",
        "MAIN_TYPE": kind,
        "SUB_TYPE": sub,
        "RGB_1": rgb,
        "HOTEND_MIN_TEMP": 190 if official else 0,
        "HOTEND_MAX_TEMP": 230 if official else 0,
        "OFFICIAL": official,
    }


def _empty_tag() -> dict[str, Any]:
    return _tag("NONE", "NONE", "NONE", 0xFFFFFF)


@dataclass
class FakeFile:
    data: bytes
    modified: float


@dataclass
class FakeState:
    klippy_state: str = "ready"
    print_state: str = "standby"
    print_filename: str = ""
    machine_type: str = "Snapmaker U1"
    nozzle_mm: tuple[float, float, float, float] = (0.4, 0.4, 0.4, 0.4)
    targets_c: dict[str, float] = field(
        default_factory=lambda: {**dict.fromkeys(HEADS, 0.0), "heater_bed": 0.0}
    )
    tags: list[dict[str, Any]] = field(
        default_factory=lambda: [
            _tag("Snapmaker", "PLA", "SnapSpeed", 0xE2DEDB),
            _tag("Snapmaker", "PLA", "SnapSpeed", 0x080A0D),
            _empty_tag(),
            _empty_tag(),
        ]
    )
    files: dict[str, FakeFile] = field(default_factory=dict)
    print_duration_s: float = 0.0
    current_layer: int = 0
    total_layers: int = 0
    z_mm: float = 0.0


def gcode_metadata(name: str, item: FakeFile) -> dict[str, Any]:
    """Metadata in the U1's field names, from the slicer's own header lines."""
    text = item.data.decode("utf-8", "replace")
    meta: dict[str, Any] = {"filename": name, "size": len(item.data), "modified": item.modified}
    config = dict(re.findall(r"^; (\w+) = (.*)$", text, flags=re.MULTILINE))
    try:
        summary = parse_gcode_text(text)
    except Exception:  # metadata stays partial, like Moonraker with a foreign file
        summary = None
    if summary is not None:
        meta.update(
            slicer="SnapmakerOrca",
            estimated_time=summary.estimated_time_s,
            filament_weight=summary.filament_used_g,
            filament_weight_total=summary.total_filament_g,
            layer_count=summary.layer_count,
        )
    if "filament_type" in config:
        meta["filament_type"] = config["filament_type"]
    if "filament_colour" in config:
        meta["filament_colour"] = config["filament_colour"]
    if "nozzle_diameter" in config:
        meta["nozzle_diameter_list"] = [float(v) for v in config["nozzle_diameter"].split(",")]
    return meta


def _multipart(body: bytes, content_type: str) -> dict[str, tuple[str | None, bytes]]:
    match = re.search(r"boundary=([^;]+)", content_type)
    if not match:
        raise ValueError("no multipart boundary")
    boundary = b"--" + match.group(1).strip('"').encode()
    fields: dict[str, tuple[str | None, bytes]] = {}
    for part in body.split(boundary)[1:]:
        if part.startswith(b"--"):
            break
        head, _, value = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
        disposition = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]*)"', disposition)
        filename = re.search(r'filename="([^"]*)"', disposition)
        if name:
            fields[name.group(1)] = (filename.group(1) if filename else None, value[:-2])
    return fields


class FakeMoonraker:
    """Runs on 127.0.0.1 in a background thread. Use as a context manager."""

    def __init__(self, port: int = 0, state: FakeState | None = None):
        self.state = state or FakeState()
        self.sent: list[str] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeMoonraker:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def __enter__(self) -> FakeMoonraker:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _objects(self, wanted: dict[str, list[str]]) -> dict[str, Any]:
        s = self.state
        printing = s.print_state == "printing"
        available: dict[str, dict[str, Any]] = {
            "webhooks": {"state": s.klippy_state, "state_message": "Printer is ready"},
            "print_stats": {
                "filename": s.print_filename,
                "state": s.print_state,
                "print_duration": s.print_duration_s,
                "info": {"total_layer": s.total_layers, "current_layer": s.current_layer},
            },
            "gcode_move": {"gcode_position": [0.0, 0.0, s.z_mm, 0.0]},
            "virtual_sdcard": {"progress": 0.0, "is_active": printing},
            "toolhead": {"homed_axes": "xyz" if printing else "", "extruder": "extruder"},
            "heater_bed": {"temperature": 24.0, "target": s.targets_c["heater_bed"]},
            "filament_detect": {"info": s.tags},
        }
        for index, head in enumerate(HEADS):
            available[head] = {
                "temperature": 24.0,
                "target": s.targets_c[head],
                "state": "ACTIVATE" if index == 0 else "PARKED",
                "nozzle_diameter": s.nozzle_mm[index],
            }
        result = {}
        for name, fields in wanted.items():
            if name in available:
                obj = available[name]
                result[name] = {k: v for k, v in obj.items() if not fields or k in fields}
        return result

    def handle(self, method: str, path: str, params: dict[str, str], body: bytes, ctype: str):
        """Return (status, result-or-error-message)."""
        s = self.state
        if method == "GET":
            if path == "/printer/info":
                return 200, {
                    "state": s.klippy_state,
                    "state_message": "Printer is ready",
                    "software_version": "2.0.0.205_20260914173503",
                }
            if path == "/server/info":
                return 200, {"klippy_state": s.klippy_state, "moonraker_version": "2.0.0"}
            if path == "/machine/system_info":
                return 200, {
                    "system_info": {
                        "product_info": {
                            "machine_type": s.machine_type,
                            "nozzle_diameter": list(s.nozzle_mm),
                            "firmware_version": "2.0.0",
                        }
                    }
                }
            if path == "/printer/objects/query":
                wanted = {k: [f for f in v.split(",") if f] for k, v in params.items()}
                return 200, {"eventtime": time.monotonic(), "status": self._objects(wanted)}
            if path == "/server/files/list":
                return 200, [
                    {"path": n, "modified": f.modified, "size": len(f.data), "permissions": "rw"}
                    for n, f in s.files.items()
                ]
            if path == "/server/files/metadata":
                name = params.get("filename", "")
                if name not in s.files:
                    return 404, f"Metadata not available for <{name}>"
                return 200, gcode_metadata(name, s.files[name])
            return 404, f"No route {path}"
        if method == "POST":
            self.sent.append(
                f"POST {path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
            )
            if path == "/server/files/upload":
                fields = _multipart(body, ctype)
                filename, data = fields["file"]
                folder = fields.get("path", (None, b""))[1].decode()
                name = f"{folder}/{filename}" if folder else str(filename)
                s.files[name] = FakeFile(data, time.time())
                return 201, {"item": {"path": name, "root": "gcodes"}, "action": "create_file"}
            if path == "/printer/print/start":
                name = params.get("filename", "")
                if name not in s.files:
                    return 400, f"File {name} does not exist"
                if s.print_state not in ("standby", "complete", "cancelled"):
                    return 400, "Printer is busy"
                s.print_state, s.print_filename = "printing", name
                return 200, "ok"
            if path == "/printer/print/cancel":
                if s.print_state not in ("printing", "paused"):
                    return 400, "No print in progress"
                s.print_state = "cancelled"
                s.targets_c = dict.fromkeys(s.targets_c, 0.0)
                return 200, "ok"
            if path == "/printer/gcode/script":
                if params.get("script") == "TURN_OFF_HEATERS":
                    s.targets_c = dict.fromkeys(s.targets_c, 0.0)
                return 200, "ok"
            return 404, f"No route {path}"
        return 405, "Method not allowed"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self) -> None:
                url = urllib.parse.urlsplit(self.path)
                params = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with fake._lock:
                    code, value = fake.handle(
                        self.command, url.path, params, body, self.headers.get("Content-Type", "")
                    )
                if code >= 400:
                    payload = {"error": {"code": code, "message": value}}
                elif url.path == "/server/files/upload":
                    payload = value  # the real upload reply has no `result` wrapper
                else:
                    payload = {"result": value}
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _reply
            do_POST = _reply

            def log_message(self, format: str, *args: Any) -> None:
                pass

        return Handler
