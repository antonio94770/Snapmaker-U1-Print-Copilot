"""Where objects cover the bed, on a grid, and free rectangles next to them.

Used to find room for the prime tower. The footprint is the projection of
every triangle onto the bed, so overhangs count as covered (the tower must
not pass under them either).
"""

from __future__ import annotations

import numpy as np


def occupancy(
    triangles: np.ndarray,
    origin: tuple[float, float],
    size: tuple[float, float],
    cell_mm: float = 2.0,
) -> np.ndarray:
    """Boolean grid `[ix, iy]`, True where a triangle's projection covers the
    cell centre or a vertex falls into the cell."""
    nx, ny = int(np.ceil(size[0] / cell_mm)), int(np.ceil(size[1] / cell_mm))
    grid = np.zeros((nx, ny), dtype=bool)
    xy = (np.asarray(triangles, dtype=np.float64)[:, :, :2] - np.array(origin)) / cell_mm
    if not len(xy):
        return grid

    def mark(points: np.ndarray) -> None:
        ix = np.clip(np.floor(points[:, 0]).astype(int), 0, nx - 1)
        iy = np.clip(np.floor(points[:, 1]).astype(int), 0, ny - 1)
        grid[ix, iy] = True

    mark(xy.reshape(-1, 2))
    mark(xy.mean(axis=1))
    low, high = xy.min(axis=1), xy.max(axis=1)
    big = np.nonzero(((high - low) > 1).any(axis=1))[0]
    for index in big:
        a, b, c = xy[index]
        x0, y0 = np.maximum(np.floor(low[index]).astype(int), 0)
        x1, y1 = np.minimum(np.ceil(high[index]).astype(int), [nx, ny])
        if x1 <= x0 or y1 <= y0:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5, indexing="ij")
        v0, v1 = b - a, c - a
        denom = v0[0] * v1[1] - v1[0] * v0[1]
        if abs(denom) < 1e-12:
            continue
        px, py = gx - a[0], gy - a[1]
        u = (px * v1[1] - v1[0] * py) / denom
        v = (v0[0] * py - px * v0[1]) / denom
        inside = (u >= 0) & (v >= 0) & (u + v <= 1)
        grid[x0:x1, y0:y1] |= inside
    return grid


def _window_sum(grid: np.ndarray, w: int, d: int) -> np.ndarray:
    """Sum of `grid` over every w x d window; result `[ix, iy]` = window at ix, iy."""
    padded = np.pad(grid.astype(np.int32), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return padded[w:, d:] - padded[:-w, d:] - padded[w:, :-d] + padded[:-w, :-d]


def free_rectangle(
    grid: np.ndarray,
    width_cells: int,
    depth_cells: int,
    margin_cells: int,
) -> tuple[int, int] | None:
    """Lower-left cell of a free `width x depth` rectangle at least
    `margin_cells` from every covered cell and from the bed edge, nearest a bed
    corner; None if there is none."""
    nx, ny = grid.shape
    w, d = width_cells + 2 * margin_cells, depth_cells + 2 * margin_cells
    if w > nx or d > ny:
        return None
    covered = _window_sum(grid, w, d)
    free = np.argwhere(covered == 0)
    if not len(free):
        return None
    corners = np.array([[0, ny - d], [nx - w, ny - d], [0, 0], [nx - w, 0]])
    distance = np.min(np.abs(free[:, None, :] - corners[None, :, :]).sum(axis=2), axis=1)
    ix, iy = free[int(np.argmin(distance))]
    return int(ix) + margin_cells, int(iy) + margin_cells
