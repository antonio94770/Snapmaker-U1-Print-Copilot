"""Evidence-based audit of the effective config and commanded extrusion paths.

Rules use the slice's own preset limits. Appearance advice is explicitly
heuristic; calibration, moisture and mechanical condition stay unverified.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import cache
from typing import Literal

from print_copilot.gcode.paths import Toolpaths

CALIBRATION_DOC = "https://github.com/OrcaSlicer/OrcaSlicer/wiki/calibration_guide"
U1_DOC = "https://wiki.snapmaker.com/en/snapmaker_u1/troubleshooting/first_layer_printing_troubleshooting_guide"


@dataclass
class Evidence:
    name: str
    value: str
    source: str


@dataclass
class Finding:
    code: str
    severity: Literal["error", "warning", "info", "unverified"]
    title: str
    explanation: str
    evidence: list[Evidence]
    next_step: str
    heuristic: bool = False
    reference: str | None = None


@dataclass
class Parameter:
    key: str
    value: str
    category: str
    coverage: Literal["rule_checked", "listed_only"]


@dataclass
class Audit:
    findings: list[Finding]
    parameters: list[Parameter]
    checked_keys: list[str]
    tools_used: list[int]
    feature_extrusion_mm: dict[str, float]
    peak_commanded_flow_mm3_s: dict[int, float]
    limitations: list[str]


def numeric_values(value: str | None) -> list[float]:
    if value is None:
        return []
    try:
        values = [float(v.strip().strip('"')) for v in re.split(r"[,;]", value)]
    except ValueError:
        return []
    return values if all(math.isfinite(v) for v in values) else []


def _category(key):
    for group, terms in (
        (
            "temperature and material",
            ("temperature", "filament_type", "filament_diameter", "bed_type"),
        ),
        ("extrusion and calibration", ("flow", "volumetric", "pressure_advance", "retract")),
        ("cooling", ("fan", "cool", "slow_down")),
        ("supports and adhesion", ("support", "raft", "brim", "skirt")),
        (
            "surface and detail",
            ("layer_height", "wall", "shell", "surface", "seam", "fuzzy", "ironing", "resolution"),
        ),
        ("motion", ("speed", "accel", "jerk")),
        ("U1 tool changes", ("wipe", "prime", "flush", "toolchange", "extruder")),
    ):
        if any(term in key for term in terms):
            return group
    return "other (inventory only)"


def audit_config(config: dict[str, str], paths: Toolpaths) -> Audit:
    findings: list[Finding] = []
    checked: set[str] = set()
    tools = sorted({s.tool for layer in paths.layers for s in layer.segments})

    def add(
        code,
        severity,
        title,
        explanation,
        keys=(),
        next_step="Inspect the preview and the effective settings.",
        heuristic=False,
        reference=None,
    ):
        checked.update(keys)
        findings.append(
            Finding(
                code,
                severity,
                title,
                explanation,
                [Evidence(key, config.get(key, "missing"), "G-code CONFIG_BLOCK") for key in keys],
                next_step,
                heuristic,
                reference,
            )
        )

    nozzles = numeric_values(config.get("nozzle_diameter"))
    checked.add("nozzle_diameter")
    if config.get("printer_model") != "Snapmaker U1":
        add(
            "u1_identity",
            "error",
            "U1 identity is not verified",
            "The slice does not identify the target as Snapmaker U1.",
            ("printer_model",),
        )
    else:
        add(
            "u1_identity",
            "info",
            "U1 identity present",
            "The effective config identifies Snapmaker U1.",
            ("printer_model",),
        )
    if (
        len(nozzles) != 4
        or any(v <= 0 for v in nozzles)
        or any(t < 0 or t >= len(nozzles) for t in tools)
    ):
        add(
            "tool_slots",
            "error",
            "Invalid U1 tool/nozzle slots",
            "Expected four positive nozzle diameters and matching used tool indexes.",
            ("nozzle_diameter",),
        )

    @cache
    def per_tool(key, tool):
        checked.add(key)
        values = numeric_values(config.get(key))
        if len(values) == 1:
            return values[0]
        if len(values) == len(nozzles) and 0 <= tool < len(values):
            return values[tool]
        return None

    layer_values = numeric_values(config.get("layer_height"))
    checked.add("layer_height")
    if len(layer_values) != 1 or layer_values[0] <= 0:
        add(
            "layer_height",
            "error",
            "Invalid layer height",
            "A positive nominal layer height is required.",
            ("layer_height",),
        )
    else:
        for tool in tools:
            low, high = per_tool("min_layer_height", tool), per_tool("max_layer_height", tool)
            keys = ("layer_height", "min_layer_height", "max_layer_height")
            if low is None or high is None or low <= 0 or high < low:
                add(
                    f"layer_limits_T{tool}",
                    "unverified",
                    f"T{tool} layer limits unavailable",
                    "The effective config has no usable per-head layer limits.",
                    keys,
                )
            elif not low <= layer_values[0] <= high:
                add(
                    f"layer_limits_T{tool}",
                    "error",
                    f"T{tool} layer height outside preset limits",
                    f"Nominal layer height must be within the preset's {low:g}..{high:g} mm range.",
                    keys,
                )
            else:
                add(
                    f"layer_limits_T{tool}",
                    "info",
                    f"T{tool} nominal layer height within preset limits",
                    (
                        "This checks preset limits, not achievable surface finish or "
                        "variable layer ranges."
                    ),
                    keys,
                )

    for key, code in (
        ("wall_loops", "walls"),
        ("top_shell_layers", "top_shell"),
        ("bottom_shell_layers", "bottom_shell"),
    ):
        values = numeric_values(config.get(key))
        checked.add(key)
        if not values:
            add(
                code,
                "unverified",
                f"{key} unavailable",
                "No numeric value in the effective config.",
                (key,),
            )
        elif values[0] <= 0:
            add(
                code,
                "warning",
                f"No {code.replace('_', ' ')} requested",
                (
                    "An open or wall-less model may be intentional; confirm it "
                    "against the desired part."
                ),
                (key,),
                heuristic=True,
            )

    for key, inactive, code, title in (
        ("fuzzy_skin", {"none"}, "texture", "Surface texturing enabled"),
        ("seam_position", {"aligned"}, "seams", "Review seam distribution"),
    ):
        value = config.get(key)
        checked.add(key)
        if value is None:
            add(
                code, "unverified", f"{key} unavailable", "Appearance behaviour is unknown.", (key,)
            )
        elif value not in inactive:
            add(
                code,
                "warning",
                title,
                (
                    "This appearance choice can conflict with a smooth visible "
                    "surface; confirm the intention. No automatic correction is "
                    "applied."
                ),
                (key,),
                heuristic=True,
            )

    features: dict[str, float] = {}
    peaks: dict[int, float] = {}
    bad_temperatures: dict[int, set[float]] = {}
    missing_temperatures: set[int] = set()
    flow_unknown: set[int] = set()
    for layer in paths.layers:
        for seg in layer.segments:
            features[seg.feature] = features.get(seg.feature, 0) + seg.extrusion_mm
            diameter = per_tool("filament_diameter", seg.tool)
            if diameter is None or diameter <= 0 or seg.feed_mm_s is None or seg.feed_mm_s <= 0:
                flow_unknown.add(seg.tool)
            elif seg.length_mm > 0:
                demand = (
                    seg.extrusion_mm * math.pi * (diameter / 2) ** 2 * seg.feed_mm_s / seg.length_mm
                )
                peaks[seg.tool] = max(peaks.get(seg.tool, 0), demand)
            if seg.temperature_c is None:
                missing_temperatures.add(seg.tool)
            else:
                low = per_tool("nozzle_temperature_range_low", seg.tool)
                high = per_tool("nozzle_temperature_range_high", seg.tool)
                if low is None or high is None or low <= 0 or high < low:
                    missing_temperatures.add(seg.tool)
                elif not low <= seg.temperature_c <= high:
                    bad_temperatures.setdefault(seg.tool, set()).add(seg.temperature_c)
    for tool in tools:
        temp_keys = ("nozzle_temperature_range_low", "nozzle_temperature_range_high")
        if tool in bad_temperatures:
            add(
                f"temperature_T{tool}",
                "warning",
                f"T{tool} commanded temperature outside filament range",
                f"Explicit temperature commands during extrusion: "
                f"{sorted(bad_temperatures[tool])} C. Commands are not sensor readings.",
                temp_keys,
            )
        elif tool in missing_temperatures:
            add(
                f"temperature_T{tool}",
                "unverified",
                f"T{tool} temperature cannot be fully checked",
                (
                    "Temperature commands or filament limits are missing; macro "
                    "heating is not interpreted."
                ),
                temp_keys,
            )
        else:
            add(
                f"temperature_T{tool}",
                "info",
                f"T{tool} explicit temperatures within declared range",
                "Preset range compliance does not establish the best temperature for this spool.",
                temp_keys,
            )
        limit = per_tool("filament_max_volumetric_speed", tool)
        if tool in flow_unknown or tool not in peaks or limit is None or limit <= 0:
            add(
                f"flow_T{tool}",
                "unverified",
                f"T{tool} extrusion demand not fully checked",
                "A positive filament diameter, feed rate and preset volumetric limit are needed.",
                ("filament_diameter", "filament_max_volumetric_speed"),
            )
        else:
            peak = peaks[tool]
            add(
                f"flow_T{tool}",
                "warning" if peak > limit * 1.01 else "info",
                f"T{tool} commanded extrusion demand",
                f"Peak commanded demand {peak:.3f} mm3/s; preset limit {limit:g} mm3/s. "
                "A 1% comparison tolerance allows G-code rounding. "
                "Acceleration and achieved flow are not simulated.",
                ("filament_max_volumetric_speed",),
            )
    for term in ("Bridge", "Overhang", "Support interface"):
        length = sum(v for k, v in features.items() if term.lower() in k.lower())
        if length:
            add(
                term.lower().replace(" ", "_"),
                "warning",
                f"Inspect {term.lower()} paths",
                f"{length:.3f} mm of filament is commanded for this feature. "
                "Its presence is not proof of a defect.",
                heuristic=True,
            )
    if len(tools) > 1 and config.get("enable_prime_tower") == "0":
        add(
            "tower_disabled",
            "warning",
            "Multiple U1 heads without a prime tower",
            (
                "The G-code uses multiple heads and the effective config disables "
                "the tower. Surface quality needs a real print check."
            ),
            ("enable_prime_tower",),
            reference=U1_DOC,
        )
    add(
        "calibration",
        "unverified",
        "Filament and head calibration not verified",
        (
            "Flow ratio, pressure advance, retraction, cooling and "
            "temperature settings are inventoried but not proven calibrated "
            "for this head and spool."
        ),
        (),
        (
            "Use physical calibration evidence for each loaded head/material "
            "before claiming validated finish."
        ),
        reference=CALIBRATION_DOC,
    )
    add(
        "physical_condition",
        "unverified",
        "Physical print conditions not observed",
        (
            "Spool dryness, nozzle/bed condition, adhesion, U1 head offsets, "
            "ringing, layer shifts and actual surface roughness cannot be "
            "certified from files."
        ),
        (),
        "Inspect hardware and a test print or photos; do not invent defect diagnoses.",
        reference=U1_DOC,
    )
    parameters = [
        Parameter(k, v, _category(k), "rule_checked" if k in checked else "listed_only")
        for k, v in sorted(config.items())
    ]
    return Audit(
        findings,
        parameters,
        sorted(checked),
        tools,
        {k: round(v, 5) for k, v in features.items()},
        {k: round(v, 5) for k, v in peaks.items()},
        [
            "Inventory is complete for CONFIG_BLOCK keys, not a validation of every setting.",
            "Nozzle temperatures are commanded targets, not measured hardware temperatures.",
            (
                "No sensor data, macro expansion, collision simulation or "
                "physical roughness prediction."
            ),
            "Firmware pre-extrusion consumption is not represented: TO VERIFY ON HARDWARE.",
        ],
    )
