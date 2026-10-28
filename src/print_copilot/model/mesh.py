"""Load triangle meshes from STL and 3MF files and write binary STL.

Only numpy and the standard library are used. A mesh is kept as a plain
triangle soup (`triangles[i]` holds the three vertices of face i, in mm);
orientation analysis needs nothing more.

3MF support covers the core specification plus the production extension's
`p:path` references to other model files inside the archive, which is how
OrcaSlicer-based apps store objects. Reading a project saved by the Snapmaker
Orca GUI is TO VERIFY IN SNAPMAKER ORCA.
"""

from __future__ import annotations

import os
import posixpath
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

import numpy as np

from print_copilot.errors import ModelError

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MODEL_REL_TYPE = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
DEFAULT_MODEL_PATH = "/3D/3dmodel.model"

# 3MF `unit` attribute to millimetres.
UNIT_TO_MM = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}

SUFFIXES = (".stl", ".3mf")


@dataclass
class Mesh:
    """Triangle soup in millimetres: `triangles` has shape (n, 3, 3)."""

    triangles: np.ndarray
    source: Path | None = None
    object_count: int = 1
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.triangles = np.asarray(self.triangles, dtype=np.float64).reshape(-1, 3, 3)

    @property
    def face_count(self) -> int:
        return len(self.triangles)

    def _cross(self) -> np.ndarray:
        t = self.triangles
        return np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])

    @property
    def areas_mm2(self) -> np.ndarray:
        return 0.5 * np.linalg.norm(self._cross(), axis=1)

    @property
    def normals(self) -> np.ndarray:
        """Unit face normals from the vertex winding (counter-clockwise = outward).
        Degenerate faces get a zero normal."""
        cross = self._cross()
        length = np.linalg.norm(cross, axis=1, keepdims=True)
        return np.divide(cross, length, out=np.zeros_like(cross), where=length > 0)

    @property
    def bounds_mm(self) -> np.ndarray:
        """`[[min_x, min_y, min_z], [max_x, max_y, max_z]]`."""
        points = self.triangles.reshape(-1, 3)
        return np.array([points.min(axis=0), points.max(axis=0)])

    @property
    def volume_mm3(self) -> float:
        """Signed volume (divergence theorem). Correct only for closed meshes."""
        t = self.triangles
        return float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)

    def open_edge_count(self) -> int:
        """Edges not shared by exactly two faces (0 for a closed, manifold mesh).
        Vertices are matched after rounding to 1 µm."""
        if not self.face_count:
            return 0
        points = np.round(self.triangles.reshape(-1, 3) * 1000).astype(np.int64)
        _, ids = np.unique(points, axis=0, return_inverse=True)
        ids = ids.reshape(-1, 3)
        edges = np.concatenate([ids[:, [0, 1]], ids[:, [1, 2]], ids[:, [2, 0]]])
        edges.sort(axis=1)
        _, counts = np.unique(edges, axis=0, return_counts=True)
        return int((counts != 2).sum())

    def transformed(self, rotation: np.ndarray) -> Mesh:
        """A copy rotated by the 3x3 matrix `rotation` (column-vector convention)."""
        rotated = self.triangles @ np.asarray(rotation, dtype=np.float64).T
        return Mesh(rotated, source=self.source, object_count=self.object_count)

    def placed_on_bed(self) -> Mesh:
        """A copy moved so it is centred on X/Y = 0 and its lowest point is at Z = 0."""
        low, high = self.bounds_mm
        offset = np.array([-(low[0] + high[0]) / 2, -(low[1] + high[1]) / 2, -low[2]])
        return Mesh(self.triangles + offset, source=self.source, object_count=self.object_count)


def _check(mesh: Mesh, path: Path) -> Mesh:
    if mesh.face_count == 0:
        raise ModelError(f"{path} contains no triangles.")
    if not np.isfinite(mesh.triangles).all():
        raise ModelError(f"{path} contains non-finite vertex coordinates.")
    if not (mesh.areas_mm2 > 0).any():
        raise ModelError(f"{path} contains only degenerate triangles.")
    return mesh


def load_stl(path: str | os.PathLike[str]) -> Mesh:
    """Read a binary or ASCII STL file. Units are taken as millimetres."""
    stl_path = Path(path)
    try:
        data = stl_path.read_bytes()
    except OSError as exc:
        raise ModelError(f"Cannot read {stl_path}: {exc}") from exc
    if len(data) >= 84:
        (count,) = struct.unpack_from("<I", data, 80)
        if len(data) == 84 + 50 * count:
            records = np.frombuffer(
                data,
                dtype=np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")]),
                count=count,
                offset=84,
            )
            return _check(Mesh(records["v"], source=stl_path), stl_path)
    return _check(Mesh(_parse_ascii_stl(data, stl_path), source=stl_path), stl_path)


def _parse_ascii_stl(data: bytes, path: Path) -> np.ndarray:
    text = data.decode("ascii", errors="replace")
    if not text.lstrip().lower().startswith("solid"):
        raise ModelError(f"{path} is neither a binary STL nor an ASCII STL.")
    vertices: list[list[float]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            try:
                vertices.append([float(v) for v in parts[1:]])
            except ValueError as exc:
                raise ModelError(f"{path}: bad vertex line {line.strip()!r}") from exc
    if len(vertices) % 3:
        raise ModelError(f"{path}: vertex count {len(vertices)} is not a multiple of 3.")
    return np.array(vertices, dtype=np.float64).reshape(-1, 3, 3)


def _parse_transform(text: str | None) -> np.ndarray:
    """3MF transform `m00 m01 m02 m10 ... m32` to a 4x4 matrix for row vectors."""
    matrix = np.eye(4)
    if text:
        values = [float(v) for v in text.split()]
        if len(values) != 12:
            raise ModelError(f"Invalid 3MF transform {text!r}: expected 12 numbers.")
        matrix[:, :3] = np.array(values).reshape(4, 3)
    return matrix


def _apply(transform: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    return triangles @ transform[:3, :3] + transform[3, :3]


class _ThreeMFReader:
    def __init__(self, archive: zipfile.ZipFile, path: Path) -> None:
        self.archive = archive
        self.path = path
        self.documents: dict[str, tuple[ElementTree.Element, float]] = {}

    def document(self, part: str) -> tuple[ElementTree.Element, float]:
        part = "/" + part.lstrip("/")
        if part not in self.documents:
            try:
                raw = self.archive.read(part.lstrip("/"))
            except KeyError as exc:
                raise ModelError(f"{self.path}: missing model part {part}") from exc
            try:
                root = ElementTree.fromstring(raw)
            except ElementTree.ParseError as exc:
                raise ModelError(f"{self.path}: invalid XML in {part}: {exc}") from exc
            unit = root.get("unit", "millimeter")
            if unit not in UNIT_TO_MM:
                raise ModelError(f"{self.path}: unknown 3MF unit {unit!r} in {part}.")
            self.documents[part] = (root, UNIT_TO_MM[unit])
        return self.documents[part]

    def object_triangles(self, part: str, object_id: str, depth: int = 0) -> np.ndarray:
        if depth > 16:
            raise ModelError(f"{self.path}: 3MF components nest too deeply.")
        root, scale = self.document(part)
        obj = root.find(f"{{{CORE_NS}}}resources/{{{CORE_NS}}}object[@id='{object_id}']")
        if obj is None:
            raise ModelError(f"{self.path}: object {object_id} not found in {part}.")
        mesh = obj.find(f"{{{CORE_NS}}}mesh")
        if mesh is not None:
            vertices = np.array(
                [
                    [float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))]
                    for v in mesh.iterfind(f"{{{CORE_NS}}}vertices/{{{CORE_NS}}}vertex")
                ],
                dtype=np.float64,
            ).reshape(-1, 3)
            faces = np.array(
                [
                    [int(t.get("v1", -1)), int(t.get("v2", -1)), int(t.get("v3", -1))]
                    for t in mesh.iterfind(f"{{{CORE_NS}}}triangles/{{{CORE_NS}}}triangle")
                ],
                dtype=np.int64,
            ).reshape(-1, 3)
            if faces.size and (faces.min() < 0 or faces.max() >= len(vertices)):
                raise ModelError(f"{self.path}: object {object_id} has bad vertex indices.")
            return vertices[faces] * scale
        parts = []
        for component in obj.iterfind(f"{{{CORE_NS}}}components/{{{CORE_NS}}}component"):
            target = component.get(f"{{{PRODUCTION_NS}}}path") or part
            child = self.object_triangles(target, component.get("objectid", ""), depth + 1)
            parts.append(_apply(_parse_transform(component.get("transform")), child))
        if not parts:
            raise ModelError(f"{self.path}: object {object_id} has neither mesh nor components.")
        return np.concatenate(parts)


def _root_model_part(archive: zipfile.ZipFile) -> str:
    try:
        rels = ElementTree.fromstring(archive.read("_rels/.rels"))
    except (KeyError, ElementTree.ParseError):
        return DEFAULT_MODEL_PATH
    for rel in rels.iterfind(f"{{{RELS_NS}}}Relationship"):
        if rel.get("Type") == MODEL_REL_TYPE and rel.get("Target"):
            return posixpath.normpath("/" + rel.get("Target", "").lstrip("/"))
    return DEFAULT_MODEL_PATH


def load_3mf(path: str | os.PathLike[str]) -> Mesh:
    """Read every build item of a 3MF file into one mesh, in plate coordinates."""
    mf_path = Path(path)
    try:
        archive = zipfile.ZipFile(mf_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ModelError(f"Cannot open {mf_path} as a 3MF (zip) file: {exc}") from exc
    with archive:
        reader = _ThreeMFReader(archive, mf_path)
        root_part = _root_model_part(archive)
        root, _ = reader.document(root_part)
        items = list(root.iterfind(f"{{{CORE_NS}}}build/{{{CORE_NS}}}item"))
        if not items:
            raise ModelError(f"{mf_path}: the 3MF build section lists no objects.")
        parts = []
        for item in items:
            target = item.get(f"{{{PRODUCTION_NS}}}path") or root_part
            triangles = reader.object_triangles(target, item.get("objectid", ""))
            parts.append(_apply(_parse_transform(item.get("transform")), triangles))
    mesh = Mesh(np.concatenate(parts), source=mf_path, object_count=len(items))
    if len(items) > 1:
        mesh.notes.append(
            f"{len(items)} objects in the 3MF build were combined and treated as one rigid body."
        )
    return _check(mesh, mf_path)


def load_mesh(path: str | os.PathLike[str]) -> Mesh:
    """Load an STL or 3MF model by file extension."""
    model_path = Path(path)
    if not model_path.is_file():
        raise ModelError(f"Model file not found: {model_path}")
    suffix = model_path.suffix.lower()
    if suffix == ".stl":
        return load_stl(model_path)
    if suffix == ".3mf":
        return load_3mf(model_path)
    raise ModelError(
        f"Unsupported model format {suffix!r} for {model_path}; use one of {', '.join(SUFFIXES)}."
    )


def write_stl(mesh: Mesh, path: str | os.PathLike[str]) -> Path:
    """Write `mesh` as a binary STL and return the path."""
    stl_path = Path(path)
    records = np.zeros(
        mesh.face_count,
        dtype=np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")]),
    )
    records["normal"] = mesh.normals
    records["v"] = mesh.triangles
    header = b"print-copilot binary STL".ljust(80, b" ")
    with stl_path.open("wb") as handle:
        handle.write(header)
        handle.write(struct.pack("<I", mesh.face_count))
        handle.write(records.tobytes())
    return stl_path
