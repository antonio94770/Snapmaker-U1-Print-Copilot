"""Minimal Moonraker HTTP client for the Snapmaker U1 (standard library only).

Verified on a U1 with firmware 2.0.0 (Moonraker 2.0.0, API 1.4.0): Moonraker
answers on port 80 and on port 7125 without login (`/access/info` reports
`login_required: false`). The read-only endpoints used here were all called on
that printer. Upload, start, cancel and G-code scripts follow the standard
Moonraker API and have not been sent to the U1 yet: TO VERIFY ON HARDWARE.

Without an explicit address the U1 is found through mDNS on every new process
(`print_copilot.printer.discovery`), so no address has to be stored.

This module only transports requests. Which requests are allowed without the
user's confirmation is decided in `print_copilot.printer.actions`.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from print_copilot.errors import PrinterError
from print_copilot.printer.discovery import find_u1

ENV_PRINTER_URL = "PRINT_COPILOT_PRINTER_URL"
# Device name advertised over mDNS, to choose among several U1 printers.
ENV_PRINTER_NAME = "PRINT_COPILOT_PRINTER_NAME"
DEFAULT_TIMEOUT_S = 10.0
_discovered: str | None = None
# Uploads of large multi-colour G-code take longer than status calls.
UPLOAD_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class Request:
    """One HTTP request, kept so a confirmation gate can show it verbatim."""

    method: str
    path: str
    params: dict[str, str] | None = None
    note: str | None = None

    def url(self, base_url: str) -> str:
        query = f"?{urllib.parse.urlencode(self.params)}" if self.params else ""
        return f"{base_url}{self.path}{query}"

    def describe(self, base_url: str) -> str:
        return f"{self.method} {self.url(base_url)}"


def discovered_url(*, refresh: bool = False) -> str:
    """The U1 found through mDNS, cached for this process until `refresh`."""
    global _discovered
    if _discovered is None or refresh:
        _discovered = find_u1(os.environ.get(ENV_PRINTER_NAME) or None).url
    return _discovered


def forget_discovered() -> None:
    global _discovered
    _discovered = None


def resolve_url(url: str | None = None) -> str:
    """Base URL from the argument, `PRINT_COPILOT_PRINTER_URL`, or mDNS discovery.

    Returned without a trailing slash.
    """
    url = url or os.environ.get(ENV_PRINTER_URL)
    if not url:
        return discovered_url()
    if "://" not in url:
        url = f"http://{url}"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PrinterError(f"Not a printer URL: {url!r}")
    if parsed.path not in ("", "/") or parsed.query:
        raise PrinterError(f"Give the printer address without a path: {url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def check_remote_name(name: str) -> str:
    """A file name inside the printer's `gcodes` root: relative, no `..`, ends in .gcode."""
    path = PurePosixPath(name.replace("\\", "/"))
    if not name or path.is_absolute() or ".." in path.parts or name.startswith("/"):
        raise PrinterError(f"Invalid printer file name: {name!r}")
    if path.suffix.lower() != ".gcode":
        raise PrinterError(f"Only .gcode files can be sent to the printer, not {name!r}")
    return str(path)


class MoonrakerClient:
    """JSON over HTTP to one Moonraker instance. Every call returns the `result` field."""

    def __init__(self, url: str | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.discovered = not (url or os.environ.get(ENV_PRINTER_URL))
        self.base_url = resolve_url(url)
        self.timeout_s = timeout_s

    def _send(
        self,
        request: Request,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float | None = None,
        unwrapped_key: str | None = None,
    ) -> Any:
        http_request = urllib.request.Request(
            request.url(self.base_url), data=body, method=request.method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(http_request, timeout=timeout_s or self.timeout_s) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            raise PrinterError(
                f"{request.method} {request.path} failed with HTTP {exc.code}: "
                f"{_error_message(exc.read())}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if self.discovered:
                forget_discovered()  # the address may have changed; rediscover next time
            raise PrinterError(
                f"Cannot reach the printer at {self.base_url} ({reason}). "
                "Check that it is on and on the same network."
            ) from exc
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise PrinterError(f"{request.path} returned something that is not JSON") from exc
        if unwrapped_key is not None:
            if not isinstance(data, dict) or unwrapped_key not in data:
                raise PrinterError(
                    f"{request.path} returned JSON without an {unwrapped_key!r} field"
                )
            return data
        if not isinstance(data, dict) or "result" not in data:
            raise PrinterError(f"{request.path} returned JSON without a 'result' field")
        return data["result"]

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        return self._send(Request("GET", path, params))

    def post(self, request: Request) -> Any:
        return self._send(request)

    # Read-only calls, all verified on the U1.

    def server_info(self) -> dict[str, Any]:
        return self.get("/server/info")

    def printer_info(self) -> dict[str, Any]:
        return self.get("/printer/info")

    def system_info(self) -> dict[str, Any]:
        return self.get("/machine/system_info")["system_info"]

    def query(self, objects: dict[str, list[str] | None]) -> dict[str, Any]:
        """`/printer/objects/query`; None asks for every field of that object."""
        params = {name: ",".join(fields) if fields else "" for name, fields in objects.items()}
        return self.get("/printer/objects/query", params)["status"]

    def list_files(self) -> list[dict[str, Any]]:
        return self.get("/server/files/list", {"root": "gcodes"})

    def file_metadata(self, name: str) -> dict[str, Any]:
        return self.get("/server/files/metadata", {"filename": check_remote_name(name)})

    def upload(self, local: Path, remote_name: str) -> dict[str, Any]:
        """Multipart upload into `gcodes`. Never asks Moonraker to start the file.

        Standard Moonraker `POST /server/files/upload`. Unlike every other
        endpoint its reply is not wrapped in `result`: it is the object itself,
        `{"item": {...}, "action": "create_file", ...}` (verified on the U1,
        firmware 2.0.0, 2026-10-28).
        """
        remote = PurePosixPath(check_remote_name(remote_name))
        boundary = f"----print-copilot-{secrets.token_hex(12)}"
        fields = [("root", "gcodes")]
        if str(remote.parent) != ".":
            fields.append(("path", str(remote.parent)))
        parts: list[bytes] = []
        for key, value in fields:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{remote.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
        )
        parts.append(local.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        return self._send(
            Request("POST", "/server/files/upload"),
            body=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout_s=UPLOAD_TIMEOUT_S,
            unwrapped_key="item",
        )


def _error_message(payload: bytes) -> str:
    try:
        error = json.loads(payload).get("error", {})
        return str(error.get("message") or error)
    except (ValueError, AttributeError):
        return payload[:200].decode("utf-8", "replace")


# Requests that change the printer, built here so gates and callers show the same thing.


def start_request(name: str) -> Request:
    """Standard Moonraker print start. TO VERIFY ON HARDWARE: the U1 may expect more
    (Snapmaker Orca's own send-to-printer path has not been inspected)."""
    return Request("POST", "/printer/print/start", {"filename": check_remote_name(name)})


def cancel_request() -> Request:
    return Request("POST", "/printer/print/cancel", note="runs CANCEL_PRINT")


def heaters_off_request() -> Request:
    # TURN_OFF_HEATERS is listed by /printer/gcode/help on the U1.
    return Request(
        "POST", "/printer/gcode/script", {"script": "TURN_OFF_HEATERS"}, note="all heaters to 0"
    )
