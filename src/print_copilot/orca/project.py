"""Read 3MF projects saved by OrcaSlicer-based apps and write copies the
Snapmaker Orca command line can slice.

Project layout (read from projects saved by Bambu Studio 1.4 to 2.6 and
Snapmaker Orca, downloaded for the integration tests; loader rules read from
`bbs_3mf.cpp` in Snapmaker Orca v2.4.0):

    3D/3dmodel.model                 build items -> objects, usually with
                                     components pointing to 3D/Objects/*.model
    Metadata/model_settings.config   per object: name, `extruder` (1-based
                                     filament) and other settings; per part
                                     (`<part id>` = component object id):
                                     subtype and `extruder` (0 = the object's);
                                     `<plate>` sections listing object ids
    Metadata/project_settings.config JSON: printer, filament colours and types

`<object id>` in model_settings.config is the id of the object a build item
references; the loader ignores metadata whose id matches no build item.
Painted colours are a `paint_color` attribute on triangles (see `paint.py`).

Snapmaker Orca CLI on 01.10.01.50 (all verified, docs/verified-facts.md section 2):

- any 3MF whose `Application` metadata starts with `BambuStudio-` or
  `Snapmaker_Orca-` crashes (exit 139) or is refused by a version check;
- a 3MF with a `<plate>` section in model_settings.config crashes: in
  `PartPlate::get_extruders_under_cli`, `expand_plate_extruders` dereferences
  the GUI app, which the CLI does not have (guarded in the v2.4.1 source);
- without those two, the same file slices: object and part extruders and
  painted colours are honoured, and `--arrange 0` keeps the build positions;
- `Metadata/project_settings.config` makes the CLI merge the project's own
  presets ("Param values in 3mf/config error" with a Bambu project), so it is
  left out and the presets come from the command line.

Installed Windows 2.4.1 recheck (docs/verified-facts.md section 4): the plate-only crash
is fixed, but an Application-only project still crashes. A complete generated
project loads with the prime tower silently disabled, so the copy workaround
is retained to preserve the settings sent by the caller.

`write_slicing_copy` therefore writes a copy without the `Application`
metadata, without plates and without any other `Metadata/` file, keeping only
the objects of one plate, moved to the centre of the bed.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

import numpy as np

from print_copilot.errors import ModelError
from print_copilot.model.mesh import CORE_NS, PRODUCTION_NS, load_3mf_items
from print_copilot.orca.paint import paint_states, remap_paint

MODEL_CONFIG = "Metadata/model_settings.config"
PROJECT_CONFIG = "Metadata/project_settings.config"
ROOT_MODEL = "3D/3dmodel.model"
PRUSA_MODEL_CONFIG = "Metadata/Slic3r_PE_model.config"

# Object and part settings that hold a 1-based filament number. `extruder`
# 0 on a part means "the object's filament"; the others are only remapped
# when a project sets them.
FILAMENT_KEYS: tuple[str, ...] = (
    "extruder",
    "wall_filament",
    "sparse_infill_filament",
    "solid_infill_filament",
    "support_filament",
    "support_interface_filament",
)
# Object and part metadata that describes the mesh, not a print setting.
# Every other key is a setting that overrides the process preset for that
# object or part (e.g. sparse_infill_density, enable_support).
NON_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "matrix",
        "source_file",
        "source_object_id",
        "source_volume_id",
        "source_offset_x",
        "source_offset_y",
        "source_offset_z",
        *FILAMENT_KEYS,
    }
)
# Metadata files whose content is lost in the slicing copy; a note says so.
DROPPED_FEATURES: dict[str, str] = {
    "Metadata/custom_gcode_per_print_z.xml": "per-layer colour changes and custom G-code",
    "Metadata/layer_config_ranges.xml": "per-height setting ranges",
    "Metadata/layer_heights_profile.txt": "variable layer height",
}

_APPLICATION = re.compile(rb'\s*<metadata name="Application">[^<]*</metadata>')
_ITEM = re.compile(rb"<item\b[^>]*?/>")
_PAINT = re.compile(rb'paint_color="([0-9A-Fa-f]*)"')
_OBJECT_START = re.compile(rb'<object\s[^>]*?\bid="([^"]+)"')


@dataclass
class ProjectFilament:
    """A filament slot of the project (`number` is 1-based, as in the file)."""

    number: int
    colour: str | None
    type: str | None
    preset: str | None


@dataclass
class ProjectPart:
    id: str
    name: str | None
    subtype: str  # normal_part, modifier_part, support_enforcer, ...
    extruder: int  # 0 = the object's filament
    painted: list[int]  # painted filament numbers, 1-based
    settings: dict[str, int] = field(default_factory=dict)  # other FILAMENT_KEYS
    source: str = ROOT_MODEL  # archive file holding the mesh
    overrides: dict[str, str] = field(default_factory=dict)  # process settings of this part


@dataclass
class ProjectObject:
    id: str
    name: str | None
    extruder: int  # 1-based; 1 when the project does not set it
    parts: list[ProjectPart]
    plate: int | None
    settings: dict[str, int] = field(default_factory=dict)  # other FILAMENT_KEYS
    overrides: dict[str, str] = field(default_factory=dict)  # process settings of this object

    def setting_overrides(self) -> dict[str, dict[str, str]]:
        """Process settings the project sets on this object or its parts,
        keyed by "object" or the part's name; empty when it sets none."""
        found = {"object": dict(self.overrides)} if self.overrides else {}
        for part in self.parts:
            if part.overrides:
                found[f"{part.subtype} {part.name or part.id}"] = dict(part.overrides)
        return found

    def filaments(self) -> list[int]:
        """Filament numbers this object prints with (1-based)."""
        used = {self.extruder}
        used.update(v for v in self.settings.values() if v > 0)
        for part in self.parts:
            if part.subtype == "modifier_part":
                # A modifier with its own filament prints its region with it.
                if part.extruder:
                    used.add(part.extruder)
                used.update(v for v in part.settings.values() if v > 0)
            if part.subtype != "normal_part":
                continue
            used.add(part.extruder or self.extruder)
            used.update(part.painted)
            used.update(v for v in part.settings.values() if v > 0)
        return sorted(used)


@dataclass
class ProjectPlate:
    number: int  # 1-based
    name: str | None
    object_ids: list[str]


@dataclass
class ProjectInfo:
    path: Path
    application: str | None
    printer: str | None
    filaments: list[ProjectFilament]
    objects: list[ProjectObject]
    plates: list[ProjectPlate]
    notes: list[str] = field(default_factory=list)

    def plate(self, number: int) -> ProjectPlate:
        for plate in self.plates:
            if plate.number == number:
                return plate
        raise ModelError(
            f"{self.path.name} has no plate {number}; plates: {[p.number for p in self.plates]}."
        )

    def plate_objects(self, number: int) -> list[ProjectObject]:
        ids = set(self.plate(number).object_ids)
        return [o for o in self.objects if o.id in ids]

    def plate_filaments(self, number: int) -> list[int]:
        used: set[int] = set()
        for obj in self.plate_objects(number):
            used.update(obj.filaments())
        return sorted(used)

    def filament(self, number: int) -> ProjectFilament | None:
        return next((f for f in self.filaments if f.number == number), None)


def _int(value: str | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _metadata(element: ElementTree.Element) -> dict[str, str]:
    return {
        m.get("key", ""): m.get("value", "")
        for m in element.findall("metadata")
        if m.get("key") is not None
    }


def _filament_settings(meta: Mapping[str, str]) -> dict[str, int]:
    return {k: _int(meta[k]) for k in FILAMENT_KEYS[1:] if k in meta}


def _setting_overrides(meta: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in meta.items() if k and k not in NON_SETTING_KEYS}


class _Archive:
    """Lazy access to the XML documents of a 3MF archive."""

    def __init__(self, archive: zipfile.ZipFile, path: Path) -> None:
        self.archive = archive
        self.path = path
        self.docs: dict[str, ElementTree.Element] = {}
        self.paint: dict[str, dict[str, set[int]]] = {}

    def read(self, name: str) -> bytes:
        try:
            return self.archive.read(name.lstrip("/"))
        except KeyError as exc:
            raise ModelError(f"{self.path}: missing part {name}") from exc

    def doc(self, name: str) -> ElementTree.Element:
        name = name.lstrip("/")
        if name not in self.docs:
            try:
                self.docs[name] = ElementTree.fromstring(self.read(name))
            except ElementTree.ParseError as exc:
                raise ModelError(f"{self.path}: invalid XML in {name}: {exc}") from exc
        return self.docs[name]

    def object(self, part: str, object_id: str) -> ElementTree.Element:
        obj = self.doc(part).find(f"{{{CORE_NS}}}resources/{{{CORE_NS}}}object[@id='{object_id}']")
        if obj is None:
            raise ModelError(f"{self.path}: object {object_id} not found in {part}.")
        return obj

    def painted(self, part: str, object_id: str) -> set[int]:
        """Painted filament numbers of a mesh object, read with a regex so a
        file with millions of triangles is not walked twice."""
        part = part.lstrip("/")
        if part not in self.paint:
            data = self.read(part)
            starts = [(m.start(), m.group(1).decode()) for m in _OBJECT_START.finditer(data)]
            found: dict[str, set[int]] = {}
            cache: dict[bytes, set[int]] = {}
            for index, (start, oid) in enumerate(starts):
                end = starts[index + 1][0] if index + 1 < len(starts) else len(data)
                states: set[int] = set()
                for match in _PAINT.finditer(data, start, end):
                    value = match.group(1)
                    if value not in cache:
                        cache[value] = paint_states(value.decode())
                    states |= cache[value]
                found[oid] = states
            self.paint[part] = found
        return {s for s in self.paint[part].get(object_id, set()) if s > 0}


def _components(arc: _Archive, part: str, object_id: str) -> list[tuple[str, str]]:
    """`(model part, object id)` of the mesh objects that make up an object."""
    obj = arc.object(part, object_id)
    if obj.find(f"{{{CORE_NS}}}mesh") is not None:
        return [(part, object_id)]
    result = []
    for comp in obj.iterfind(f"{{{CORE_NS}}}components/{{{CORE_NS}}}component"):
        target = comp.get(f"{{{PRODUCTION_NS}}}path") or part
        child = comp.get("objectid", "")
        if arc.object(target, child).find(f"{{{CORE_NS}}}mesh") is None:
            raise ModelError(
                f"{arc.path}: object {object_id} nests components more than one level deep, "
                "which project files saved by the slicer do not do."
            )
        result.append((target.lstrip("/"), child))
    if not result:
        raise ModelError(f"{arc.path}: object {object_id} has neither mesh nor components.")
    return result


def _project_filaments(arc: _Archive, notes: list[str]) -> tuple[str | None, list[ProjectFilament]]:
    try:
        raw = arc.archive.read(PROJECT_CONFIG)
    except KeyError:
        return None, []
    try:
        config = json.loads(raw)
    except ValueError:
        notes.append(f"{PROJECT_CONFIG} is not valid JSON; filament colours unknown.")
        return None, []
    colours = config.get("filament_colour") or []
    types = config.get("filament_type") or []
    presets = config.get("filament_settings_id") or []
    count = max(len(colours), len(types), len(presets))

    def at(values: list, index: int) -> str | None:
        return values[index] if index < len(values) and values[index] else None

    printer = config.get("printer_settings_id")
    return (
        printer if isinstance(printer, str) else None,
        [
            ProjectFilament(i + 1, at(colours, i), at(types, i), at(presets, i))
            for i in range(count)
        ],
    )


def read_project_settings(path: str | os.PathLike[str]) -> dict[str, object]:
    """The settings the project was saved with (`project_settings.config`);
    empty when the 3MF has none or they are not valid JSON."""
    try:
        with zipfile.ZipFile(path) as archive:
            config = json.loads(archive.read(PROJECT_CONFIG))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return {}
    return config if isinstance(config, dict) else {}


def read_project(path: str | os.PathLike[str]) -> ProjectInfo:
    """Read objects, parts, filament assignments, painting and plates of a 3MF."""
    mf_path = Path(path)
    if not mf_path.is_file():
        raise ModelError(f"Project file not found: {mf_path}")
    try:
        archive = zipfile.ZipFile(mf_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ModelError(f"Cannot open {mf_path} as a 3MF (zip) file: {exc}") from exc
    notes: list[str] = []
    with archive:
        arc = _Archive(archive, mf_path)
        names = set(archive.namelist())
        root = arc.doc(ROOT_MODEL)
        application = next(
            (
                m.text
                for m in root.findall(f"{{{CORE_NS}}}metadata")
                if m.get("name") == "Application"
            ),
            None,
        )
        printer, filaments = _project_filaments(arc, notes)

        config_objects: dict[str, ElementTree.Element] = {}
        plates: list[ProjectPlate] = []
        if MODEL_CONFIG in names:
            try:
                config = ElementTree.fromstring(archive.read(MODEL_CONFIG))
            except ElementTree.ParseError as exc:
                raise ModelError(f"{mf_path}: invalid XML in {MODEL_CONFIG}: {exc}") from exc
            config_objects = {o.get("id", ""): o for o in config.findall("object")}
            for plate in config.findall("plate"):
                meta = _metadata(plate)
                ids = [
                    _metadata(inst).get("object_id", "") for inst in plate.findall("model_instance")
                ]
                plates.append(
                    ProjectPlate(
                        number=_int(meta.get("plater_id"), len(plates) + 1),
                        name=meta.get("plater_name") or None,
                        object_ids=[i for i in ids if i],
                    )
                )
        elif PRUSA_MODEL_CONFIG in names:
            notes.append(
                "PrusaSlicer project: per-object filaments and painting in "
                f"{PRUSA_MODEL_CONFIG} are not read; every object uses filament 1."
            )
        for name, feature in DROPPED_FEATURES.items():
            if name in names:
                notes.append(f"The project has {feature} ({name}); slicing copies leave it out.")

        objects: list[ProjectObject] = []
        build = root.findall(f"{{{CORE_NS}}}build/{{{CORE_NS}}}item")
        if not build:
            raise ModelError(f"{mf_path}: the 3MF build section lists no objects.")
        for item in build:
            object_id = item.get("objectid", "")
            part = (item.get(f"{{{PRODUCTION_NS}}}path") or ROOT_MODEL).lstrip("/")
            element = config_objects.get(object_id)
            meta = _metadata(element) if element is not None else {}
            part_meta = (
                {p.get("id", ""): p for p in element.findall("part")} if element is not None else {}
            )
            parts = []
            for comp_part, comp_id in _components(arc, part, object_id):
                pe = part_meta.get(comp_id)
                pm = _metadata(pe) if pe is not None else {}
                parts.append(
                    ProjectPart(
                        id=comp_id,
                        name=pm.get("name") or None,
                        subtype=(pe.get("subtype") if pe is not None else None) or "normal_part",
                        extruder=_int(pm.get("extruder")),
                        painted=sorted(arc.painted(comp_part, comp_id)),
                        settings=_filament_settings(pm),
                        source=comp_part,
                        overrides=_setting_overrides(pm),
                    )
                )
            plate = next((p.number for p in plates if object_id in p.object_ids), None)
            objects.append(
                ProjectObject(
                    id=object_id,
                    name=meta.get("name") or None,
                    extruder=max(_int(meta.get("extruder"), 1), 1),
                    parts=parts,
                    plate=plate,
                    settings=_filament_settings(meta),
                    overrides=_setting_overrides(meta),
                )
            )

    known = {o.id for o in objects}
    stray = sorted(set(config_objects) - known, key=lambda s: _int(s))
    if stray:
        notes.append(
            f"{MODEL_CONFIG} has settings for object id(s) {stray[:10]} that no build item "
            "uses; Snapmaker Orca ignores them, so those objects keep filament 1 unless "
            "painted."
        )
    if not plates:
        plates = [ProjectPlate(1, None, [o.id for o in objects])]
        for obj in objects:
            obj.plate = 1
    unplaced = [o.id for o in objects if o.plate is None]
    if unplaced:
        notes.append(f"Objects {unplaced} are on no plate and are never sliced.")
    for plate in plates:
        plate.object_ids = [i for i in plate.object_ids if i in known]
    return ProjectInfo(
        path=mf_path,
        application=application,
        printer=printer,
        filaments=filaments,
        objects=objects,
        plates=plates,
        notes=notes,
    )


def is_project(path: str | os.PathLike[str]) -> bool:
    """True if a 3MF carries slicer metadata that the CLI cannot read directly."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if names & {MODEL_CONFIG, PROJECT_CONFIG}:
                return True
            head = archive.read(ROOT_MODEL)[:20000] if ROOT_MODEL in names else b""
    except (OSError, zipfile.BadZipFile) as exc:
        raise ModelError(f"Cannot open {path} as a 3MF (zip) file: {exc}") from exc
    return _APPLICATION.search(head) is not None


def archive_model_files(path: str | os.PathLike[str]) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return [n for n in archive.namelist() if n.endswith(".model")]


def _rel_target(relationship: bytes) -> str:
    found = re.search(rb'Target="([^"]+)"', relationship)
    return found.group(1).decode().lstrip("/") if found else ""


def _repaint_objects(data: bytes, object_ids: set[str], repaint) -> bytes:
    """Apply `repaint` to the `paint_color` values of the given mesh objects only."""
    starts = [(m.start(), m.group(1).decode()) for m in _OBJECT_START.finditer(data)]
    pieces = []
    previous = 0
    for index, (start, oid) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(data)
        if oid in object_ids:
            pieces.append(data[previous:start])
            pieces.append(_PAINT.sub(repaint, data[start:end]))
            previous = end
    pieces.append(data[previous:])
    return b"".join(pieces)


@dataclass
class SlicingCopy:
    path: Path
    plate: int
    object_ids: list[str]
    offset_mm: list[float]  # X/Y/Z added to every build item of the plate
    bounds_mm: list[list[float]]  # plate objects after the move
    notes: list[str] = field(default_factory=list)


def _translate(item: bytes, offset: np.ndarray) -> bytes:
    match = re.search(rb'transform="([^"]*)"', item)
    values = [float(v) for v in match.group(1).split()] if match else []
    if match and len(values) != 12:
        raise ModelError(f"Invalid 3MF transform {match.group(1)!r}.")
    if not values:
        values = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
    values[9:12] = [values[9] + offset[0], values[10] + offset[1], values[11] + offset[2]]
    text = " ".join(f"{v:.9g}" for v in values).encode()
    if match:
        return item[: match.start(1)] + text + item[match.end(1) :]
    return item.replace(b"<item", b'<item transform="' + text + b'"', 1)


def _rewrite_config(
    raw: bytes, keep: set[str], mapping: Mapping[int, int], defaults: Mapping[str, int]
) -> bytes:
    config = ElementTree.fromstring(raw)
    for child in list(config):
        drop_object = child.tag == "object" and child.get("id") not in keep
        if child.tag in ("plate", "assemble") or drop_object:
            config.remove(child)

    def remap(element: ElementTree.Element, *, uses_filament: bool) -> None:
        for meta in element.findall("metadata"):
            key = meta.get("key")
            value = _int(meta.get("value"))
            if key not in FILAMENT_KEYS or value <= 0:
                continue
            if value in mapping:
                meta.set("value", str(mapping[value]))
            elif uses_filament:
                raise ModelError(f"Filament {value} ({key}) has no tool head in the mapping.")
            else:
                # Support enforcers and blockers print nothing themselves.
                meta.set("value", "0")

    for obj in config.findall("object"):
        remap(obj, uses_filament=True)
        if not any(m.get("key") == "extruder" for m in obj.findall("metadata")):
            ElementTree.SubElement(
                obj, "metadata", {"key": "extruder", "value": str(defaults[obj.get("id", "")])}
            )
        for part in obj.findall("part"):
            subtype = part.get("subtype") or "normal_part"
            remap(part, uses_filament=subtype in ("normal_part", "modifier_part"))
    present = {o.get("id") for o in config.findall("object")}
    for object_id, extruder in defaults.items():
        if object_id not in present:
            obj = ElementTree.SubElement(config, "object", {"id": object_id})
            ElementTree.SubElement(obj, "metadata", {"key": "extruder", "value": str(extruder)})
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(config)


def write_slicing_copy(
    project: ProjectInfo,
    plate: int,
    mapping: Mapping[int, int],
    dest: str | os.PathLike[str],
    *,
    bed_mm: tuple[float, float, float] | None,
    bed_origin_mm: tuple[float, float] = (0.0, 0.0),
) -> SlicingCopy:
    """Write a copy of `project` the CLI can slice: only `plate`'s objects,
    filament numbers changed by `mapping` (project filament -> tool head, both
    1-based), moved so the plate's objects are centred on the bed.

    Fails if a used filament has no target or the objects do not fit `bed_mm`.
    """
    objects = project.plate_objects(plate)
    if not objects:
        raise ModelError(f"Plate {plate} of {project.path.name} has no objects.")
    missing = [n for n in project.plate_filaments(plate) if n not in mapping]
    if missing:
        raise ModelError(f"Filament(s) {missing} used on plate {plate} have no tool head.")
    keep = {o.id for o in objects}
    notes: list[str] = []

    items = [(oid, tris) for oid, tris in load_3mf_items(project.path) if oid in keep]
    points = np.concatenate([tris.reshape(-1, 3) for _, tris in items])
    low, high = points.min(axis=0), points.max(axis=0)
    offset = np.zeros(3)
    if bed_mm is not None:
        size = high - low
        if size[0] > bed_mm[0] or size[1] > bed_mm[1] or size[2] > bed_mm[2]:
            raise ModelError(
                f"Plate {plate} of {project.path.name} measures {size[0]:.1f} x {size[1]:.1f} x "
                f"{size[2]:.1f} mm, larger than the printable volume {bed_mm[0]:g} x "
                f"{bed_mm[1]:g} x {bed_mm[2]:g} mm."
            )
        centre = np.array([bed_origin_mm[0] + bed_mm[0] / 2, bed_origin_mm[1] + bed_mm[1] / 2])
        offset[:2] = centre - (low[:2] + high[:2]) / 2
    offset[2] = -low[2]
    notes.append(
        f"Plate {plate} objects moved by X {offset[0]:+.2f}, Y {offset[1]:+.2f}, "
        f"Z {offset[2]:+.2f} mm so they sit centred on the bed; their arrangement is kept."
    )

    identity = all(k == v for k, v in mapping.items())
    paint_cache: dict[bytes, bytes] = {}

    def repaint(match: re.Match[bytes]) -> bytes:
        value = match.group(1)
        if value not in paint_cache:
            paint_cache[value] = remap_paint(value.decode(), mapping).encode()
        return b'paint_color="' + paint_cache[value] + b'"'

    # Mesh objects of the kept objects, per archive file. Painting of other
    # objects may use filaments the mapping does not cover, so it is left
    # alone, and files that only hold other objects are not copied.
    meshes: dict[str, set[str]] = {}
    for obj in objects:
        for part in obj.parts:
            meshes.setdefault(part.source.lstrip("/"), set()).add(part.id)
    other_objects = {o.id for o in project.objects} - keep
    unused = {
        name
        for name in archive_model_files(project.path)
        if name != ROOT_MODEL and name not in meshes
    }

    dest_path = Path(dest)
    with (
        zipfile.ZipFile(project.path) as zin,
        zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zout,
    ):
        names = zin.namelist()
        defaults = {o.id: mapping[o.extruder] for o in objects}
        for name in names:
            if name.startswith("Metadata/") or name.startswith("Auxiliaries/") or name in unused:
                continue
            data = zin.read(name)
            if name == ROOT_MODEL:
                data = _APPLICATION.sub(b"", data)

                def item(match: re.Match[bytes]) -> bytes:
                    found = re.search(rb'objectid="([^"]+)"', match.group(0))
                    if not found or found.group(1).decode() not in keep:
                        return b""
                    return _translate(match.group(0), offset)

                data = _ITEM.sub(item, data)
                # Objects of other plates: the loader would follow their
                # components into files that are not copied.
                data = re.sub(
                    rb"<object\s[^>]*?\bid=\"([^\"]+)\".*?</object>\s*",
                    lambda m: b"" if m.group(1).decode() in other_objects else m.group(0),
                    data,
                    flags=re.S,
                )
            elif name.endswith(".rels") and unused:
                data = re.sub(
                    rb"\s*<Relationship\b[^>]*?/>",
                    lambda m: b"" if _rel_target(m.group(0)) in unused else m.group(0),
                    data,
                )
            if name in meshes and not identity:
                data = _repaint_objects(data, meshes[name], repaint)
            zout.writestr(name, data)
        config = zin.read(MODEL_CONFIG) if MODEL_CONFIG in names else b"<config/>"
        zout.writestr(MODEL_CONFIG, _rewrite_config(config, keep, mapping, defaults))

    moved = [(low + offset).round(3).tolist(), (high + offset).round(3).tolist()]
    return SlicingCopy(
        path=dest_path,
        plate=plate,
        object_ids=[o.id for o in objects],
        offset_mm=offset.round(3).tolist(),
        bounds_mm=moved,
        notes=notes,
    )


# Keys of a flattened preset that describe the preset itself, not a setting.
_PRESET_IDENTITY_KEYS = frozenset(
    {
        "name",
        "inherits",
        "from",
        "instantiation",
        "setting_id",
        "type",
        "filament_id",
        "compatible_printers",
        "compatible_printers_condition",
        "compatible_prints",
        "compatible_prints_condition",
    }
)


def project_settings(
    machine: Mapping[str, object],
    process: Mapping[str, object],
    filaments: list[Mapping[str, object]],
    *,
    machine_name: str,
    process_name: str,
    filament_names: list[str],
    colours: list[str],
    bases: list[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """The `project_settings.config` JSON for the given flattened presets, in
    the form OrcaSlicer projects use: one flat dict, per-filament keys as one
    list entry per head.

    `bases` are the named presets before any override, in `inherits_group`
    order (process, filaments, machine). When the app opens the project it
    resets every key not listed in `different_settings_to_system` to the
    named preset's value (`PresetCollection::load_external_preset`, Snapmaker
    Orca v2.4.1), so the keys that differ from `bases` are listed there.
    """
    config: dict[str, object] = {}
    for source in (process, machine):
        config.update({k: v for k, v in source.items() if k not in _PRESET_IDENTITY_KEYS})
    # `version` is one string for the project: as a per-filament list the app's
    # JSON loader stops at it and drops every key sorted after it, such as
    # `wall_loops` (log: "type must be string, but is array").
    keys = sorted({k for f in filaments for k in f} - _PRESET_IDENTITY_KEYS - {"version"})
    for key in keys:
        values: list[object] = []
        for preset in filaments:
            value = preset.get(key, "")
            values.extend(value if isinstance(value, list) else [value])
        config[key] = values
    config["filament_colour"] = list(colours)
    config["printer_settings_id"] = machine_name
    config["print_settings_id"] = process_name
    config["filament_settings_id"] = list(filament_names)
    config["inherits_group"] = [process_name, *filament_names, machine_name]
    if bases is not None:
        written = [process, *filaments, machine]
        if len(bases) != len(written):
            raise ModelError(f"Expected {len(written)} base presets, got {len(bases)}.")
        config["different_settings_to_system"] = [
            _different_keys(preset, base) for preset, base in zip(written, bases, strict=True)
        ]
    config["from"] = "project"
    config["name"] = "project_settings"
    return config


def _different_keys(preset: Mapping[str, object], base: Mapping[str, object]) -> str:
    """The keys of `preset` whose value differs from `base`, joined by `;`
    as the app writes `different_settings_to_system` entries."""
    keys = (k for k in preset if k not in _PRESET_IDENTITY_KEYS and preset[k] != base.get(k))
    return ";".join(sorted(keys))


def _plate_xml(object_ids: list[str]) -> bytes:
    plate = ElementTree.Element("plate")
    for key, value in (("plater_id", "1"), ("plater_name", ""), ("locked", "false")):
        ElementTree.SubElement(plate, "metadata", {"key": key, "value": value})
    for object_id in object_ids:
        instance = ElementTree.SubElement(plate, "model_instance")
        ElementTree.SubElement(instance, "metadata", {"key": "object_id", "value": object_id})
        ElementTree.SubElement(instance, "metadata", {"key": "instance_id", "value": "0"})
    return ElementTree.tostring(plate)


def write_app_project(
    slicing_copy: str | os.PathLike[str],
    dest: str | os.PathLike[str],
    *,
    application: str,
    object_ids: list[str],
    settings: Mapping[str, object],
) -> Path:
    """Turn a slicing copy into a project for the app: `Application`
    metadata, one plate with every object, and `settings` as
    `project_settings.config`. Refuses to overwrite an existing file."""
    dest_path = Path(dest)
    if dest_path.exists():
        raise ModelError(f"{dest_path} already exists; choose a new file name.")
    tag = f'<metadata name="Application">{application}</metadata>'.encode()
    with (
        zipfile.ZipFile(slicing_copy) as zin,
        zipfile.ZipFile(dest_path, "x", zipfile.ZIP_DEFLATED) as zout,
    ):
        for name in zin.namelist():
            data = zin.read(name)
            if name == ROOT_MODEL:
                head = re.search(rb"<model\b[^>]*>", data)
                if head is None:
                    raise ModelError(f"{slicing_copy}: {ROOT_MODEL} has no <model> element.")
                data = data[: head.end()] + b"\n " + tag + data[head.end() :]
            elif name == MODEL_CONFIG:
                data = data.replace(b"</config>", _plate_xml(object_ids) + b"\n</config>")
            zout.writestr(name, data)
        zout.writestr(PROJECT_CONFIG, json.dumps(settings, indent=4, ensure_ascii=False))
    return dest_path


_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships '
    'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/{target}" Id="rel-1" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>\n'
)
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<Types '
    'xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" '
    'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>\n'
)
PARTS_MODEL = "3D/Objects/object_1.model"


def write_model_project(
    triangles: np.ndarray, dest: str | os.PathLike[str], *, name: str, filament_type: str | None
) -> Path:
    """Wrap a plain mesh (an STL, or a 3MF without slicer metadata) into a
    one-object, one-filament project in the layout `read_project` reads, so
    the project workflow can put it on a chosen tool head.

    `triangles` is an (n, 3, 3) array in mm. The filament has no colour: a
    plain mesh does not say which one it should print with.
    """
    points, inverse = np.unique(triangles.reshape(-1, 3), axis=0, return_inverse=True)
    faces = inverse.reshape(-1, 3)
    vertices = "".join(f'<vertex x="{x:.6g}" y="{y:.6g}" z="{z:.6g}"/>' for x, y, z in points)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    header = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{CORE_NS}" '
        f'xmlns:p="{PRODUCTION_NS}" requiredextensions="p">'
    )
    parts = (
        f'{header}<resources><object id="1" type="model"><mesh><vertices>{vertices}'
        f"</vertices><triangles>{tris}</triangles></mesh></object></resources></model>\n"
    )
    root = (
        f'{header}<resources><object id="2" type="model"><components>'
        f'<component p:path="/{PARTS_MODEL}" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        "</components></object></resources><build>"
        '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build></model>\n'
    )
    config = ElementTree.Element("config")
    obj = ElementTree.SubElement(config, "object", {"id": "2"})
    for key, value in (("name", name), ("extruder", "1")):
        ElementTree.SubElement(obj, "metadata", {"key": key, "value": value})
    part = ElementTree.SubElement(obj, "part", {"id": "1", "subtype": "normal_part"})
    ElementTree.SubElement(part, "metadata", {"key": "name", "value": name})
    settings = {"filament_colour": [""], "filament_type": [filament_type or ""]}
    dest_path = Path(dest)
    with zipfile.ZipFile(dest_path, "x", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS.format(target=ROOT_MODEL))
        archive.writestr(ROOT_MODEL, root)
        archive.writestr("3D/_rels/3dmodel.model.rels", _RELS.format(target=PARTS_MODEL))
        archive.writestr(PARTS_MODEL, parts)
        archive.writestr(MODEL_CONFIG, ElementTree.tostring(config, xml_declaration=True))
        archive.writestr(PROJECT_CONFIG, json.dumps(settings))
    return dest_path
