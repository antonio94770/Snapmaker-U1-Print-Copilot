"""Candidate print orientations, an overhang heuristic and quality goals.

Everything here is geometry, not slicing: the numbers are a **heuristic** used
to choose which orientations are worth slicing. Measured time, grams and
support material come only from real slices (`actions.optimize_orientation`).

A candidate is described by the direction, in the model's own coordinates,
that ends up pointing down at the bed. Rotation about Z does not change
overhangs, so it is left alone (the Snapmaker Orca CLI centres the model on the
plate by itself, verified on 01.10.01.50).

Overhang rule (same meaning as OrcaSlicer's `support_threshold_angle`): a
downward-facing surface needs support when its slope, measured from the
horizontal plane, is below the threshold angle.

Protected regions are surfaces that must print cleanly, chosen by the user or
by an agent that looked at the rendered model (a miniature's face, a logo, a
visible side). For the `quality` goal they should not need support, should not
rest on the bed and should not be shallow upward slopes where layer steps show.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from print_copilot.model.mesh import Mesh

DEFAULT_THRESHOLD_DEG = 30.0
# Faces whose centroid is this close to the lowest point rest on the bed.
BED_CONTACT_TOL_MM = 0.1
# Down directions closer than this are the same candidate.
SAME_DIRECTION_DEG = 1.0
# How many flat-face directions (largest contact area first) become candidates.
DEFAULT_FLAT_FACE_CANDIDATES = 6
# Upward surfaces with a slope from horizontal in this range show layer steps
# ("staircase effect") most; flat tops are printed as solid top layers.
STAIR_MIN_DEG = 2.0
STAIR_MAX_DEG = 30.0
# Downward surfaces that need no support but slope less than this from horizontal
# print as visible layer steps: each layer overhangs the one below by
# layer_height / tan(slope) (geometry, not a slicer figure).
STEPPED_OVERHANG_MAX_SLOPE_DEG = 45.0
# Below this area the note is not worth the user's attention.
STEPPED_OVERHANG_NOTE_MIN_MM2 = 100.0
# Faces exactly on the support threshold count as stepped whichever side float
# rounding puts them on.
_THRESHOLD_EPSILON = 1e-4
# Elevations (degrees above horizontal) a protected surface is turned to when
# extra candidates are generated for it, and how many azimuths per elevation.
REGION_ELEVATIONS_DEG: tuple[float, ...] = (0.0, 30.0, 45.0, 60.0, 90.0)
REGION_AZIMUTHS = 4
# An orientation is "stable" when its bed contact is at least this share of
# its X/Y bounding box area; less means it stands on an edge or a point and
# relies on supports to stay put (heuristic).
STABLE_CONTACT_RATIO = 0.05

# Weights of the quality cost (mm² of surface, heuristic). A support scar or a
# bed mark on a protected surface counts ten times one elsewhere; layer steps
# count less than support scars.
QUALITY_WEIGHTS: dict[str, float] = {
    "protected_overhang_area_mm2": 10.0,
    "protected_bed_contact_area_mm2": 10.0,
    "protected_stair_area_mm2": 3.0,
    "overhang_area_mm2": 1.0,
    "stair_area_mm2": 0.5,
}

Goal = Literal["speed", "quality", "min_support"]
GOALS: tuple[str, ...] = ("speed", "quality", "min_support")

HEURISTIC_LABEL = (
    "heuristic, not a slice: overhang area is the surface steeper than the support "
    "threshold facing down; support volume is the volume of vertical columns from those "
    f"surfaces to the bed, ignoring parts of the model below them, so it overestimates; "
    f"stair area is upward surface sloped {STAIR_MIN_DEG:g}-{STAIR_MAX_DEG:g} degrees from "
    "horizontal, where layer steps show most"
)

DOWN = np.array([0.0, 0.0, -1.0])

AXES: tuple[tuple[str, tuple[float, float, float]], ...] = (
    ("-Z", (0.0, 0.0, -1.0)),
    ("+Z", (0.0, 0.0, 1.0)),
    ("-X", (-1.0, 0.0, 0.0)),
    ("+X", (1.0, 0.0, 0.0)),
    ("-Y", (0.0, -1.0, 0.0)),
    ("+Y", (0.0, 1.0, 0.0)),
)


@dataclass
class Region:
    """A surface to keep clean, in the model's coordinates as loaded.

    A face belongs to the region when its outward normal is within `angle_deg`
    of `direction` (if given) and its centroid lies inside the box (if given).
    At least one of the two must be set.
    """

    label: str = "protected"
    direction: list[float] | None = None
    angle_deg: float = 60.0
    box_min_mm: list[float] | None = None
    box_max_mm: list[float] | None = None

    def validate(self) -> None:
        if self.direction is None and self.box_min_mm is None and self.box_max_mm is None:
            raise ValueError(f"Region {self.label!r} needs a direction, a box or both.")
        if self.direction is not None and (
            len(self.direction) != 3 or not np.linalg.norm(self.direction) > 0
        ):
            raise ValueError(f"Region {self.label!r}: direction must be a non-zero 3-vector.")
        for name in ("box_min_mm", "box_max_mm"):
            value = getattr(self, name)
            if value is not None and len(value) != 3:
                raise ValueError(f"Region {self.label!r}: {name} must have 3 numbers.")
        if not 0 < self.angle_deg <= 180:
            raise ValueError(f"Region {self.label!r}: angle_deg must be in (0, 180].")

    def mask(self, mesh: Mesh) -> np.ndarray:
        self.validate()
        selected = np.ones(mesh.face_count, dtype=bool)
        if self.direction is not None:
            direction = np.asarray(self.direction, dtype=np.float64)
            direction /= np.linalg.norm(direction)
            selected &= mesh.normals @ direction >= math.cos(math.radians(self.angle_deg))
        centroids = mesh.triangles.mean(axis=1)
        if self.box_min_mm is not None:
            selected &= (centroids >= np.asarray(self.box_min_mm)).all(axis=1)
        if self.box_max_mm is not None:
            selected &= (centroids <= np.asarray(self.box_max_mm)).all(axis=1)
        return selected

    def mean_normal(self, mesh: Mesh) -> np.ndarray | None:
        """Area-weighted mean outward normal of the region, or None if empty/flat."""
        mask = self.mask(mesh)
        weighted = (mesh.normals[mask] * mesh.areas_mm2[mask, None]).sum(axis=0)
        length = np.linalg.norm(weighted)
        return weighted / length if length > 1e-9 else None


def regions_mask(mesh: Mesh, regions: list[Region] | None) -> np.ndarray | None:
    if not regions:
        return None
    mask = np.zeros(mesh.face_count, dtype=bool)
    for region in regions:
        mask |= region.mask(mesh)
    return mask


def rotation_to_bed(down: np.ndarray) -> np.ndarray:
    """3x3 rotation that turns the unit vector `down` into -Z (Rodrigues)."""
    a = np.asarray(down, dtype=np.float64)
    a = a / np.linalg.norm(a)
    v = np.cross(a, DOWN)
    c = float(np.dot(a, DOWN))
    if c < -1.0 + 1e-9:
        # `down` is +Z: turn the model upside down about X.
        return np.diag([1.0, -1.0, -1.0])
    vx = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])
    return np.eye(3) + vx + vx @ vx / (1.0 + c)


def _direction_label(down: np.ndarray) -> str:
    for name, axis in AXES:
        if np.dot(down, axis) > math.cos(math.radians(0.01)):
            return f"{name} down"
    x, y, z = (round(float(c), 3) + 0.0 for c in down)
    return f"({x}, {y}, {z}) down"


@dataclass
class FaceClasses:
    """Per-face booleans for one orientation."""

    on_bed: np.ndarray
    needs_support: np.ndarray
    stair: np.ndarray
    height_above_bed_mm: np.ndarray
    size_mm: list[float]
    stepped_overhang: np.ndarray | None = None


def classify_faces(
    mesh: Mesh, rotation: np.ndarray, *, threshold_deg: float = DEFAULT_THRESHOLD_DEG
) -> FaceClasses:
    rotated = mesh.transformed(rotation)
    normals = rotated.normals
    low, high = rotated.bounds_mm
    height = rotated.triangles[:, :, 2].mean(axis=1) - low[2]
    facing_down = -normals[:, 2]
    on_bed = (facing_down > math.cos(math.radians(SAME_DIRECTION_DEG))) & (
        height < BED_CONTACT_TOL_MM
    )
    needs_support = (facing_down > math.cos(math.radians(threshold_deg))) & (
        height >= BED_CONTACT_TOL_MM
    )
    facing_up = normals[:, 2]
    stair = (facing_up > math.cos(math.radians(STAIR_MAX_DEG))) & (
        facing_up < math.cos(math.radians(STAIR_MIN_DEG))
    )
    stepped = (
        (facing_down > math.cos(math.radians(STEPPED_OVERHANG_MAX_SLOPE_DEG)))
        & (facing_down <= math.cos(math.radians(threshold_deg)) + _THRESHOLD_EPSILON)
        & (height >= BED_CONTACT_TOL_MM)
    )
    return FaceClasses(
        on_bed, needs_support, stair, height, [float(v) for v in high - low], stepped
    )


@dataclass
class SurfaceMetrics:
    bed_contact_area_mm2: float
    overhang_area_mm2: float
    support_volume_mm3: float
    stair_area_mm2: float
    size_mm: list[float]
    protected_area_mm2: float | None = None
    protected_overhang_area_mm2: float | None = None
    protected_bed_contact_area_mm2: float | None = None
    protected_stair_area_mm2: float | None = None
    stepped_overhang_area_mm2: float = 0.0


def measure(
    mesh: Mesh,
    rotation: np.ndarray,
    *,
    threshold_deg: float = DEFAULT_THRESHOLD_DEG,
    protected: np.ndarray | None = None,
) -> SurfaceMetrics:
    """Heuristic metrics of `mesh` rotated by `rotation`; `protected` is a face mask."""
    classes = classify_faces(mesh, rotation, threshold_deg=threshold_deg)
    areas = mesh.areas_mm2
    projected = areas * np.abs(mesh.transformed(rotation).normals[:, 2])
    support = classes.needs_support
    metrics = SurfaceMetrics(
        bed_contact_area_mm2=float(areas[classes.on_bed].sum()),
        overhang_area_mm2=float(areas[support].sum()),
        support_volume_mm3=float((projected[support] * classes.height_above_bed_mm[support]).sum()),
        stair_area_mm2=float(areas[classes.stair].sum()),
        size_mm=classes.size_mm,
        stepped_overhang_area_mm2=float(areas[classes.stepped_overhang].sum()),
    )
    if protected is not None:
        metrics.protected_area_mm2 = float(areas[protected].sum())
        metrics.protected_overhang_area_mm2 = float(areas[protected & support].sum())
        metrics.protected_bed_contact_area_mm2 = float(areas[protected & classes.on_bed].sum())
        metrics.protected_stair_area_mm2 = float(areas[protected & classes.stair].sum())
    return metrics


@dataclass
class OrientationCandidate:
    """One orientation and its heuristic metrics (all in mm, mm², mm³)."""

    label: str
    down_vector: list[float]  # model-frame direction that faces the bed
    rotation_matrix: list[list[float]]  # apply to model coordinates (column vectors)
    origin: str  # "as loaded", "axis", "flat face" or "protected region"
    height_mm: float
    size_mm: list[float]  # X, Y, Z extent after rotation
    bed_contact_area_mm2: float
    overhang_area_mm2: float
    support_volume_mm3: float  # heuristic, see HEURISTIC_LABEL
    stair_area_mm2: float
    protected_overhang_area_mm2: float | None = None
    protected_bed_contact_area_mm2: float | None = None
    protected_stair_area_mm2: float | None = None
    # Downward surface between the support threshold and STEPPED_OVERHANG_MAX_SLOPE_DEG
    # from horizontal: printed without supports, with visible layer steps. Not ranked.
    stepped_overhang_area_mm2: float = 0.0
    quality_cost: float = 0.0  # heuristic, lower is better, see QUALITY_WEIGHTS
    stable: bool = True  # bed contact >= STABLE_CONTACT_RATIO of the X/Y box area
    fits_bed: bool | None = None  # None when the bed size is unknown
    rank: int = 0


@dataclass
class OrientationAnalysis:
    model: str
    face_count: int
    object_count: int
    open_edges: int
    volume_mm3: float
    size_mm: list[float]  # as loaded
    threshold_deg: float
    goal: str
    ranking: str
    heuristic: str
    candidates: list[OrientationCandidate]  # best first
    regions: list[Region] = field(default_factory=list)
    protected_area_mm2: float | None = None
    notes: list[str] = field(default_factory=list)


RANKING_RULES = {
    "speed": "fits the bed, then stable (bed contact at least 5% of the X/Y box), then "
    "least support volume, then largest bed contact, then lowest",
    "min_support": "fits the bed, then stable (bed contact at least 5% of the X/Y box), then "
    "least support volume, then largest bed contact, then lowest",
    "quality": "fits the bed, then stable (bed contact at least 5% of the X/Y box), then "
    "lowest quality cost (heuristic, mm2: 10 x protected "
    "surface needing support + 10 x protected surface on the bed + 3 x protected stair area "
    "+ 1 x other surface needing support + 0.5 x other stair area; differences under 2% "
    "count as equal), then least support volume, then largest bed contact, then lowest",
}


def quality_cost(c: OrientationCandidate) -> float:
    """Weighted surface cost of an orientation (heuristic, mm²). Protected areas
    are counted with their own weight and left out of the general terms."""
    p_over = c.protected_overhang_area_mm2 or 0.0
    p_bed = c.protected_bed_contact_area_mm2 or 0.0
    p_stair = c.protected_stair_area_mm2 or 0.0
    w = QUALITY_WEIGHTS
    return (
        w["protected_overhang_area_mm2"] * p_over
        + w["protected_bed_contact_area_mm2"] * p_bed
        + w["protected_stair_area_mm2"] * p_stair
        + w["overhang_area_mm2"] * max(c.overhang_area_mm2 - p_over, 0.0)
        + w["stair_area_mm2"] * max(c.stair_area_mm2 - p_stair, 0.0)
    )


def _flat_face_directions(mesh: Mesh, limit: int) -> list[np.ndarray]:
    """Normals of the largest flat faces that can rest on the bed.

    Faces are grouped by normal; for each group only the faces lying on the
    outermost plane in that direction count, because only they touch the bed.
    """
    normals = mesh.normals
    areas = mesh.areas_mm2
    valid = areas > 0
    keys = np.round(normals[valid], 3)
    groups, inverse = np.unique(keys, axis=0, return_inverse=True)
    inverse = inverse.reshape(-1)
    points = mesh.triangles.reshape(-1, 3)
    centroids = mesh.triangles[valid].mean(axis=1)
    valid_areas = areas[valid]
    scored: list[tuple[float, np.ndarray]] = []
    for index, key in enumerate(groups):
        direction = key / np.linalg.norm(key)
        extreme = float((points @ direction).max())
        members = inverse == index
        touching = members & (centroids @ direction > extreme - BED_CONTACT_TOL_MM)
        area = float(valid_areas[touching].sum())
        if area > 0:
            scored.append((area, direction))
    scored.sort(key=lambda item: -item[0])
    return [direction for _, direction in scored[:limit]]


def _region_directions(facing: np.ndarray) -> list[tuple[str, np.ndarray]]:
    """Down vectors that turn the unit vector `facing` to each elevation in
    `REGION_ELEVATIONS_DEG` above horizontal, at `REGION_AZIMUTHS` azimuths.

    After `rotation_to_bed(d)` the facing direction p has a Z component of
    -dot(p, d), so d = -sin(e) p + cos(e) q with q perpendicular to p.
    """
    helper = np.array([0.0, 0.0, 1.0]) if abs(facing[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    q1 = np.cross(facing, helper)
    q1 /= np.linalg.norm(q1)
    q2 = np.cross(facing, q1)
    result = []
    for elevation in REGION_ELEVATIONS_DEG:
        e = math.radians(elevation)
        azimuths = 1 if elevation >= 90 else REGION_AZIMUTHS
        for k in range(azimuths):
            a = 2 * math.pi * k / azimuths
            q = math.cos(a) * q1 + math.sin(a) * q2
            down = -math.sin(e) * facing + math.cos(e) * q
            if elevation >= 90:
                label = "protected facing up"
            elif elevation == 0:
                label = f"protected facing sideways #{k + 1}"
            else:
                label = f"protected tilted {elevation:g} deg up #{k + 1}"
            result.append((label, down / np.linalg.norm(down)))
    return result


def candidate_directions(
    mesh: Mesh,
    *,
    flat_faces: int = DEFAULT_FLAT_FACE_CANDIDATES,
    regions: list[Region] | None = None,
) -> list[tuple[str, str, np.ndarray]]:
    """`(origin, label, down_vector)`: as loaded, the six axes, the largest flat
    faces and, for each protected region, the region turned to several
    elevations. Duplicates (within `SAME_DIRECTION_DEG`) keep the first."""
    raw: list[tuple[str, str, np.ndarray]] = [("as loaded", "as loaded", DOWN.copy())]
    raw += [("axis", _direction_label(np.array(a)), np.array(a)) for _, a in AXES]
    raw += [("flat face", _direction_label(d), d) for d in _flat_face_directions(mesh, flat_faces)]
    for region in regions or []:
        facing = region.mean_normal(mesh)
        if facing is None:
            continue
        for label, down in _region_directions(facing):
            name = label.replace("protected", region.label, 1)
            raw.append(("protected region", name, down))
    unique: list[tuple[str, str, np.ndarray]] = []
    limit = math.cos(math.radians(SAME_DIRECTION_DEG))
    for origin, label, direction in raw:
        if all(np.dot(direction, kept) < limit for _, _, kept in unique):
            unique.append((origin, label, direction))
    return unique


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def analyse_orientations(
    mesh: Mesh,
    *,
    threshold_deg: float = DEFAULT_THRESHOLD_DEG,
    bed_size_mm: tuple[float, float, float] | None = None,
    flat_faces: int = DEFAULT_FLAT_FACE_CANDIDATES,
    regions: list[Region] | None = None,
    goal: Goal = "speed",
) -> OrientationAnalysis:
    """Score candidate orientations, best first for `goal` (see `RANKING_RULES`).

    Support volumes within 0.5% of the model volume and quality costs within 2%
    of the best one count as equal.
    `bed_size_mm` is (X, Y, Z); X/Y may be swapped by rotating about Z.
    """
    if not 0 < threshold_deg < 90:
        raise ValueError(f"threshold_deg must be between 0 and 90, got {threshold_deg}")
    if goal not in GOALS:
        raise ValueError(f"Unknown goal {goal!r}; choose from {', '.join(GOALS)}.")
    for region in regions or []:
        region.validate()
    protected = regions_mask(mesh, regions)
    notes = list(mesh.notes)
    if protected is not None and not protected.any():
        raise ValueError(
            "The protected regions select no surface of the model; check direction, angle "
            "and box (model coordinates as loaded, in mm)."
        )
    volume = mesh.volume_mm3
    candidates = []
    for origin, label, direction in candidate_directions(
        mesh, flat_faces=flat_faces, regions=regions
    ):
        rotation = rotation_to_bed(direction)
        m = measure(mesh, rotation, threshold_deg=threshold_deg, protected=protected)
        fits = None
        if bed_size_mm:
            bx, by, bz = bed_size_mm
            footprint = sorted(m.size_mm[:2])
            fits = (
                footprint[0] <= min(bx, by) and footprint[1] <= max(bx, by) and m.size_mm[2] <= bz
            )
        candidates.append(
            OrientationCandidate(
                label=label,
                down_vector=[round(float(c), 6) + 0.0 for c in direction],
                rotation_matrix=[[round(float(c), 9) + 0.0 for c in row] for row in rotation],
                origin=origin,
                height_mm=round(m.size_mm[2], 3),
                size_mm=[round(s, 3) for s in m.size_mm],
                bed_contact_area_mm2=round(m.bed_contact_area_mm2, 2),
                overhang_area_mm2=round(m.overhang_area_mm2, 2),
                support_volume_mm3=round(m.support_volume_mm3, 1),
                stair_area_mm2=round(m.stair_area_mm2, 2),
                protected_overhang_area_mm2=_round(m.protected_overhang_area_mm2, 2),
                protected_bed_contact_area_mm2=_round(m.protected_bed_contact_area_mm2, 2),
                protected_stair_area_mm2=_round(m.protected_stair_area_mm2, 2),
                stepped_overhang_area_mm2=round(m.stepped_overhang_area_mm2, 2),
                fits_bed=fits,
            )
        )

    for c in candidates:
        c.quality_cost = round(quality_cost(c), 1)
        footprint = c.size_mm[0] * c.size_mm[1]
        c.stable = c.bed_contact_area_mm2 >= STABLE_CONTACT_RATIO * footprint
    protected_area = float(mesh.areas_mm2[protected].sum()) if protected is not None else None
    volume_tol = max(abs(volume) * 0.005, 1.0)
    best_cost = min((c.quality_cost for c in candidates if c.stable), default=0.0)
    cost_tol = max(best_cost * 0.02, 1.0)

    def key(c: OrientationCandidate) -> tuple:
        tail = (round(c.support_volume_mm3 / volume_tol), -c.bed_contact_area_mm2, c.height_mm)
        if goal != "quality":
            return (c.fits_bed is False, not c.stable, *tail)
        return (c.fits_bed is False, not c.stable, round(c.quality_cost / cost_tol), *tail)

    candidates.sort(key=key)
    for rank, candidate in enumerate(candidates, start=1):
        candidate.rank = rank

    open_edges = mesh.open_edge_count()
    if open_edges:
        notes.append(
            f"The mesh has {open_edges} open or non-manifold edges; volume and the "
            "heuristic may be off. The slicer may repair or reject it."
        )
    if not any(c.stable for c in candidates):
        notes.append(
            "No candidate rests on a flat area of at least 5% of its footprint; every "
            "orientation relies on supports (or a brim/raft) to stand."
        )
    if bed_size_mm and not any(c.fits_bed for c in candidates):
        notes.append("No candidate orientation fits the printable volume of the printer.")
    low, high = mesh.bounds_mm
    return OrientationAnalysis(
        model=str(mesh.source) if mesh.source else "",
        face_count=mesh.face_count,
        object_count=mesh.object_count,
        open_edges=open_edges,
        volume_mm3=round(volume, 1),
        size_mm=[round(float(v), 3) for v in high - low],
        threshold_deg=threshold_deg,
        goal=goal,
        ranking=RANKING_RULES[goal],
        heuristic=HEURISTIC_LABEL,
        candidates=candidates,
        regions=list(regions or []),
        protected_area_mm2=_round(protected_area, 2),
        notes=notes,
    )


def stepped_overhang_note(
    candidate: OrientationCandidate,
    *,
    threshold_deg: float,
    layer_height_mm: float | None,
) -> str | None:
    """Warn that downward slopes below the support threshold print without supports
    but with visible layer steps. Geometry only; the roughness is not measured."""
    area = candidate.stepped_overhang_area_mm2
    if area < STEPPED_OVERHANG_NOTE_MIN_MM2:
        return None
    steepest = math.tan(math.radians(threshold_deg))
    text = (
        f"Orientation {candidate.label!r}: {area:.0f} mm2 of downward surface slopes between "
        f"{threshold_deg:g} and {STEPPED_OVERHANG_MAX_SLOPE_DEG:g} degrees from horizontal. It "
        "prints without supports but with visible layer steps (geometry, not measured "
        "roughness): each layer overhangs the one below by layer height / tan(slope), "
    )
    if layer_height_mm:
        text += (
            f"up to {layer_height_mm / steepest:.2f} mm at {layer_height_mm:g} mm layers "
            f"(slope {threshold_deg:g} degrees). "
        )
    else:
        text += f"up to {1 / steepest:.2f} x the layer height (slope {threshold_deg:g} degrees). "
    text += (
        "Tell the user before printing: finer layers shrink the steps, at the cost of print time."
    )
    return text


def oriented_mesh(mesh: Mesh, candidate: OrientationCandidate) -> Mesh:
    """`mesh` rotated to the candidate and placed on the bed (lowest point at Z 0)."""
    return mesh.transformed(np.array(candidate.rotation_matrix)).placed_on_bed()
