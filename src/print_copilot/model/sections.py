"""Horizontal cross-sections of a mesh and the local thickness of their outlines.

Everything here is geometry, not slicing: the numbers are a **heuristic** used
to decide which settings are worth trying. Whether a thin feature really prints
is shown by the sliced toolpaths, not by this module.

A section at height z is the set of segments where the plane cuts the
triangles. The local thickness at a point of the outline is the distance from
that point, straight inwards (against the face normal projected on the plane),
to the next outline segment: for a strip of width w it is w.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from print_copilot.model.mesh import Mesh

DEFAULT_SECTIONS = 24
DEFAULT_SAMPLES_PER_SECTION = 1500
# Rays start this far inside the outline so they do not hit their own segment.
RAY_START_MM = 1e-4
# Pairs of sample rays and segments handled at once (memory bound).
CHUNK_PAIRS = 4_000_000


@dataclass
class Section:
    z_mm: float
    segments: np.ndarray  # (n, 2, 2): start and end X/Y of each outline segment
    outward: np.ndarray  # (n, 2): unit outward normal of each segment in the plane

    @property
    def lengths_mm(self) -> np.ndarray:
        return np.linalg.norm(self.segments[:, 1] - self.segments[:, 0], axis=1)


def cut(mesh: Mesh, z_mm: float, normals: np.ndarray | None = None) -> Section:
    """The outline of `mesh` at height `z_mm` (mesh coordinates). `normals`
    are the mesh's face normals, if already computed."""
    t = mesh.triangles
    z = t[:, :, 2] - z_mm
    # A vertex exactly on the plane would give zero-length segments; nudge it.
    z = np.where(np.abs(z) < 1e-9, 1e-9, z)
    spans = (z.min(axis=1) < 0) & (z.max(axis=1) > 0)
    t, z = t[spans], z[spans]
    normals = (mesh.normals if normals is None else normals)[spans]
    a = t
    b = np.roll(t, -1, axis=1)
    za, zb = z, np.roll(z, -1, axis=1)
    crosses = za * zb < 0  # (n, 3): exactly two edges per spanning triangle
    # Edges that do not cross divide by zero here; they are never picked below.
    with np.errstate(divide="ignore", invalid="ignore"):
        f = za / (za - zb)
        points = a[:, :, :2] + f[:, :, None] * (b[:, :, :2] - a[:, :, :2])
    order = np.argsort(~crosses, axis=1, kind="stable")[:, :2]
    rows = np.arange(len(t))[:, None]
    segments = points[rows, order]
    flat = normals[:, :2]
    length = np.linalg.norm(flat, axis=1)
    keep = (length > 1e-6) & (np.linalg.norm(segments[:, 1] - segments[:, 0], axis=1) > 1e-9)
    return Section(float(z_mm), segments[keep], flat[keep] / length[keep, None])


def local_thickness(
    section: Section, samples: int = DEFAULT_SAMPLES_PER_SECTION, seed: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """Thickness at up to `samples` outline points, picked at random in
    proportion to segment length (fixed seed, so results repeat), and the
    outline length each point stands for. Points whose inward ray hits nothing
    (open meshes) are left out."""
    lengths = section.lengths_mm
    total = float(lengths.sum())
    if not len(lengths) or total <= 0:
        return np.empty(0), np.empty(0)
    if len(lengths) <= samples:
        picked = np.arange(len(lengths))
        weights = lengths
    else:
        rng = np.random.default_rng(seed)
        picked = rng.choice(len(lengths), size=samples, p=lengths / total)
        weights = np.full(samples, total / samples)
    seg = section.segments
    inward = -section.outward[picked]
    origins = seg[picked].mean(axis=1) + RAY_START_MM * inward
    p, s = seg[:, 0], seg[:, 1] - seg[:, 0]
    thickness = np.full(len(picked), np.inf)
    step = max(1, CHUNK_PAIRS // len(seg))
    for start in range(0, len(picked), step):
        o = origins[start : start + step, None, :]
        d = inward[start : start + step, None, :]
        denom = d[..., 0] * s[None, :, 1] - d[..., 1] * s[None, :, 0]
        w = p[None] - o
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (w[..., 0] * s[None, :, 1] - w[..., 1] * s[None, :, 0]) / denom
            u = (w[..., 0] * d[..., 1] - w[..., 1] * d[..., 0]) / denom
        hit = (np.abs(denom) > 1e-12) & (t > 0) & (u >= 0) & (u <= 1)
        thickness[start : start + step] = np.where(hit, t, np.inf).min(axis=1)
    found = np.isfinite(thickness)
    return thickness[found] + RAY_START_MM, weights[found]


@dataclass
class ThicknessProfile:
    """Local outline thickness sampled on horizontal sections (heuristic)."""

    sections: int
    outline_mm: float  # outline length the samples stand for
    thin_limits_mm: list[float]
    # Share of the sampled outline, by length, thinner than each limit.
    thin_shares: list[float]
    # Heights (mm above the lowest point) of sections with outline thinner
    # than the first limit.
    thin_heights_mm: list[float]
    min_thickness_mm: float | None
    notes: list[str] = field(default_factory=list)


def thickness_profile(
    mesh: Mesh,
    limits_mm: list[float],
    *,
    sections: int = DEFAULT_SECTIONS,
    samples: int = DEFAULT_SAMPLES_PER_SECTION,
) -> ThicknessProfile:
    """Sample `sections` evenly spaced horizontal sections of `mesh` as it will
    be printed (Z up) and measure how much of their outline is thinner than
    each of `limits_mm`."""
    low, high = mesh.bounds_mm
    height = float(high[2] - low[2])
    if height <= 0:
        return ThicknessProfile(0, 0.0, list(limits_mm), [0.0] * len(limits_mm), [], None)
    # Section centres, away from the very bottom and top faces.
    heights = (np.arange(sections) + 0.5) / sections * height
    normals = mesh.normals
    values, weights, thin_at = [], [], []
    for z in heights:
        section = cut(mesh, low[2] + z, normals)
        thickness, weight = local_thickness(section, samples, seed=int(z * 1000))
        values.append(thickness)
        weights.append(weight)
        if len(thickness) and thickness.min() < limits_mm[0]:
            thin_at.append(round(float(z), 1))
    thickness = np.concatenate(values) if values else np.empty(0)
    weight = np.concatenate(weights) if weights else np.empty(0)
    total = float(weight.sum())
    notes = []
    if total <= 0:
        notes.append("No section outline could be measured; the mesh may be open or flat.")
    shares = [
        round(float(weight[thickness < limit].sum()) / total, 4) if total > 0 else 0.0
        for limit in limits_mm
    ]
    return ThicknessProfile(
        sections=sections,
        outline_mm=round(total, 1),
        thin_limits_mm=list(limits_mm),
        thin_shares=shares,
        thin_heights_mm=thin_at,
        min_thickness_mm=round(float(thickness.min()), 3) if len(thickness) else None,
        notes=notes,
    )
