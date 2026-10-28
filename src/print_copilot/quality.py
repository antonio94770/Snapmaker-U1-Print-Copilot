"""Quality review workflow shared by MCP and CLI. No printer operations.

Creates inspectable evidence, not a certificate of print quality. Visual
review is explicit and is invalidated when any reviewed input/artifact changes.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from print_copilot import actions, multicolor
from print_copilot.errors import PrintCopilotError
from print_copilot.gcode.header import parse_gcode, read_config_block
from print_copilot.gcode.paths import read_paths
from print_copilot.orca.heads import HeadAssignment
from print_copilot.orca.install import OrcaInstall
from print_copilot.orca.project import is_project
from print_copilot.orca.slicer import DEFAULT_TIMEOUT_S, OverrideValue, default_work_dir
from print_copilot.quality_audit import Audit, Evidence, Finding, audit_config, numeric_values
from print_copilot.quality_preview import Preview, write_preview
from print_copilot.serialize import to_jsonable


class QualityError(PrintCopilotError):
    """A quality review cannot be completed or its evidence is stale."""


@dataclass
class ReviewVariant:
    label: str
    overrides: dict[str, OverrideValue]
    rationale: str
    gcode_path: Path | None = None
    time_s: int | None = None
    filament_g: float | None = None
    audit: Audit | None = None
    preview: Preview | None = None
    model_preview: Path | None = None
    error: str | None = None
    slice_notes: list[str] = field(default_factory=list)
    head_assignment: HeadAssignment | None = None


@dataclass
class QualityReport:
    schema_version: int
    source: Path
    report_path: Path
    html_path: Path
    variants: list[ReviewVariant]
    recommendation: str | None
    recommendation_basis: str
    status: str
    evidence_sha256: dict[str, str]
    notes: list[str]
    review: dict = field(default_factory=dict)


@dataclass
class ReviewReceipt:
    report_path: Path
    status: str
    variant: str
    decision: str
    observations: str
    accepted_findings: list[str]
    recorded_at_utc: str
    hardware_quality: str = "unverified"
    print_authorization: bool = False


def preview_gcode(
    gcode: str | Path,
    *,
    layers: list[int] | None = None,
    work_dir: str | Path | None = None,
    max_segments: int = 1_000_000,
) -> Preview:
    """Render requested layer numbers or representative layers; no installation needed."""
    base = Path(work_dir) if work_dir else default_work_dir()
    directory = base.resolve() / f"preview-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    directory.mkdir(parents=True, exist_ok=False)
    try:
        return write_preview(read_paths(gcode, max_segments=max_segments), directory, layers=layers)
    except (OSError, ValueError) as exc:
        raise QualityError(f"Cannot create G-code preview: {exc}") from exc


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audit_file(path: Path, directory: Path, max_segments: int) -> tuple[Audit, Preview]:
    paths = read_paths(path, max_segments=max_segments)
    config = read_config_block(path)
    if not config:
        raise QualityError(f"{path}: no effective CONFIG_BLOCK to audit.")
    measured = parse_gcode(path)
    audit = audit_config(config, paths)
    if paths.tagged_layer_count != measured.layer_count:
        audit.findings.append(
            Finding(
                "layer_count",
                "error",
                "Layer markers disagree with slicer summary",
                f"Parsed {paths.tagged_layer_count} layer markers; "
                f"summary reports {measured.layer_count}.",
                [],
                "Re-slice and check whether the file is complete.",
            )
        )
    # Verify actual extrusion bounds, including supports and the tower. Custom
    # start/change G-code is excluded because it may intentionally purge off-bed.
    segments = [s for layer in paths.layers for s in layer.segments if s.feature != "Custom"]
    area = config.get("printable_area", "")
    try:
        polygon = [tuple(float(v) for v in p.strip().split("x")) for p in area.split(",")]
        if len(polygon) < 3 or any(
            len(p) != 2 or not all(math.isfinite(v) for v in p) for p in polygon
        ):
            raise ValueError
    except ValueError:
        polygon = []
    heights = numeric_values(config.get("printable_height"))
    if polygon and len(heights) == 1 and heights[0] > 0:
        bad_line = next(
            (
                s.source_line
                for s in segments
                if any(
                    point[2] < -1e-5
                    or point[2] > heights[0] + 1e-5
                    or not _inside(point[:2], polygon)
                    for point in (s.start, s.end)
                )
            ),
            None,
        )
        audit.findings.append(
            Finding(
                "extrusion_bounds",
                "error" if bad_line else "info",
                "Commanded extrusion bounds",
                f"Extrusion outside declared printable space at G-code line {bad_line}."
                if bad_line
                else (
                    "Explicit, non-Custom extrusion endpoints fit the printable "
                    "polygon and height. No collision simulation."
                ),
                [
                    Evidence("printable_area", area, "G-code CONFIG_BLOCK"),
                    Evidence("printable_height", str(heights[0]), "G-code CONFIG_BLOCK"),
                ],
                "Inspect the full plate, tower and tool clearance.",
            )
        )
    else:
        audit.findings.append(
            Finding(
                "extrusion_bounds",
                "unverified",
                "Printable bounds unavailable",
                "No usable printable polygon and height in the effective config.",
                [],
                "Verify the selected machine preset.",
            )
        )
    bounds_keys = {"printable_area", "printable_height"}
    audit.checked_keys = sorted(set(audit.checked_keys) | bounds_keys)
    for parameter in audit.parameters:
        if parameter.key in bounds_keys:
            parameter.coverage = "rule_checked"
    try:
        preview = write_preview(paths, directory)
    except (OSError, ValueError) as exc:
        raise QualityError(f"Cannot write the complete toolpath preview: {exc}") from exc
    return audit, preview


def _inside(point, polygon) -> bool:
    """Even/odd polygon test including its edges; no rectangular-bed assumption."""
    x, y = point
    inside = False
    for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        dx, dy = b[0] - a[0], b[1] - a[1]
        if abs((x - a[0]) * dy - (y - a[1]) * dx) < 1e-5 and (
            min(a[0], b[0]) - 1e-5 <= x <= max(a[0], b[0]) + 1e-5
            and min(a[1], b[1]) - 1e-5 <= y <= max(a[1], b[1]) + 1e-5
        ):
            return True
        if (a[1] > y) != (b[1] > y) and x < a[0] + (y - a[1]) * dx / dy:
            inside = not inside
    return inside


def finer_layer_variant(selection: actions.Selection) -> actions.Variant | None:
    """One conservative geometric candidate; it is not a calibrated optimum.

    Halve the selected layer height, clamped to the machine's own lower limit.
    Keep temperatures, flow, cooling, retraction and hardware settings unchanged.
    """
    try:
        current = float(selection.process.settings["layer_height"])
        lows = [float(v) for v in selection.machine.settings["min_layer_height"]]
        highs = [float(v) for v in selection.machine.settings["max_layer_height"]]
        if (
            not lows
            or not highs
            or not all(math.isfinite(v) and v > 0 for v in [current, *lows, *highs])
        ):
            return None
        target = round(max(current / 2, max(lows)), 5)
        if target >= current or target > min(highs):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return actions.Variant({"layer_height": target}, label="finer layers (heuristic)")


def _recommendation_key(variant: ReviewVariant):
    # Feature presence invites inspection; it is not itself a defect. Detected
    # configuration risks take precedence over the preference for thinner layers.
    inspection_only = {
        "bridge",
        "overhang",
        "support_interface",
        "calibration",
        "physical_condition",
    }
    risks = sum(
        f.severity in {"warning", "unverified"} and f.code not in inspection_only
        for f in variant.audit.findings
    )
    height = next(
        (
            numeric_values(p.value)[0]
            for p in variant.audit.parameters
            if p.key == "layer_height" and numeric_values(p.value)
        ),
        math.inf,
    )
    return risks, height, variant.time_s


def review_print(
    model: str | Path,
    *,
    install: OrcaInstall | None = None,
    selection: actions.Selection | None = None,
    plate: int | None = None,
    mapping: dict[int, int] | None = None,
    reduce_colours: bool = False,
    head: int | None = None,
    variants: list[actions.Variant] | None = None,
    compare: bool = True,
    work_dir: str | Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_segments: int = 1_000_000,
) -> QualityReport:
    """Audit existing G-code or slice STL/one 3MF plate and compare variants.

    Always retains a baseline; at most four candidates plus baseline. A failed
    candidate is visible and never turns into a different printer/preset.
    Successful results remain pending until explicit visual-review attestation.
    """
    source = Path(model).resolve()
    if max_segments <= 0:
        raise QualityError("max_segments must be positive.")
    if not source.is_file():
        raise QualityError(f"Quality review input not found: {source}")
    if source.suffix.lower() not in {".gcode", ".stl", ".3mf"}:
        raise QualityError("Quality review accepts STL, 3MF projects or sliced .gcode files.")
    is_gcode = source.suffix.lower() == ".gcode"
    if is_gcode and (
        variants or plate is not None or mapping or reduce_colours or head is not None
    ):
        raise QualityError("Existing G-code can only be audited; slicing options require a model.")
    if not is_gcode and (install is None or selection is None):
        raise QualityError(
            "Model quality review requires an Orca installation and resolved presets."
        )
    if variants and not compare:
        raise QualityError("Comparison variants cannot be combined with compare=False.")
    specs = [actions.Variant({}, label="baseline")]
    if not is_gcode and compare:
        if variants:
            specs.extend(variants)
        else:
            candidate = finer_layer_variant(selection)
            if candidate:
                specs.append(candidate)
    if len(specs) > 5:
        raise QualityError("At most four comparison variants plus the baseline are allowed.")
    labels = [v.display_label() for v in specs]
    if len(labels) != len(set(labels)):
        raise QualityError("Quality variant labels must be unique, including 'baseline'.")
    base = Path(work_dir) if work_dir else default_work_dir()
    folder = base.resolve() / f"quality-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    folder.mkdir(parents=True, exist_ok=False)
    original_hash = file_hash(source)
    evidence = {str(source): original_hash}
    results = []
    # An STL on a chosen head goes through the project workflow like a 3MF.
    project = not is_gcode and (
        head is not None or (source.suffix.lower() == ".3mf" and is_project(source))
    )
    for index, spec in enumerate(specs):
        directory = folder / f"variant-{index}"
        directory.mkdir()
        rationale = (
            "Unchanged selected setup; reference for all comparisons."
            if index == 0
            else (
                "Heuristic candidate; changed settings must be checked visually "
                "and on a test print."
            )
        )
        variant = ReviewVariant(spec.display_label(), spec.overrides, rationale)
        results.append(variant)
        try:
            if is_gcode:
                gcode, geometry = source, None
            elif project:
                run = multicolor.slice_project(
                    install,
                    source,
                    selection,
                    plate=plate,
                    mapping=mapping,
                    reduce_colours=reduce_colours,
                    head=head,
                    overrides=spec.overrides,
                    work_dir=directory,
                    timeout_s=timeout_s,
                )
                gcode, geometry = run.gcode_path, run.slicing_copy
            else:
                run = actions.run_slice(
                    install,
                    source,
                    selection,
                    overrides=spec.overrides,
                    work_dir=directory,
                    timeout_s=timeout_s,
                )
                gcode, geometry = run.gcode_path, source
            variant.gcode_path = gcode
            if not is_gcode:
                variant.slice_notes = list(selection.notes) + list(getattr(run, "notes", []))
                if project:
                    variant.head_assignment = run.assignment
            before = file_hash(gcode)
            measured = parse_gcode(gcode)
            variant.time_s, variant.filament_g = (
                measured.estimated_time_s,
                measured.total_filament_g,
            )
            variant.audit, variant.preview = _audit_file(gcode, directory, max_segments)
            if variant.head_assignment and any(
                choice.type_match is False for choice in variant.head_assignment.choices
            ):
                variant.audit.findings.append(
                    Finding(
                        "head_material_mismatch",
                        "warning",
                        "Project and head filament types differ",
                        "A requested project material differs from the assigned head preset. "
                        "Inspect the assignment and notes before accepting the conversion.",
                        [],
                        "Choose suitable head presets, confirm the loaded spools, and re-slice.",
                    )
                )
            if file_hash(gcode) != before:
                raise QualityError(
                    "G-code changed while it was being reviewed; start a new review."
                )
            evidence[str(gcode)] = before
            if geometry:
                effective = read_config_block(gcode)
                thresholds = numeric_values(effective.get("support_threshold_angle"))
                threshold = thresholds[0] if len(thresholds) == 1 and thresholds[0] > 0 else None
                rendered = actions.render_model(
                    geometry,
                    out_path=directory / "model.png",
                    selection=selection,
                    threshold_deg=threshold,
                )
                variant.model_preview = rendered.png_path
                evidence[str(rendered.png_path)] = file_hash(rendered.png_path)
                overhang = rendered.face_areas_mm2.get("needs_support", 0)
                if overhang and effective.get("enable_support") == "0":
                    variant.audit.findings.append(
                        Finding(
                            "overhangs_without_support",
                            "warning",
                            "Geometric overhangs with supports disabled",
                            f"The model renderer flags {overhang:g} mm2 of downward-facing surface "
                            "using its heuristic threshold. Some spans may be printable bridges; "
                            "the geometry score does not establish a defect.",
                            [Evidence("enable_support", "0", "G-code CONFIG_BLOCK")],
                            "Inspect these regions; compare orientation and support strategies "
                            "before acceptance.",
                            heuristic=True,
                        )
                    )
            for artifact in (variant.preview.png_path, variant.preview.html_path):
                evidence[str(artifact)] = file_hash(artifact)
        except (PrintCopilotError, OSError, ValueError) as exc:
            variant.error = str(exc)
            # A failed baseline is never replaced by a successful candidate.
            if index == 0:
                break
    if file_hash(source) != original_hash:
        raise QualityError("Source changed during quality review; start a new review.")
    eligible = [
        v
        for v in results
        if not v.error and v.audit and not any(f.severity == "error" for f in v.audit.findings)
    ]
    baseline_ok = (
        bool(results[0].audit)
        and not results[0].error
        and not any(f.severity == "error" for f in results[0].audit.findings)
    )
    recommended = min(eligible, key=_recommendation_key) if eligible and baseline_ok else None
    report = QualityReport(
        1,
        source,
        folder / "report.json",
        folder / "report.html",
        results,
        recommended.label if recommended else None,
        (
            "Heuristic: among candidates without errors, prefer fewer configuration "
            "warnings/unverified checks, then lower nominal layer height, then slicer time. "
            "Feature-presence inspection and physical calibration findings do not rank "
            "candidates. No measured roughness or physical winner."
        ),
        "blocked" if not eligible or not baseline_ok else "visual_review_pending",
        evidence,
        [
            "No firmware, installation, user preset or printer was modified.",
            (
                "Full effective settings are inventoried; listed_only means no "
                "automatic rule validated that parameter."
            ),
            (
                "View the model, selected-layer PNG and all relevant layers in "
                "the interactive preview before recording a review."
            ),
            (
                "Recorded review is an attestation, not proof that an agent "
                "viewed an image, and never authorizes a physical print."
            ),
            (
                "All variants retain their logs. Calibration and print quality "
                "remain unverified on hardware."
            ),
        ],
    )
    _save_report(report)
    return report


def _save_report(report: QualityReport):
    content = to_jsonable(report)
    # Seal report content separately so edits to findings invalidate attestation.
    content["review_input_sha256"] = _content_hash(content)
    report.report_path.write_text(
        json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _write_report_html(content, report.html_path)


def _content_hash(data):
    immutable = {
        k: v for k, v in data.items() if k not in {"review", "status", "review_input_sha256"}
    }
    return hashlib.sha256(
        json.dumps(immutable, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _write_report_html(data, path):
    esc = lambda v: html.escape(str(v))  # noqa: E731
    cards = []
    for v in data["variants"]:
        body = f"<h2>{esc(v['label'])}</h2><p>{esc(v['rationale'])}</p>"
        if v.get("slice_notes"):
            body += "<details><summary>Preset and head-assignment notes</summary><p>"
            body += "<br>".join(esc(note) for note in v["slice_notes"]) + "</p></details>"
        if v.get("head_assignment"):
            body += "<details><summary>Project filaments assigned to U1 heads</summary><pre>"
            body += esc(json.dumps(v["head_assignment"], indent=2)) + "</pre></details>"
        if v.get("error"):
            body += f"<p class='error'>{esc(v['error'])}</p>"
        if v.get("audit"):
            body += f"<p>Slicer time: {esc(v['time_s'])} s · filament: {esc(v['filament_g'])} g</p>"
            preview = Path(v["preview"]["html_path"]).relative_to(path.parent).as_posix()
            png = Path(v["preview"]["png_path"]).relative_to(path.parent).as_posix()
            body += (
                f"<a href='{esc(preview)}'>Inspect every layer</a>"
                f"<img src='{esc(png)}' alt='Selected G-code layers'>"
            )
            if v.get("model_preview"):
                model_png = Path(v["model_preview"]).relative_to(path.parent).as_posix()
                body += (
                    "<details><summary>Model geometry</summary>"
                    f"<img src='{esc(model_png)}' alt='Model views'></details>"
                )
            for f in v["audit"]["findings"]:
                body += (
                    f"<p class='{esc(f['severity'])}'><b>{esc(f['severity'].upper())}: "
                    f"{esc(f['title'])}</b><br>{esc(f['explanation'])}"
                    f"<br>{esc(f['next_step'])}</p>"
                )
            body += (
                "<details><summary>Complete effective-settings "
                "inventory</summary><table><tr><th>Parameter</th><th>Effective "
                "value</th><th>Coverage</th></tr>"
            )
            for p in v["audit"]["parameters"]:
                body += (
                    f"<tr><td>{esc(p['key'])}</td><td><pre>{esc(p['value'])}</pre></td>"
                    f"<td>{esc(p['coverage'])}</td></tr>"
                )
            body += "</table></details>"
        cards.append(f"<section>{body}</section>")
    content = Path(__file__).with_name("quality_report.html").read_text(encoding="utf-8")
    content += (
        f"<h1>U1 quality review</h1><p><b>Status: {esc(data['status'])}</b></p>"
        f"<p>Candidate for review: {esc(data['recommendation'])}</p>"
        f"<p>{esc(data['recommendation_basis'])}</p>"
    )
    content += "<p>" + "<br>".join(esc(v) for v in data["notes"]) + "</p>" + "".join(cards)
    content += (
        "<h2>Recorded review</h2><pre>"
        + esc(json.dumps(data.get("review", {}), indent=2))
        + "</pre></html>"
    )
    path.write_text(content, encoding="utf-8")


def record_quality_review(
    report_path: str | Path,
    *,
    variant: str,
    decision: Literal["accept_with_limitations", "revise"],
    observations: str,
    accepted_findings: list[str] | None = None,
) -> ReviewReceipt:
    """Record an explicit visual-review attestation for unchanged artifacts.

    Acceptance requires written observations and acknowledgement of every
    warning/unverified finding for the chosen variant. Errors cannot be waived.
    An attestation is never a print-start confirmation or a quality certificate.
    """
    path = Path(report_path).resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["schema_version"] != 1 or data["review_input_sha256"] != _content_hash(data):
            raise QualityError(
                "Quality report contents changed or its schema is unsupported; run a new review."
            )
        if Path(data["report_path"]).resolve() != path:
            raise QualityError("The review report was moved; generate a new review.")
        for name, expected in data["evidence_sha256"].items():
            if file_hash(Path(name)) != expected:
                raise QualityError(f"Review evidence changed: {name}. Generate a new review.")
        chosen = next(v for v in data["variants"] if v["label"] == variant)
    except (OSError, ValueError, KeyError, StopIteration, TypeError) as exc:
        raise QualityError(f"Cannot verify the quality review and chosen variant: {exc}") from exc
    if decision not in {"accept_with_limitations", "revise"}:
        raise QualityError("Decision must be accept_with_limitations or revise.")
    if not observations.strip():
        raise QualityError("Written visual observations are required; describe what you inspected.")
    if decision == "accept_with_limitations":
        if chosen.get("error") or not chosen.get("audit") or data["variants"][0].get("error"):
            raise QualityError("A failed variant or failed baseline cannot be accepted.")
        findings = chosen["audit"]["findings"]
        if any(
            f["severity"] == "error" for f in findings + data["variants"][0]["audit"]["findings"]
        ):
            raise QualityError("Detected errors cannot be waived; revise the slice first.")
        required = {f["code"] for f in findings if f["severity"] in {"warning", "unverified"}}
        if missing := required - set(accepted_findings or []):
            raise QualityError(f"Acknowledge remaining findings explicitly: {sorted(missing)}")
    data["review"] = {
        "variant": variant,
        "decision": decision,
        "observations": observations.strip(),
        "accepted_findings": sorted(set(accepted_findings or [])),
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "hardware_quality": "unverified",
        "print_authorization": False,
    }
    data["status"] = (
        "reviewed_with_limitations"
        if decision == "accept_with_limitations"
        else "revision_requested"
    )
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_report_html(data, Path(data["html_path"]))
    return ReviewReceipt(path, data["status"], **{k: v for k, v in data["review"].items()})
