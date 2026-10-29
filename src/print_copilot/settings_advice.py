"""Process settings worth trying for one model, from its geometry and purpose.

Nothing here is measured: every proposal is a **heuristic** or a common rule
of thumb, labelled as such, and comes with the evidence that triggered it. The
proposals are returned as variants for `quality.review_print` or
`actions.compare_variants`, which slice them and report the measured numbers.
Only a few keys are proposed, on purpose: the ones the model's shape or the
user's purpose clearly bear on. Speeds, temperatures, cooling and flow stay as
the presets set them; tuning those needs test prints, not geometry.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np

from print_copilot import actions
from print_copilot.errors import ModelError
from print_copilot.model.mesh import load_mesh
from print_copilot.model.orientation import classify_faces
from print_copilot.model.sections import ThicknessProfile, thickness_profile
from print_copilot.orca.project import is_project, read_project
from print_copilot.orca.slicer import OverrideValue

Purpose = Literal["visual", "functional"]
PURPOSES: tuple[str, ...] = ("visual", "functional")

# Thin features: share of the sampled outline, by length, thinner than two
# outer wall lines above which variable-width (Arachne) walls are proposed.
# `wall_generator = arachne` is a value used by profiles shipped with
# Snapmaker Orca 2.4.1 (resources/profiles/*/process); the U1 presets use classic.
THIN_SHARE_MIN = 0.02
# Layer height proposed for visual prints: the layer height of the
# "0.12mm Standard @Snapmaker U1 (0.4 nozzle)" system preset of Snapmaker Orca 2.4.1.
VISUAL_LAYER_MM = 0.12
# Visual prints get finer layers when this share of the upward surface is
# sloped where layer steps show, or when the print is this short (heuristic).
STAIR_SHARE_MIN = 0.25
SMALL_PRINT_MM = 30.0
# Functional prints: common rule of thumb for strength, not U1-specific.
FUNCTIONAL_WALL_LOOPS = 4
FUNCTIONAL_INFILL_PERCENT = 25
# Height divided by the square root of the bed contact area above which a
# print is called tall on a small base and an outer brim is proposed
# (heuristic). `outer_only` is a `brim_type` value the CLI applies, and the
# config block shows `auto_brim` when the U1 presets leave the key out; auto
# brim added one to a 5 x 5 x 60 mm pillar but none to a 100 mm figure on
# 335 mm2 (2.4.1, 2026-10-29).
TALL_RATIO = 4.0
SLICER_DEFAULT_BRIM = "auto_brim"
# review_print compares at most four candidates plus the baseline.
MAX_VARIANTS = 4

HEURISTIC_LABEL = (
    "heuristic, not a slice: thickness is the inward distance across the outline of "
    "horizontal sections sampled through the model as loaded (print orientation, Z up); "
    "stair share is the upward surface sloped where layer steps show divided by all upward "
    "surface; functional wall and infill values are a common rule of thumb"
)


@dataclass
class SettingProposal:
    key: str
    current: str | None
    proposed: OverrideValue
    reason: str
    basis: Literal["geometry heuristic", "rule of thumb"]


@dataclass
class GeometryFacts:
    """Shape of the model as loaded, which is how it will be printed."""

    face_count: int
    size_mm: list[float]
    height_mm: float
    volume_mm3: float
    bed_contact_area_mm2: float
    height_to_contact: float | None  # height / sqrt(bed contact area); None without contact
    upward_area_mm2: float
    stair_area_mm2: float
    stair_share: float
    outer_wall_line_width_mm: float
    thickness: ThicknessProfile


@dataclass
class SettingsSuggestion:
    model: Path
    machine: str
    process: str
    purpose: str | None
    geometry: GeometryFacts
    proposals: list[SettingProposal]
    variants: list[actions.Variant]  # pass to review_print or compare_variants
    heuristic: str = HEURISTIC_LABEL
    notes: list[str] = field(default_factory=list)


def _number(value: object) -> float | None:
    try:
        number = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first(value: object) -> object:
    return value[0] if isinstance(value, list) and value else value


def outer_line_width(selection: actions.Selection) -> tuple[float, str | None]:
    """The process' outer wall line width in mm and a note when a fallback was used.

    A percentage is of the nozzle diameter; 0 means "use line_width"."""
    nozzle = _number(_first(selection.machine.settings.get("nozzle_diameter")))
    for key in ("outer_wall_line_width", "line_width"):
        raw = selection.process.settings.get(key)
        value = _number(raw)
        if value is None or value <= 0:
            continue
        if str(raw).strip().endswith("%"):
            if nozzle is None:
                continue
            value = nozzle * value / 100
        note = None if key == "outer_wall_line_width" else f"outer wall line width from {key}"
        return value, note
    if nozzle and nozzle > 0:
        return nozzle, "no usable line width in the process; using the nozzle diameter"
    raise ModelError(
        f"Neither the process {selection.process.name!r} nor the machine "
        f"{selection.machine.name!r} gives a usable line width or nozzle diameter."
    )


def _layer_limits(selection: actions.Selection) -> tuple[float, float] | None:
    lows = selection.machine.settings.get("min_layer_height")
    highs = selection.machine.settings.get("max_layer_height")
    low = [_number(v) for v in (lows if isinstance(lows, list) else [lows])]
    high = [_number(v) for v in (highs if isinstance(highs, list) else [highs])]
    if not low or not high or None in low or None in high:
        return None
    return max(low), min(high)


def suggest_settings(
    model: str | os.PathLike[str],
    selection: actions.Selection,
    *,
    purpose: Purpose | None = None,
) -> SettingsSuggestion:
    """Measure the model's shape and propose the few process settings it bears
    on, as variants to slice and compare. No slicing happens here.

    The model is analysed as loaded: pass the rotated STL that
    `optimize_orientation` wrote when the orientation was changed.
    """
    if purpose is not None and purpose not in PURPOSES:
        raise ModelError(f"Unknown purpose {purpose!r}; choose from {', '.join(PURPOSES)}.")
    path = Path(model)
    mesh = load_mesh(path)
    process = selection.process.settings
    notes = list(mesh.notes)

    line_mm, note = outer_line_width(selection)
    if note:
        notes.append(note)
    profile = thickness_profile(mesh, [round(2 * line_mm, 3), round(line_mm, 3)])
    notes += profile.notes

    classes = classify_faces(mesh, np.eye(3))
    areas = mesh.areas_mm2
    upward = mesh.normals[:, 2] > math.sin(math.radians(2.0))
    upward_area = float(areas[upward].sum())
    stair_area = float(areas[classes.stair].sum())
    contact = float(areas[classes.on_bed].sum())
    height = classes.size_mm[2]
    facts = GeometryFacts(
        face_count=mesh.face_count,
        size_mm=[round(v, 3) for v in classes.size_mm],
        height_mm=round(height, 3),
        volume_mm3=round(mesh.volume_mm3, 1),
        bed_contact_area_mm2=round(contact, 2),
        height_to_contact=round(height / math.sqrt(contact), 2) if contact > 0 else None,
        upward_area_mm2=round(upward_area, 2),
        stair_area_mm2=round(stair_area, 2),
        stair_share=round(stair_area / upward_area, 4) if upward_area > 0 else 0.0,
        outer_wall_line_width_mm=round(line_mm, 3),
        thickness=profile,
    )

    proposals: list[SettingProposal] = []
    thin_share, very_thin_share = profile.thin_shares
    generator = process.get("wall_generator")
    if thin_share >= THIN_SHARE_MIN and generator == "classic":
        proposals.append(
            SettingProposal(
                "wall_generator",
                generator,
                "arachne",
                f"{thin_share:.1%} of the sampled outline is thinner than two outer wall lines "
                f"({2 * line_mm:.2f} mm), at heights {_heights(profile.thin_heights_mm)}; "
                "fixed-width walls do not fit such features (they overlap, leave gaps or skip "
                "them) and variable-width walls adapt to them. Check those layers in the "
                "toolpath preview.",
                "geometry heuristic",
            )
        )
    if very_thin_share > 0:
        notes.append(
            f"{very_thin_share:.1%} of the sampled outline is thinner than one line width "
            f"({line_mm:.2f} mm, thinnest sample {profile.min_thickness_mm} mm): those parts "
            "may print weak or not at all with any setting; check them in the preview."
        )

    if purpose is None:
        notes.append(
            "No purpose given: layer height, walls and infill were not judged. Ask whether the "
            "print is visual (looks matter) or functional (strength matters)."
        )
    elif purpose == "visual":
        layer = _number(process.get("layer_height"))
        limits = _layer_limits(selection)
        target = VISUAL_LAYER_MM
        if limits:
            target = min(max(target, limits[0]), limits[1])
        stairs = facts.stair_share >= STAIR_SHARE_MIN
        small = height <= SMALL_PRINT_MM
        if layer is not None and layer > target and (stairs or small):
            why = []
            if stairs:
                why.append(
                    f"{facts.stair_share:.0%} of the upward surface is sloped "
                    "where layer steps show"
                )
            if small:
                why.append(f"the print is only {height:.1f} mm tall")
            proposals.append(
                SettingProposal(
                    "layer_height",
                    process.get("layer_height"),
                    target,
                    f"Visual print and {' and '.join(why)}; {target:g} mm is the layer height "
                    "of the U1 0.12mm Standard system preset. Costs time: compare the slices.",
                    "geometry heuristic",
                )
            )
    else:
        walls = _number(process.get("wall_loops"))
        if walls is not None and walls < FUNCTIONAL_WALL_LOOPS:
            proposals.append(
                SettingProposal(
                    "wall_loops",
                    process.get("wall_loops"),
                    FUNCTIONAL_WALL_LOOPS,
                    "Functional print: more walls add most of a part's strength.",
                    "rule of thumb",
                )
            )
        infill = _number(process.get("sparse_infill_density"))
        if infill is not None and infill < FUNCTIONAL_INFILL_PERCENT:
            proposals.append(
                SettingProposal(
                    "sparse_infill_density",
                    process.get("sparse_infill_density"),
                    f"{FUNCTIONAL_INFILL_PERCENT}%",
                    "Functional print: denser infill supports top surfaces and resists crushing.",
                    "rule of thumb",
                )
            )

    if facts.height_to_contact is None:
        notes.append(
            "The model touches the bed only at points or edges as loaded; it needs supports, "
            "a brim or another orientation to stay put."
        )
    elif facts.height_to_contact > TALL_RATIO:
        brim = process.get("brim_type", SLICER_DEFAULT_BRIM)
        if brim in (SLICER_DEFAULT_BRIM, "no_brim"):
            proposals.append(
                SettingProposal(
                    "brim_type",
                    brim,
                    "outer_only",
                    f"Tall on a small base: {height:.1f} mm high on {contact:.0f} mm2 of bed "
                    "contact. The slicer's automatic brim decides by itself and may add none; "
                    "an outer brim keeps such a part from tipping or lifting. Check the first "
                    "layer in the toolpath preview.",
                    "geometry heuristic",
                )
            )
    if path.suffix.lower() == ".3mf" and path.is_file() and is_project(path):
        notes += _object_setting_notes(path, proposals)

    return SettingsSuggestion(
        model=path,
        machine=selection.machine.name,
        process=selection.process.name,
        purpose=purpose,
        geometry=facts,
        proposals=proposals,
        variants=_variants(proposals),
        notes=notes,
    )


def _object_setting_notes(path: Path, proposals: list[SettingProposal]) -> list[str]:
    """Proposals the project's objects override with their own settings.

    An object's own value wins over a process override for that object:
    verified on 2.4.1 (2026-10-29), an object with sparse_infill_density 80%
    kept its infill when the process was overridden to 5%, while the other
    object's infill dropped."""
    proposed = {p.key for p in proposals}
    notes = []
    for obj in read_project(path).objects:
        for place, values in obj.setting_overrides().items():
            for key in sorted(proposed & values.keys()):
                notes.append(
                    f"Object {obj.name or obj.id!r} ({place}) sets {key}={values[key]} itself; "
                    f"the {key} proposal does not change it. Change it in Snapmaker Orca if "
                    "it should follow."
                )
    return notes


def _heights(values: list[float]) -> str:
    if not values:
        return "-"
    shown = ", ".join(f"{v:g}" for v in values[:6])
    return f"{shown}{', ...' if len(values) > 6 else ''} mm"


def _variants(proposals: list[SettingProposal]) -> list[actions.Variant]:
    """All proposals together, then each alone when there are several, so the
    slices show what each one costs or brings."""
    if not proposals:
        return []
    together = actions.Variant(
        {p.key: p.proposed for p in proposals}, label="suggested settings (heuristic)"
    )
    if len(proposals) == 1:
        return [together]
    alone = [actions.Variant({p.key: p.proposed}) for p in proposals]
    return [together, *alone][:MAX_VARIANTS]
