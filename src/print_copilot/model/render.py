"""Render a mesh to a PNG contact sheet that an AI agent (or a person) can look at.

Pure numpy software rendering: every triangle is sampled densely enough that
neighbouring samples are less than a pixel apart, and a z-buffer keeps the
sample nearest to the camera. Faces are flat shaded, depth edges are darkened
so small details (a miniature's face) stay readable, and each view shows a
grid in millimetres, the model axes and a label. The PNG is written with the
standard library (zlib), so no imaging dependency is needed.

Views are orthographic. "front" looks from -Y towards +Y with +Z up, the usual
CAD convention; `VIEWS` lists the screen axes of every view.
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

from print_copilot.model.mesh import Mesh

# Screen right and up vectors, in model coordinates. The camera looks along
# -(right x up), i.e. it sits on the side right x up points to.
_ISO = np.array([-1.0, -1.0, 1.0]) / math.sqrt(3)
_ISO_BACK = np.array([1.0, 1.0, 1.0]) / math.sqrt(3)


def _iso_axes(camera: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    right = np.cross([0.0, 0.0, 1.0], camera)
    right /= np.linalg.norm(right)
    return right, np.cross(camera, right)


VIEWS: dict[str, tuple[np.ndarray, np.ndarray]] = {
    "front": (np.array([1.0, 0, 0]), np.array([0, 0, 1.0])),
    "back": (np.array([-1.0, 0, 0]), np.array([0, 0, 1.0])),
    "left": (np.array([0, -1.0, 0]), np.array([0, 0, 1.0])),
    "right": (np.array([0, 1.0, 0]), np.array([0, 0, 1.0])),
    "top": (np.array([1.0, 0, 0]), np.array([0, 1.0, 0])),
    "bottom": (np.array([1.0, 0, 0]), np.array([0, -1.0, 0])),
    "iso": _iso_axes(_ISO),
    "iso_back": _iso_axes(_ISO_BACK),
}
DEFAULT_VIEWS: tuple[str, ...] = ("front", "back", "left", "right", "top", "bottom", "iso")

# Face colours (RGB 0..1).
BASE_COLOR = np.array([0.72, 0.75, 0.80])
OVERHANG_COLOR = np.array([0.90, 0.25, 0.20])  # needs support
PROTECTED_COLOR = np.array([0.25, 0.50, 0.95])  # surface to keep clean
CONFLICT_COLOR = np.array([0.85, 0.20, 0.85])  # protected and needs support
BED_COLOR = np.array([0.30, 0.70, 0.35])  # rests on the bed
BACKGROUND = np.array([0.96, 0.96, 0.95])
GRID = np.array([0.86, 0.86, 0.84])
INK = np.array([0.10, 0.10, 0.12])
AXIS_COLORS = {"X": np.array([0.85, 0.15, 0.15]), "Y": np.array([0.10, 0.60, 0.15]),
               "Z": np.array([0.15, 0.30, 0.90])}  # fmt: skip

CELL_PX = 360
HEADER_PX = 22
MARGIN_PX = 16
MAX_SAMPLES_PER_CHUNK = 4_000_000

# 5x7 bitmap font, one string of 5 bits per row.
_FONT_ROWS = {
    "A": "01110 10001 10001 11111 10001 10001 10001",
    "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110",
    "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111",
    "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111",
    "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110",
    "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001",
    "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001",
    "N": "10001 11001 10101 10011 10001 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110",
    "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101",
    "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110",
    "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110",
    "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 10101 01010",
    "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100",
    "Z": "11111 00001 00010 00100 01000 10000 11111",
    "0": "01110 10001 10011 10101 11001 10001 01110",
    "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111",
    "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010",
    "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110",
    "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110",
    "9": "01110 10001 10001 01111 00001 00010 01100",
    " ": "00000 00000 00000 00000 00000 00000 00000",
    "-": "00000 00000 00000 11111 00000 00000 00000",
    "+": "00000 00100 00100 11111 00100 00100 00000",
    "(": "00010 00100 01000 01000 01000 00100 00010",
    ")": "01000 00100 00010 00010 00010 00100 01000",
    ".": "00000 00000 00000 00000 00000 01100 01100",
    ":": "00000 01100 01100 00000 01100 01100 00000",
    "/": "00000 00001 00010 00100 01000 10000 00000",
    "=": "00000 00000 11111 00000 11111 00000 00000",
}
FONT = {
    ch: np.array([[c == "1" for c in row] for row in rows.split()], dtype=bool)
    for ch, rows in _FONT_ROWS.items()
}


@dataclass
class ViewInfo:
    """Where a view sits in the sheet and how its pixels map to millimetres.

    A model point p appears at column `origin_px[0] + dot(p, right) / mm_per_px`
    and row `origin_px[1] - dot(p, up) / mm_per_px`, relative to the cell's
    top-left corner `cell_px`.
    """

    name: str
    right: list[float]
    up: list[float]
    cell_px: list[int]  # (column, row) of the cell's top-left pixel in the sheet
    origin_px: list[float]  # model origin (0, 0, 0) inside the cell
    mm_per_px: float
    grid_mm: float


@dataclass
class Sheet:
    png: bytes
    width_px: int
    height_px: int
    views: list[ViewInfo]
    legend: dict[str, str] = field(default_factory=dict)


def _nice_grid(mm_per_px: float) -> float:
    """Grid spacing (1-2-5 steps) with lines at least 24 px apart."""
    target = 24 * mm_per_px
    exponent = math.floor(math.log10(target))
    for step in (1, 2, 5, 10):
        if step * 10**exponent >= target:
            return float(step * 10**exponent)
    return float(10 ** (exponent + 1))


def _barycentric_grid(k: int) -> np.ndarray:
    """Barycentric sample points with spacing 1/k, plus the centroid."""
    points = [(i / k, j / k, (k - i - j) / k) for i in range(k + 1) for j in range(k + 1 - i)]
    points.append((1 / 3, 1 / 3, 1 / 3))
    return np.array(points)


def _rasterize(screen: np.ndarray, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    """Z-buffer of triangles in screen space.

    `screen` has shape (n, 3, 3): column, row, depth (larger = nearer) per
    vertex. Returns `(depth, face)` images; `face` is -1 where nothing is drawn.
    """
    depth = np.full(width * height, -np.inf)
    face = np.full(width * height, -1, dtype=np.int64)
    if not len(screen):
        return depth.reshape(height, width), face.reshape(height, width)
    edges = np.stack(
        [screen[:, 1, :2] - screen[:, 0, :2], screen[:, 2, :2] - screen[:, 1, :2],
         screen[:, 0, :2] - screen[:, 2, :2]],
        axis=1,
    )  # fmt: skip
    longest = np.linalg.norm(edges, axis=2).max(axis=1)
    # Power-of-two subdivision levels keep the number of groups small.
    levels = 2 ** np.ceil(np.log2(np.maximum(longest / 0.6, 1.0))).astype(np.int64)
    all_idx, all_depth, all_face = [], [], []
    for k in np.unique(levels):
        bary = _barycentric_grid(int(k))
        members = np.nonzero(levels == k)[0]
        chunk = max(1, MAX_SAMPLES_PER_CHUNK // len(bary))
        for start in range(0, len(members), chunk):
            ids = members[start : start + chunk]
            samples = np.einsum("mj,gjd->gmd", bary, screen[ids]).reshape(-1, 3)
            owner = np.repeat(ids, len(bary))
            col = np.floor(samples[:, 0]).astype(np.int64)
            row = np.floor(samples[:, 1]).astype(np.int64)
            inside = (col >= 0) & (col < width) & (row >= 0) & (row < height)
            idx = row[inside] * width + col[inside]
            z = samples[inside, 2]
            np.maximum.at(depth, idx, z)
            all_idx.append(idx)
            all_depth.append(z)
            all_face.append(owner[inside])
    idx = np.concatenate(all_idx)
    z = np.concatenate(all_depth)
    owners = np.concatenate(all_face)
    winners = z >= depth[idx]
    face[idx[winners]] = owners[winners]
    return depth.reshape(height, width), face.reshape(height, width)


def _draw_text(image: np.ndarray, text: str, col: int, row: int, color, scale: int = 2) -> None:
    for ch in text.upper():
        glyph = FONT.get(ch, FONT[" "])
        big = np.kron(glyph, np.ones((scale, scale), dtype=bool))
        h, w = big.shape
        r0, c0 = max(row, 0), max(col, 0)
        r1, c1 = min(row + h, image.shape[0]), min(col + w, image.shape[1])
        if r1 > r0 and c1 > c0:
            region = big[r0 - row : r1 - row, c0 - col : c1 - col]
            image[r0:r1, c0:c1][region] = color
        col += (5 + 1) * scale


def _draw_line(image: np.ndarray, p0, p1, color, width: int = 2) -> None:
    steps = int(max(abs(p1[0] - p0[0]), abs(p1[1] - p0[1]))) + 1
    cols = np.linspace(p0[0], p1[0], steps).round().astype(int)
    rows = np.linspace(p0[1], p1[1], steps).round().astype(int)
    for dr in range(width):
        for dc in range(width):
            r, c = rows + dr, cols + dc
            ok = (r >= 0) & (r < image.shape[0]) & (c >= 0) & (c < image.shape[1])
            image[r[ok], c[ok]] = color


def _render_cell(
    mesh: Mesh,
    face_colors: np.ndarray,
    name: str,
    mm_per_px: float,
    centre: np.ndarray,
) -> tuple[np.ndarray, ViewInfo]:
    right, up = VIEWS[name]
    camera = np.cross(right, up)
    size = CELL_PX
    body = size - HEADER_PX
    tri = mesh.triangles
    s = tri @ right
    t = tri @ up
    cs, ct = float(centre @ right), float(centre @ up)
    col = (s - cs) / mm_per_px + size / 2
    row = (ct - t) / mm_per_px + HEADER_PX + body / 2
    screen = np.stack([col, row, tri @ camera], axis=2)
    depth, face = _rasterize(screen, size, size)

    image = np.empty((size, size, 3))
    image[:] = BACKGROUND
    grid_mm = _nice_grid(mm_per_px)
    origin_col = -cs / mm_per_px + size / 2
    origin_row = ct / mm_per_px + HEADER_PX + body / 2
    grid_cols = (np.arange(size) - origin_col) * mm_per_px / grid_mm
    grid_rows = (origin_row - np.arange(size)) * mm_per_px / grid_mm
    on_col = np.abs(grid_cols - np.round(grid_cols)) * grid_mm / mm_per_px < 0.5
    on_row = np.abs(grid_rows - np.round(grid_rows)) * grid_mm / mm_per_px < 0.5
    grid = on_row[:, None] | on_col[None, :]
    image[grid & (face < 0)] = GRID

    drawn = face >= 0
    if drawn.any():
        normals = mesh.normals[face[drawn]]
        shade = 0.35 + 0.65 * np.abs(normals @ camera)
        image[drawn] = face_colors[face[drawn]] * shade[:, None]
        # Darken creases and depth steps so silhouettes and fine relief show.
        # The second difference of depth is ~0 on smooth surfaces, however steep.
        edges = np.zeros_like(drawn)
        threshold = 2 * mm_per_px
        for axis in (0, 1):
            ahead = np.roll(depth, -1, axis=axis)
            behind = np.roll(depth, 1, axis=axis)
            ahead_drawn = np.roll(drawn, -1, axis=axis)
            behind_drawn = np.roll(drawn, 1, axis=axis)
            both = drawn & ahead_drawn & behind_drawn
            curvature = np.zeros_like(depth)
            curvature[both] = np.abs(ahead[both] - 2 * depth[both] + behind[both])
            edges |= curvature > threshold
            edges |= drawn & ~(ahead_drawn & behind_drawn)
        image[edges] *= 0.5

    image[:HEADER_PX] = np.array([0.88, 0.88, 0.86])
    _draw_text(image, name.replace("_", " "), 6, 4, INK)
    _draw_text(image, f"GRID {grid_mm:g} MM", size - 6 - 12 * 12, 4, INK)
    # Axis triad in the lower left corner.
    base = (28, size - 28)
    for axis_name, axis in zip("XYZ", np.eye(3), strict=True):
        dx, dy = float(axis @ right), -float(axis @ up)
        if math.hypot(dx, dy) < 0.15:
            continue  # axis points at the viewer
        end = (base[0] + 20 * dx, base[1] + 20 * dy)
        _draw_line(image, base, end, AXIS_COLORS[axis_name])
        _draw_text(image, axis_name, int(end[0] + 3 * dx) - 2, int(end[1] + 3 * dy) - 3,
                   AXIS_COLORS[axis_name], scale=1)  # fmt: skip
    _draw_line(image, (0, 0), (size - 1, 0), GRID, width=1)
    _draw_line(image, (0, 0), (0, size - 1), GRID, width=1)

    info = ViewInfo(
        name=name,
        right=[float(v) + 0.0 for v in np.round(right, 4)],
        up=[float(v) + 0.0 for v in np.round(up, 4)],
        cell_px=[0, 0],
        origin_px=[round(origin_col, 1), round(origin_row, 1)],
        mm_per_px=round(mm_per_px, 4),
        grid_mm=grid_mm,
    )
    return image, info


def _legend_cell(entries: list[tuple[np.ndarray, str]]) -> np.ndarray:
    image = np.empty((CELL_PX, CELL_PX, 3))
    image[:] = BACKGROUND
    image[:HEADER_PX] = np.array([0.88, 0.88, 0.86])
    _draw_text(image, "LEGEND", 6, 4, INK)
    for i, (color, text) in enumerate(entries):
        row = HEADER_PX + 16 + i * 30
        image[row : row + 18, 12:30] = color
        _draw_text(image, text, 40, row + 2, INK)
    return image


def _hex(color: np.ndarray) -> str:
    r, g, b = (round(float(v) * 255) for v in color)
    return f"#{r:02x}{g:02x}{b:02x}"


def encode_png(image: np.ndarray) -> bytes:
    """RGB float image (h, w, 3) in 0..1 to PNG bytes."""
    data = (np.clip(image, 0, 1) * 255 + 0.5).astype(np.uint8)
    height, width, _ = data.shape
    raw = np.concatenate([np.zeros((height, 1), np.uint8), data.reshape(height, -1)], axis=1)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
        + chunk(b"IEND", b"")
    )


def render_sheet(
    mesh: Mesh,
    *,
    views: list[str] | tuple[str, ...] = DEFAULT_VIEWS,
    face_colors: np.ndarray | None = None,
    legend: list[tuple[np.ndarray, str]] | None = None,
    columns: int = 4,
) -> Sheet:
    """Render `views` of `mesh` side by side, at one common scale.

    `face_colors` (n, 3) colours each triangle; `legend` adds a cell that
    explains them.
    """
    unknown = [v for v in views if v not in VIEWS]
    if unknown or not views:
        raise ValueError(f"Unknown view(s) {unknown}; choose from {', '.join(VIEWS)}.")
    colors = np.tile(BASE_COLOR, (mesh.face_count, 1)) if face_colors is None else face_colors
    low, high = mesh.bounds_mm
    centre = (low + high) / 2
    extent = 0.0
    for name in views:
        right, up = VIEWS[name]
        points = mesh.triangles.reshape(-1, 3)
        extent = max(extent, np.ptp(points @ right), np.ptp(points @ up))
    usable = CELL_PX - HEADER_PX - 2 * MARGIN_PX
    mm_per_px = max(extent, 1e-6) / usable

    cells: list[np.ndarray] = []
    infos: list[ViewInfo] = []
    for name in views:
        image, info = _render_cell(mesh, colors, name, mm_per_px, centre)
        cells.append(image)
        infos.append(info)
    if legend:
        cells.append(_legend_cell(legend))
    columns = min(columns, len(cells))
    rows = math.ceil(len(cells) / columns)
    sheet = np.empty((rows * CELL_PX, columns * CELL_PX, 3))
    sheet[:] = BACKGROUND
    for index, image in enumerate(cells):
        r, c = divmod(index, columns)
        sheet[r * CELL_PX : (r + 1) * CELL_PX, c * CELL_PX : (c + 1) * CELL_PX] = image
        if index < len(infos):
            infos[index].cell_px = [c * CELL_PX, r * CELL_PX]
    return Sheet(
        png=encode_png(sheet),
        width_px=sheet.shape[1],
        height_px=sheet.shape[0],
        views=infos,
        legend={text: _hex(color) for color, text in legend or []},
    )
