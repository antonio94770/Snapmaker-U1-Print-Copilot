"""Read commanded, layer-tagged extrusion paths, without executing G-code.

Firmware macros are opaque. Feed rates are commands, not achieved speeds.
XY arcs are tessellated for display only; extrusion/rate uses their arc length.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from print_copilot.errors import GcodeParseError

_COMMAND = re.compile(r"^([GMT]\d+(?:\.\d+)?)\b", re.I)
_ARG = re.compile(r"([XYZEFIJRSTP])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))", re.I)


@dataclass(slots=True)
class Segment:
    start: tuple[float, float, float]
    end: tuple[float, float, float]
    tool: int
    feature: str
    feed_mm_s: float | None
    extrusion_mm: float
    source_line: int
    path_start: bool = False
    temperature_c: float | None = None
    length_mm: float = 0.0


@dataclass
class PathLayer:
    number: int
    segments: list[Segment] = field(default_factory=list)

    @property
    def z_mm(self) -> float:
        return min(s.end[2] for s in self.segments)


@dataclass
class Toolpaths:
    layers: list[PathLayer]
    segment_count: int
    notes: list[str]
    macro_names: list[str]
    tagged_layer_count: int


def _arc_points(start, end, args, clockwise, tolerance):
    if args.get("P", 1) != 1:
        raise GcodeParseError("Multi-turn extrusion arcs are not supported by the preview.")
    x, y, z = start
    tx, ty, tz = end
    if "R" in args:
        if "I" in args or "J" in args:
            raise GcodeParseError("Arc mixes radius and centre offsets.")
        radius = abs(args["R"])
        dx, dy = tx - x, ty - y
        chord = math.hypot(dx, dy)
        if chord == 0 or chord > 2 * radius + 1e-6:
            raise GcodeParseError("Invalid radius arc.")
        h = math.sqrt(max(0.0, radius * radius - chord * chord / 4))
        sign = -1 if clockwise else 1
        if args["R"] < 0:
            sign *= -1
        cx = (x + tx) / 2 - sign * h * dy / chord
        cy = (y + ty) / 2 + sign * h * dx / chord
    else:
        if "I" not in args and "J" not in args:
            raise GcodeParseError("Arc has neither radius nor centre offsets.")
        cx, cy = x + args.get("I", 0), y + args.get("J", 0)
        radius = math.hypot(x - cx, y - cy)
        if radius == 0 or abs(math.hypot(tx - cx, ty - cy) - radius) > max(tolerance, 0.01):
            raise GcodeParseError("Arc endpoints do not share a radius.")
    angle = math.atan2(y - cy, x - cx)
    delta = (math.atan2(ty - cy, tx - cx) - angle) % (2 * math.pi)
    if clockwise:
        delta = delta - 2 * math.pi if delta else -2 * math.pi
    elif delta == 0:
        delta = 2 * math.pi
    step = 2 * math.acos(max(-1.0, 1 - min(tolerance / radius, 1)))
    count = max(1, math.ceil(abs(delta) / step))
    if count > 10000:
        raise GcodeParseError("Arc needs more than 10000 display chords; split the input arc.")
    points = [
        (
            cx + radius * math.cos(angle + delta * n / count),
            cy + radius * math.sin(angle + delta * n / count),
            z + (tz - z) * n / count,
        )
        for n in range(1, count + 1)
    ]
    points[-1] = end
    return points, math.hypot(radius * abs(delta), tz - z)


def read_paths(path: str | Path, *, max_segments: int = 1_000_000) -> Toolpaths:
    """Parse G0..G3, G90/G91, G92, M82/M83, Tn and explicit temperatures.

    Only positive-E moves after a ;LAYER_CHANGE are drawn. Unretractions and
    startup/purge-line moves before the first layer are deliberately excluded.
    Unsupported coordinate transforms/units/arc planes fail instead of drawing
    incorrect geometry. Missing layer tags are an error, not an empty preview.
    """
    if max_segments <= 0:
        raise GcodeParseError("max_segments must be positive.")
    xyz = [None, None, None]
    offsets = [0.0, 0.0, 0.0]
    absolute = True
    relative_e = False
    e = 0.0
    tool = 0
    feed = None
    feature = "Unclassified"
    plane = "G17"
    layers: list[PathLayer] = []
    layer = None
    count = 0
    interrupted = True
    temperatures: dict[int, float] = {}
    macros: set[str] = set()
    try:
        stream = Path(path).open(encoding="utf-8", errors="replace")  # noqa: SIM115
    except OSError as exc:
        raise GcodeParseError(f"Cannot read G-code {path}: {exc}") from exc
    with stream:
        for line_no, raw in enumerate(stream, 1):
            if raw.startswith("; CONFIG_BLOCK_START"):
                break
            if raw.startswith(";LAYER_CHANGE"):
                layer = PathLayer(len(layers) + 1)
                layers.append(layer)
                interrupted = True
            if raw.startswith(";TYPE:"):
                feature = raw[6:].strip()
                interrupted = True
            code = raw.split(";", 1)[0].strip().upper()
            if not code:
                continue
            command = _COMMAND.match(code)
            if not command:
                macros.add(code.split()[0])
                # A macro may change position; its expansion is not known here.
                # Subsequent explicit moves re-establish commanded coordinates.
                continue
            cmd = command[1]
            argument_text = code[command.end() :]
            args = {m[1]: float(m[2]) for m in _ARG.finditer(argument_text)}
            if cmd in {"G0", "G1", "G2", "G3"} and _ARG.sub("", argument_text).strip():
                raise GcodeParseError(f"Unsupported motion arguments at line {line_no}: {code}")
            if (
                cmd.startswith("G")
                and cmd
                not in {
                    "G0",
                    "G1",
                    "G2",
                    "G3",
                    "G17",
                    "G18",
                    "G19",
                    "G20",
                    "G21",
                    "G28",
                    "G90",
                    "G91",
                    "G90.1",
                    "G91.1",
                    "G92",
                }
                and "E" in args
            ):
                raise GcodeParseError(f"Unsupported extrusion command {cmd} at line {line_no}.")
            if not all(math.isfinite(v) for v in args.values()):
                raise GcodeParseError(f"Non-finite coordinate at line {line_no}.")
            if cmd in {"G20", "G90.1", "G54", "G55", "G56", "G57", "G58", "G59"}:
                raise GcodeParseError(f"Unsupported coordinate mode {cmd} at line {line_no}.")
            if cmd in {"G17", "G18", "G19"}:
                plane = cmd
            elif cmd in {"G90", "G91"}:
                absolute = cmd == "G90"
            elif cmd in {"M82", "M83"}:
                relative_e = cmd == "M83"
            elif cmd == "G92":
                if "E" in args:
                    e = args["E"]
                for i, axis in enumerate("XYZ"):
                    if axis in args:
                        if xyz[i] is None:
                            raise GcodeParseError(
                                f"G92 {axis} before known position at line {line_no}."
                            )
                        offsets[i] = xyz[i] - args[axis]
            elif cmd == "G28":
                xyz = [None, None, None]
            elif cmd.startswith("T"):
                tool = int(cmd[1:])
                interrupted = True
            elif cmd in {"M104", "M109"} and "S" in args:
                temperatures[int(args.get("T", tool))] = args["S"]
            elif cmd in {"G0", "G1", "G2", "G3"}:
                previous = tuple(xyz)
                for i, axis in enumerate("XYZ"):
                    if axis in args:
                        if not absolute and xyz[i] is None:
                            raise GcodeParseError(
                                f"Relative {axis} before known position at line {line_no}."
                            )
                        xyz[i] = args[axis] + (offsets[i] if absolute else xyz[i])
                if "F" in args:
                    feed = args["F"] / 60
                extrusion = args.get("E", 0) if relative_e else args.get("E", e) - e
                if "E" in args:
                    e = e + args["E"] if relative_e else args["E"]
                xy_move = any(axis in args for axis in "XY") or cmd in {"G2", "G3"}
                if extrusion <= 0 or not xy_move:
                    if xy_move or extrusion < 0:
                        interrupted = True
                    continue
                if layer is None:
                    continue
                if any(v is None for v in (*previous, *xyz)):
                    raise GcodeParseError(
                        f"Extrusion starts at an unknown XYZ position at line {line_no}."
                    )
                end = tuple(xyz)
                points = [end]
                length = math.dist(previous, end)
                if cmd in {"G2", "G3"}:
                    if plane != "G17":
                        raise GcodeParseError(
                            f"Unsupported extrusion arc plane {plane} at line {line_no}."
                        )
                    points, length = _arc_points(previous, end, args, cmd == "G2", 0.05)
                if length <= 0:
                    continue
                for i, point in enumerate(points):
                    count += 1
                    if count > max_segments:
                        raise GcodeParseError(
                            f"Preview exceeds {max_segments} segments; "
                            "increase max_segments explicitly."
                        )
                    # Arc E is distributed uniformly along its equal-angle chords.
                    layer.segments.append(
                        Segment(
                            previous,
                            point,
                            tool,
                            feature,
                            feed,
                            extrusion / len(points),
                            line_no,
                            interrupted and i == 0,
                            temperatures.get(tool),
                            length_mm=length / len(points),
                        )
                    )
                    previous = point
                interrupted = False
    if not layers or not any(layer.segments for layer in layers):
        raise GcodeParseError(
            "No layer-tagged extrusion paths; a trustworthy preview cannot be made."
        )
    # Empty tagged layers still count, but have no drawable Z or geometry.
    return Toolpaths(
        [layer for layer in layers if layer.segments],
        count,
        [
            (
                "Commanded extrusion paths only; firmware macros, physical "
                "defects and achieved speeds are not simulated."
            ),
            "Travel moves and startup extrusion before the first layer are excluded.",
            (
                "Arc display uses chords with 0.05 mm tolerance; path starts are "
                "seam candidates, not confirmed seams."
            ),
        ],
        sorted(macros),
        len(layers),
    )
