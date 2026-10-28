"""Find Snapmaker U1 printers on the LAN through mDNS (standard library only).

Verified on a U1 with firmware 2.0.0: the printer answers a multicast PTR query
for `_snapmaker._tcp.local` with TXT records `machine_type=Snapmaker U1`,
`device_name`, `version` and `ip=<current address>`. Moonraker is then reached
on port 80 of that address. The TXT data also carries a serial number and a
user id; they are read past and never returned.
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass

from print_copilot.errors import PrinterError

SERVICE = "_snapmaker._tcp.local"
MDNS_GROUP = ("224.0.0.251", 5353)
DEFAULT_TIMEOUT_S = 2.0
TYPE_A, TYPE_PTR, TYPE_TXT = 1, 12, 16
# Moonraker answers on port 80 and 7125 on the U1; 80 is the documented web port.
MOONRAKER_PORT = 80


@dataclass(frozen=True)
class DiscoveredPrinter:
    device_name: str
    machine_type: str
    firmware_version: str | None
    ip: str

    @property
    def url(self) -> str:
        return f"http://{self.ip}:{MOONRAKER_PORT}"


def build_query(name: str = SERVICE) -> bytes:
    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    # Class IN with the unicast-response bit, so answers come back to our socket.
    return header + labels + struct.pack(">HH", TYPE_PTR, 0x8001)


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    end = None
    for _ in range(128):  # bounded: compression pointers could loop
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            continue
        if length == 0:
            return ".".join(labels), end if end is not None else offset + 1
        labels.append(data[offset + 1 : offset + 1 + length].decode("utf-8", "replace"))
        offset += 1 + length
    raise ValueError("DNS name too long")


def parse_response(data: bytes) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """TXT key/values per record name, and A records (name -> IPv4)."""
    _, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
    if not flags & 0x8000:
        return {}, {}
    offset = 12
    for _ in range(qd):
        _, offset = _read_name(data, offset)
        offset += 4
    txt: dict[str, dict[str, str]] = {}
    addresses: dict[str, str] = {}
    for _ in range(an + ns + ar):
        name, offset = _read_name(data, offset)
        rtype, _, _, length = struct.unpack(">HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + length]
        offset += length
        if rtype == TYPE_TXT:
            values = txt.setdefault(name, {})
            i = 0
            while i < len(rdata):
                item = rdata[i + 1 : i + 1 + rdata[i]].decode("utf-8", "replace")
                i += 1 + rdata[i]
                key, _, value = item.partition("=")
                values[key] = value
        elif rtype == TYPE_A and length == 4:
            addresses[name] = socket.inet_ntoa(rdata)
    return txt, addresses


def printers_from(txt: dict[str, dict[str, str]], source_ip: str) -> list[DiscoveredPrinter]:
    found = []
    for name, values in txt.items():
        if not name.endswith(SERVICE) or "machine_type" not in values:
            continue
        found.append(
            DiscoveredPrinter(
                device_name=values.get("device_name") or name.split(".")[0],
                machine_type=values["machine_type"],
                firmware_version=values.get("version"),
                ip=values.get("ip") or source_ip,
            )
        )
    return found


def discover(timeout_s: float = DEFAULT_TIMEOUT_S) -> list[DiscoveredPrinter]:
    """Every Snapmaker printer that answers within `timeout_s`, one entry per address."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(0.25)
        sock.sendto(build_query(), MDNS_GROUP)
        found: dict[str, DiscoveredPrinter] = {}
        deadline = time.monotonic() + timeout_s
        resent = False
        while time.monotonic() < deadline:
            if not resent and time.monotonic() > deadline - timeout_s / 2:
                sock.sendto(build_query(), MDNS_GROUP)  # one retry, mDNS is lossy
                resent = True
            try:
                data, (source_ip, _) = sock.recvfrom(9000)
            except TimeoutError:
                continue
            try:
                txt, _ = parse_response(data)
            except (ValueError, IndexError, struct.error):
                continue  # someone else's malformed packet
            for printer in printers_from(txt, source_ip):
                found.setdefault(printer.ip, printer)
        return sorted(found.values(), key=lambda p: (p.device_name, p.ip))
    except OSError as exc:
        raise PrinterError(f"mDNS discovery failed on this computer: {exc}") from exc
    finally:
        sock.close()


def find_u1(name: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> DiscoveredPrinter:
    """The one U1 on the LAN, or the one whose device name is `name`. Fails loudly otherwise."""
    printers = [p for p in discover(timeout_s) if p.machine_type == "Snapmaker U1"]
    if name:
        printers = [p for p in printers if p.device_name == name]
    if not printers:
        raise PrinterError(
            "No Snapmaker U1 answered on the network (mDNS _snapmaker._tcp). Check that it is "
            "on and on the same network, or pass the address with --printer-url / "
            "PRINT_COPILOT_PRINTER_URL."
            + (f" Looked for the device name {name!r}." if name else "")
        )
    if len(printers) > 1:
        listed = ", ".join(f"{p.device_name} ({p.ip})" for p in printers)
        raise PrinterError(
            f"Several U1 printers answered: {listed}. Choose one with "
            "PRINT_COPILOT_PRINTER_NAME or --printer-url."
        )
    return printers[0]
