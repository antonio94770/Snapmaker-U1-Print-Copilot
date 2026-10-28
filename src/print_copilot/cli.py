"""`print-copilot` command line. Same features as the MCP tools, no AI needed."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from print_copilot import __version__, actions, multicolor, quality
from print_copilot.errors import ConfirmationError, ModelError, PrintCopilotError, SliceError
from print_copilot.model.orientation import GOALS, OrientationAnalysis, Region
from print_copilot.model.render import VIEWS
from print_copilot.orca.install import locate, locate_data_dir
from print_copilot.orca.presets import DEFAULT_VENDOR, KINDS, PresetLibrary
from print_copilot.orca.slicer import DEFAULT_TIMEOUT_S, SliceRun
from print_copilot.printer import actions as printer
from print_copilot.printer import discovery
from print_copilot.printer.fake import FakeMoonraker
from print_copilot.printer.moonraker import ENV_PRINTER_URL, MoonrakerClient
from print_copilot.serialize import to_jsonable


def _preset_library(args) -> PresetLibrary:
    data = locate_data_dir(args.orca_data)
    if (data / "system").is_dir():
        return PresetLibrary(data, vendor=args.vendor)
    install = locate(exe=args.orca_exe, data_dir=data)
    return PresetLibrary(data, vendor=args.vendor, system_dir=install.system_dir)


def _print_json(value: Any) -> None:
    json.dump(to_jsonable(value), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def format_duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def cmd_presets(args: argparse.Namespace) -> int:
    library = _preset_library(args)
    catalog = actions.list_presets(
        library,
        args.kind,
        printer=args.printer,
        include_abstract=args.include_abstract,
        all_vendors=args.all_vendors,
    )
    for problem in catalog.broken:
        print(f"warning: skipped broken preset: {problem}", file=sys.stderr)

    if args.json:
        _print_json(catalog.presets)
        return 0
    for kind, entries in catalog.presets.items():
        print(f"{kind} ({len(entries)})")
        for entry in entries:
            marker = " [user]" if entry.source == "user" else ""
            print(f"  {entry.name}{marker}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    setup = actions.get_current_setup(locate_data_dir(args.orca_data), args.printer)
    if args.json:
        _print_json(setup)
        return 0
    print(f"Current setup in {setup.conf_path}")
    print(f"  printer   : {setup.machine}")
    print(f"  process   : {setup.process}")
    for index, name in enumerate(setup.filaments):
        colour = setup.filament_colors[index] if index < len(setup.filament_colors) else "?"
        print(f"  T{index}        : {name}  ({colour})")
    print(f"  bed type  : {setup.curr_bed_type} (raw value)")
    matrix = setup.flush_volumes_matrix_mm3
    if matrix:
        print("  purge volumes [mm3], row = from, column = to:")
        print("        " + "".join(f"{'T' + str(j):>7}" for j in range(len(matrix))))
        for i, row in enumerate(matrix):
            print(f"    T{i}  " + "".join(f"{v:7.0f}" for v in row))
    others = [m for m in setup.known_machines if m != setup.machine]
    if others:
        print(f"  other printers with a stored selection: {', '.join(others)}")
    return 0


def parse_assignments(items: list[str] | None, *, what: str = "override") -> dict[str, str]:
    """`["key=value", ...]` to a dict, rejecting malformed or repeated keys."""
    result: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise SliceError(f"Invalid {what} {item!r}: expected KEY=VALUE.")
        if key in result:
            raise SliceError(f"The {what} {key!r} is given twice.")
        result[key] = value.strip()
    return result


def _selection(args: argparse.Namespace, library: PresetLibrary) -> actions.Selection:
    selection = actions.select_presets(
        library, printer=args.printer, process=args.process, filaments=args.filament
    )
    for note in selection.notes:
        print(f"note: {note}", file=sys.stderr)
    return selection


def _print_slice(run: SliceRun) -> None:
    r = run.result
    print(f"Model sliced: {run.gcode_path}")
    print(f"  printer   : {run.machine}")
    print(f"  process   : {run.process}")
    for index, name in enumerate(run.filaments):
        grams = r.filament_used_g[index] if index < len(r.filament_used_g) else 0.0
        print(f"  T{index}        : {name}  ({grams:.2f} g)")
    for key, value in run.overrides.items():
        print(f"  override  : {key} = {value}")
    print(f"  time      : {format_duration(r.estimated_time_s)} ({r.estimated_time_s} s)")
    print(f"  filament  : {r.total_filament_g:.2f} g")
    print(f"  layers    : {r.layer_count}")
    for key, value in r.settings.items():
        print(f"  {key} = {value}")
    print(f"  run dir   : {run.run_dir}")


def cmd_slice(args: argparse.Namespace) -> int:
    install = locate(args.orca_exe, args.orca_data)
    library = PresetLibrary(install.data_dir, vendor=args.vendor, system_dir=install.system_dir)
    overrides = parse_assignments(args.set)
    selection = _selection(args, library)
    run = actions.run_slice(
        install,
        args.model,
        selection,
        overrides=overrides,
        work_dir=args.work_dir,
        timeout_s=args.timeout,
    )
    if args.json:
        _print_json(run)
    else:
        _print_slice(run)
    return 0


def _load_variants(args: argparse.Namespace) -> list[actions.Variant]:
    variants = [
        actions.Variant(
            overrides=parse_assignments([part for part in spec.split(";") if part.strip()])
        )
        for spec in args.variant or []
    ]
    if args.variants_file:
        try:
            data = json.loads(Path(args.variants_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SliceError(f"Cannot read variants file {args.variants_file}: {exc}") from exc
        if not isinstance(data, list) or not all(isinstance(v, dict) for v in data):
            raise SliceError(
                'The variants file must be a JSON list of {"label": ..., "overrides": {...}}.'
            )
        variants += [
            actions.Variant(overrides=dict(v.get("overrides") or {}), label=v.get("label"))
            for v in data
        ]
    if not variants:
        raise SliceError("Give at least one --variant or a --variants-file.")
    return variants


def _print_comparison(comparison: actions.Comparison) -> None:
    print(f"Compared {len(comparison.variants)} variants of {comparison.model}")
    print(f"  printer : {comparison.machine}")
    print(f"  process : {comparison.process}")
    heads = len(comparison.filaments)
    header = f"{'variant':<34} {'time':>10} {'d time':>7} {'grams':>7} {'d g':>7} {'layers':>6}"
    print(header + "".join(f"{'T' + str(i) + ' g':>7}" for i in range(heads)))
    for v in comparison.variants:
        print(
            f"{v.label[:34]:<34} {format_duration(v.estimated_time_s):>10} "
            f"{v.time_delta_s:>+7d} {v.total_filament_g:>7.2f} {v.filament_delta_g:>+7.2f} "
            f"{v.layer_count:>6d}" + "".join(f"{g:>7.2f}" for g in v.filament_used_g)
        )
    print("Times and grams are the slicer's own figures from each G-code;")
    print("deltas (d, in s and g) are against the first variant. Run dirs:")
    for v in comparison.variants:
        print(f"  {v.label}: {v.run_dir}")


def cmd_compare(args: argparse.Namespace) -> int:
    install = locate(args.orca_exe, args.orca_data)
    library = PresetLibrary(install.data_dir, vendor=args.vendor, system_dir=install.system_dir)
    variants = _load_variants(args)
    selection = _selection(args, library)
    comparison = actions.compare_variants(
        install, args.model, selection, variants, work_dir=args.work_dir, timeout_s=args.timeout
    )
    if args.json:
        _print_json(comparison)
    else:
        _print_comparison(comparison)
    return 0


def parse_regions(items: list[str] | None) -> list[Region] | None:
    """`--protect` values: JSON objects with label, direction, angle_deg,
    box_min_mm, box_max_mm (model coordinates as loaded, mm)."""
    regions = []
    for item in items or []:
        try:
            data = json.loads(item)
        except ValueError as exc:
            raise ModelError(f"--protect {item!r} is not valid JSON: {exc}") from exc
        allowed = {"label", "direction", "angle_deg", "box_min_mm", "box_max_mm"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ModelError(
                f"--protect {item!r} must be a JSON object with keys from {sorted(allowed)}."
            )
        region = Region(**data)
        try:
            region.validate()
        except ValueError as exc:
            raise ModelError(str(exc)) from exc
        regions.append(region)
    return regions or None


def parse_vector(text: str | None) -> list[float] | None:
    if text is None:
        return None
    try:
        values = [float(v) for v in text.split(",")]
    except ValueError as exc:
        raise ModelError(f"Expected three comma-separated numbers, got {text!r}.") from exc
    if len(values) != 3:
        raise ModelError(f"Expected three comma-separated numbers, got {text!r}.")
    return values


def _area(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _print_analysis(analysis: OrientationAnalysis) -> None:
    sx, sy, sz = analysis.size_mm
    print(f"Model: {analysis.model}")
    print(
        f"  {analysis.face_count} triangles, {analysis.object_count} object(s), "
        f"{analysis.volume_mm3:.0f} mm3, {sx:g} x {sy:g} x {sz:g} mm as loaded"
    )
    print(f"  support threshold: {analysis.threshold_deg:g} degrees, goal: {analysis.goal}")
    protected = analysis.protected_area_mm2 is not None
    if protected:
        names = ", ".join(r.label for r in analysis.regions)
        print(f"  protected: {names} ({analysis.protected_area_mm2:.1f} mm2)")
    header = (
        f"{'#':>2} {'orientation':<26} {'height':>7} {'contact':>9} {'overhang':>9} "
        f"{'support':>10} {'stairs':>8}"
    )
    units = f"{'':>2} {'':<26} {'mm':>7} {'mm2':>9} {'mm2':>9} {'mm3':>10} {'mm2':>8}"
    if protected:
        header += f" {'P supp':>8} {'P bed':>8} {'P stair':>8}"
        units += f" {'mm2':>8} {'mm2':>8} {'mm2':>8}"
    quality = analysis.goal == "quality"
    if quality:
        header += f" {'cost':>8}"
        units += f" {'mm2':>8}"
    print(header + f" {'fits':>5}")
    print(units)
    for c in analysis.candidates:
        fits = "?" if c.fits_bed is None else ("yes" if c.fits_bed else "NO")
        if not c.stable:
            fits += "*"
        line = (
            f"{c.rank:>2} {c.label[:26]:<26} {c.height_mm:>7.1f} {c.bed_contact_area_mm2:>9.1f} "
            f"{c.overhang_area_mm2:>9.1f} {c.support_volume_mm3:>10.0f} {c.stair_area_mm2:>8.1f}"
        )
        if protected:
            line += (
                f" {_area(c.protected_overhang_area_mm2):>8}"
                f" {_area(c.protected_bed_contact_area_mm2):>8}"
                f" {_area(c.protected_stair_area_mm2):>8}"
            )
        if quality:
            line += f" {c.quality_cost:>8.0f}"
        print(line + f" {fits:>5}")
    if not all(c.stable for c in analysis.candidates):
        print("* stands on an edge or a point (bed contact under 5% of its footprint)")
    print(f"Ranking: {analysis.ranking}.")
    print(f"Heuristic: {analysis.heuristic}.")
    for note in analysis.notes:
        print(f"note: {note}")


def cmd_analyze(args: argparse.Namespace) -> int:
    selection = None
    if not args.no_presets:
        library = _preset_library(args)
        selection = _selection(args, library)
    analysis = actions.analyze_model(
        args.model,
        selection=selection,
        threshold_deg=args.threshold,
        regions=parse_regions(args.protect),
        goal=args.goal,
    )
    if args.json:
        _print_json(analysis)
    else:
        _print_analysis(analysis)
    return 0


def _print_orientation_report(report: actions.OrientationReport) -> None:
    _print_analysis(report.analysis)
    print()
    print(f"Sliced with {report.process} on {report.machine}:")
    print(
        f"{'#':>2} {'orientation':<22} {'support':<7} {'time':>10} {'grams':>7} "
        f"{'supp g':>7} {'iface mm':>8} {'bridge':>7} {'layers':>6}"
    )
    for s in report.slices:
        mark = " <- recommended" if s is report.recommended else ""
        print(
            f"{s.rank:>2} {s.candidate[:22]:<22} {s.support_mode:<7} "
            f"{format_duration(s.estimated_time_s):>10} {s.total_filament_g:>7.2f} "
            f"{s.support_filament_g:>7.2f} {s.support_interface_mm:>8.1f} "
            f"{s.bridge_mm:>7.1f} {s.layer_count:>6d}{mark}"
        )
    print("Times and grams are the slicer's own figures; support grams, support interface")
    print("and bridge lengths (mm of filament) are measured from the G-code toolpaths.")
    print(f"Recommendation rule ({report.goal}): {report.recommendation_rule}.")
    down = ", ".join(f"{v:g}" for v in report.recommended.down_vector)
    print(f"Recommended: {report.recommended.candidate} (down vector {down})")
    print(f"Recommended model file: {report.recommended.model_path}")
    for note in report.notes:
        print(f"note: {note}")
    print(f"Work dir: {report.work_dir}")


def cmd_orient(args: argparse.Namespace) -> int:
    install = locate(args.orca_exe, args.orca_data)
    library = PresetLibrary(install.data_dir, vendor=args.vendor, system_dir=install.system_dir)
    regions = parse_regions(args.protect)
    selection = _selection(args, library)
    report = actions.optimize_orientation(
        install,
        args.model,
        selection,
        top=args.top,
        support_modes=args.support or list(actions.DEFAULT_SUPPORT_MODES),
        goal=args.goal,
        regions=regions,
        threshold_deg=args.threshold,
        work_dir=args.work_dir,
        timeout_s=args.timeout,
    )
    if args.json:
        _print_json(report)
    else:
        _print_orientation_report(report)
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    selection = None
    if not args.no_presets:
        library = _preset_library(args)
        selection = _selection(args, library)
    result = actions.render_model(
        args.model,
        down_vector=parse_vector(args.down),
        regions=parse_regions(args.protect),
        threshold_deg=args.threshold,
        selection=selection,
        views=args.view or actions.DEFAULT_VIEWS,
        highlight=not args.no_highlight,
        out_path=args.out,
    )
    if args.json:
        _print_json(result)
        return 0
    print(f"Rendered {result.width_px} x {result.height_px} px: {result.png_path}")
    print(f"  coordinates: {result.frame}")
    for name, colour in result.legend.items():
        print(f"  {colour}  {name.lower()}")
    for name, area in result.face_areas_mm2.items():
        print(f"  {name.replace('_', ' ')}: {area:.1f} mm2")
    for note in result.notes:
        print(f"note: {note}")
    return 0


def parse_mapping(items: list[str] | None) -> dict[int, int] | None:
    """`["5=T1", "6=2"]` (project filament = tool head) to {5: 1, 6: 2}."""
    result: dict[int, int] = {}
    for key, value in parse_assignments(items, what="mapping").items():
        head = value.upper().removeprefix("T")
        if not key.isdigit() or not head.isdigit():
            raise SliceError(
                f"Invalid mapping {key}={value}: expected FILAMENT=T<head>, e.g. 5=T1."
            )
        result[int(key)] = int(head)
    return result or None


def parse_head(value: str | None) -> int | None:
    """`T1` or `1` to 1."""
    if value is None:
        return None
    head = value.upper().removeprefix("T")
    if not head.isdigit():
        raise SliceError(f"Invalid head {value!r}: expected T<n>, e.g. T1.")
    return int(head)


def _project_selection(args: argparse.Namespace) -> tuple[Any, actions.Selection]:
    install = locate(args.orca_exe, args.orca_data)
    library = PresetLibrary(install.data_dir, vendor=args.vendor, system_dir=install.system_dir)
    selection = _selection(args, library)
    if not getattr(args, "no_printer", True):
        shown = len(selection.notes)
        selection = multicolor.with_printer_spools(selection)
        for note in selection.notes[shown:]:
            print(f"note: {note}", file=sys.stderr)
    return install, selection


def _print_assignment(assignment: Any) -> None:
    for c in assignment.choices:
        distance = "" if c.delta_e is None else f", delta E {c.delta_e:g}"
        how = "given" if c.explicit else "chosen"
        wanted = " ".join(filter(None, [c.project_colour or "?", c.project_type]))
        print(
            f"    filament {c.filament} ({wanted}) -> "
            f"T{c.head} {c.head_filament} ({c.head_colour or '?'}{distance}) [{how}]"
        )


def cmd_project(args: argparse.Namespace) -> int:
    selection = None
    if not args.no_presets:
        _, selection = _project_selection(args)
    summary = multicolor.inspect_project(args.model, selection, reduce_colours=args.reduce_colours)
    if args.json:
        _print_json(summary)
        return 0
    print(f"Project {summary.project}")
    print(f"  saved by : {summary.application}")
    print(f"  printer  : {summary.printer}")
    for head in summary.heads:
        print(f"  T{head.index}       : {head.filament} ({head.colour or '?'}, {head.type})")
    for plate in summary.plates:
        name = f" '{plate.name}'" if plate.name else ""
        print(f"  plate {plate.number}{name}: {len(plate.objects)} object(s)")
        used = ", ".join(f"{w.number} {w.colour or '?'} {w.type or ''}" for w in plate.filaments)
        print(f"    filaments: {used}")
        if plate.assignment:
            _print_assignment(plate.assignment)
        if plate.assignment_error:
            print(f"    no assignment: {plate.assignment_error}")
    for note in summary.notes:
        print(f"note: {note}")
    return 0


def _print_project_slice(r: Any) -> None:
    print(f"Plate {r.plate} of {r.project} sliced: {r.gcode_path}")
    print(f"  printer   : {r.machine}")
    print(f"  process   : {r.process}")
    _print_assignment(r.assignment)
    print(f"  time      : {format_duration(r.estimated_time_s)} ({r.estimated_time_s} s)")
    print(f"  filament  : {r.total_filament_g:.2f} g")
    print(f"  changes   : {r.tool_changes} tool changes on {r.layers_with_changes} layers")
    print(f"  tower     : {r.prime_tower_g:.2f} g prime tower, {r.purge_g:.2f} g of it purge")
    for h in r.heads:
        print(
            f"  T{h.head}        : {h.total_g:.2f} g ({h.filament}, {h.colour}), "
            f"tower {h.prime_tower_g:.2f} g, filaments {h.project_filaments}"
        )
    for note in r.notes:
        print(f"note: {note}")


def cmd_slice_project(args: argparse.Namespace) -> int:
    install, selection = _project_selection(args)
    result = multicolor.slice_project(
        install,
        args.model,
        selection,
        plate=args.plate,
        mapping=parse_mapping(args.map),
        reduce_colours=args.reduce_colours,
        overrides=parse_assignments(args.set),
        work_dir=args.work_dir,
        timeout_s=args.timeout,
        head=parse_head(args.head),
    )
    if args.json:
        _print_json(result)
    else:
        _print_project_slice(result)
    return 0


def cmd_strategies(args: argparse.Namespace) -> int:
    install, selection = _project_selection(args)
    variants = _load_variants(args) if (args.variant or args.variants_file) else None
    comparison = multicolor.compare_strategies(
        install,
        args.model,
        selection,
        plate=args.plate,
        mapping=parse_mapping(args.map),
        reduce_colours=args.reduce_colours,
        variants=variants,
        work_dir=args.work_dir,
        timeout_s=args.timeout,
        head=parse_head(args.head),
    )
    if args.json:
        _print_json(comparison)
        return 0
    print(f"Plate {comparison.plate} of {comparison.project}")
    _print_assignment(comparison.assignment)
    print(
        f"{'strategy':<26} {'time':>10} {'d time':>8} {'grams':>8} {'d g':>8} "
        f"{'changes':>8} {'tower g':>8}"
    )
    for v in comparison.variants:
        if v.slice is None:
            reason = (v.error or "").strip().splitlines()
            print(f"{v.label[:26]:<26} failed: {reason[-1][:200] if reason else ''}")
            continue
        r = v.slice
        print(
            f"{v.label[:26]:<26} {format_duration(r.estimated_time_s):>10} {v.time_delta_s:>+8d} "
            f"{r.total_filament_g:>8.2f} {v.filament_delta_g:>+8.2f} {r.tool_changes:>8d} "
            f"{r.prime_tower_g:>8.2f}"
        )
    print(f"fastest: {comparison.fastest}; least filament: {comparison.least_filament}")
    print("All numbers are the slicer's own figures from each G-code.")
    for v in comparison.variants:
        if v.error:
            print(f"failed {v.label}: {v.error}")
    for note in comparison.notes:
        print(f"note: {note}")
    return 0


def cmd_export_project(args: argparse.Namespace) -> int:
    install, selection = _project_selection(args)
    exported = multicolor.export_project(
        install,
        args.model,
        selection,
        args.out,
        plate=args.plate,
        mapping=parse_mapping(args.map),
        reduce_colours=args.reduce_colours,
        overrides=parse_assignments(args.set),
        work_dir=args.work_dir,
        head=parse_head(args.head),
        slice_gcode=not args.no_slice,
        timeout_s=args.timeout,
    )
    opened = multicolor.open_in_app(install, exported.path) if args.open else None
    if args.json:
        data = to_jsonable(exported)
        data["opened_with"] = opened
        _print_json(data)
        return 0
    print(f"Project written: {exported.path}")
    print(f"  plate {exported.plate} of {exported.source}")
    print(f"  printer   : {exported.machine}")
    print(f"  process   : {exported.process}")
    _print_assignment(exported.assignment)
    if exported.slice and exported.gcode_path:
        r = exported.slice
        print(f"G-code written: {exported.gcode_path}")
        print(f"  time      : {format_duration(r.estimated_time_s)} ({r.estimated_time_s} s)")
        print(f"  filament  : {r.total_filament_g:.2f} g")
        print(f"  changes   : {r.tool_changes} tool changes on {r.layers_with_changes} layers")
    for note in exported.notes:
        print(f"note: {note}")
    if exported.gcode_path:
        print(f"To reopen what this G-code prints: {exported.path}")
    if opened:
        print("Opened in Snapmaker Orca.")
    return 0


def cmd_quality(args: argparse.Namespace) -> int:
    install = selection = None
    if Path(args.model).suffix.lower() != ".gcode":
        install, selection = _project_selection(args)
    elif args.printer or args.process or args.filament:
        raise quality.QualityError(
            "Existing G-code uses its own effective config; omit preset choices."
        )
    variants = _load_variants(args) if args.variant or args.variants_file else None
    report = quality.review_print(
        args.model,
        install=install,
        selection=selection,
        plate=args.plate,
        mapping=parse_mapping(args.map),
        reduce_colours=args.reduce_colours,
        head=parse_head(args.head),
        variants=variants,
        compare=not args.no_compare,
        work_dir=args.work_dir,
        timeout_s=args.timeout,
        max_segments=args.max_segments,
    )
    if args.json:
        _print_json(report)
    else:
        print(
            f"Quality review: {report.status}\nReport: {report.report_path}\n"
            f"Open: {report.html_path}"
        )
        print(f"Candidate for visual review: {report.recommendation}")
        for variant in report.variants:
            print(f"\n{variant.label}: {variant.time_s} s, {variant.filament_g} g (slicer)")
            if variant.error:
                print(f"  FAILED: {variant.error}")
            elif variant.audit:
                for finding in variant.audit.findings:
                    print(f"  {finding.severity.upper()} [{finding.code}]: {finding.title}")
        print("Visual review is pending. Physical finish and calibration remain unverified.")
    return 1 if report.status == "blocked" else 0


def cmd_preview(args: argparse.Namespace) -> int:
    preview = quality.preview_gcode(
        args.gcode, layers=args.layer, work_dir=args.work_dir, max_segments=args.max_segments
    )
    if args.json:
        _print_json(preview)
    else:
        print(
            f"Selected layers: {preview.layers_shown}\nImage: {preview.png_path}\n"
            f"All layers: {preview.html_path}"
        )
    return 0


def cmd_quality_review(args: argparse.Namespace) -> int:
    receipt = quality.record_quality_review(
        args.report,
        variant=args.variant,
        decision=args.decision,
        observations=args.observations,
        accepted_findings=args.accept_finding,
    )
    if args.json:
        _print_json(receipt)
    else:
        print(f"{receipt.status}: {receipt.variant}\nHardware quality: {receipt.hardware_quality}")
    return 0


def _client(args: argparse.Namespace) -> MoonrakerClient:
    return MoonrakerClient(args.printer_url)


def cmd_printer_discover(args: argparse.Namespace) -> int:
    found = discovery.discover(args.timeout)
    if args.json:
        _print_json([{**to_jsonable(p), "url": p.url} for p in found])
        return 0
    for p in found:
        print(f"{p.device_name}: {p.machine_type}, firmware {p.firmware_version}, {p.url}")
    if not found:
        print("No Snapmaker printer answered on the network.")
    return 0 if found else 1


def cmd_printer_status(args: argparse.Namespace) -> int:
    status = printer.printer_status(_client(args))
    if args.json:
        _print_json(status)
        return 0
    print(f"{status.machine_type} at {status.base_url}, firmware {status.firmware_version}")
    print(f"Klipper: {status.klippy_state} ({status.state_message})")
    print(
        f"Print: {status.print_state}"
        + (f" {status.print_filename}" if status.print_filename else "")
    )
    if status.print_state in ("printing", "paused"):
        print(f"  progress {status.progress:.0%}, {format_duration(int(status.print_duration_s))}")
    print(f"Bed: {status.bed_temperature_c} / {status.bed_target_c} C")
    for head in status.heads:
        spool = head.rfid_filament
        loaded = (
            f"{spool.manufacturer} {spool.type} {spool.sub_type} {spool.colour}"
            if spool
            else "no RFID spool read"
        )
        active = " (active)" if head.object_name == status.active_head else ""
        print(
            f"T{head.index}{active}: {head.temperature_c} / {head.target_c} C, "
            f"{head.nozzle_diameter_mm} mm nozzle, {loaded}"
        )
    print(f"Ready to start a print: {'yes' if status.ready_to_start else 'no'}")
    return 0


def cmd_printer_files(args: argparse.Namespace) -> int:
    files = printer.list_files(_client(args))
    if args.json:
        _print_json(files)
        return 0
    for f in files:
        print(f"{f.name}  ({f.size_bytes / 1e6:.1f} MB)")
    if not files:
        print("No G-code files on the printer.")
    return 0


def cmd_printer_upload(args: argparse.Namespace) -> int:
    result = printer.upload_gcode(_client(args), args.gcode, args.name, overwrite=args.overwrite)
    if args.json:
        _print_json(result)
    else:
        print(f"Uploaded {result.name} ({result.size_bytes} bytes). Not started.")
    return 0


def _print_plan(plan: printer.StartPlan) -> None:
    print(f"File: {plan.filename} on {plan.base_url}")
    print(f"Would send: {plan.request}")
    if plan.estimated_time_s is not None:
        print(
            f"Slicer estimate stored in the file: {format_duration(plan.estimated_time_s)}, "
            f"{plan.filament_total_g} g"
        )
    for head in plan.heads:
        print(
            f"  T{head.index}: needs {head.filament_type} {head.colour or ''} "
            f"({head.weight_g} g); RFID spool: {head.loaded_type or 'none read'} "
            f"{head.loaded_colour or ''}"
        )
    for check in plan.checks:
        print(f"  {check.level.upper()}: {check.message}")


def cmd_printer_start(args: argparse.Namespace) -> int:
    client = _client(args)
    plan = printer.plan_start(client, args.filename)
    _print_plan(plan)
    if plan.blocked:
        print("Blocked: fix the errors above. Nothing was sent.", file=sys.stderr)
        return 1
    if args.dry_run:
        print("Dry run: nothing was sent.")
        return 0
    printer.take_plan(plan.token)
    if args.yes:
        confirmed_by = "--yes on the command line"
    else:
        if not sys.stdin.isatty():
            raise ConfirmationError("No terminal to confirm in. Pass --yes to confirm explicitly.")
        answer = input("The printer will heat and move. Type 'start' to send: ")
        if answer.strip().lower() != "start":
            print("Not confirmed. Nothing was sent.")
            return 1
        confirmed_by = "typed confirmation"
    sent = printer.execute_start(client, plan, confirmed_by=confirmed_by)
    print(f"Sent: {sent.request}")
    return 0


def cmd_printer_cancel(args: argparse.Namespace) -> int:
    sent = printer.cancel_print(_client(args))
    print(f"Sent: {sent.request}")
    return 0


def cmd_printer_heaters_off(args: argparse.Namespace) -> int:
    sent = printer.heaters_off(_client(args))
    print(f"Sent: {sent.request}")
    return 0


def cmd_printer_fake(args: argparse.Namespace) -> int:
    fake = FakeMoonraker(port=args.port)
    print(f"Fake U1 Moonraker at {fake.url} (Ctrl+C to stop). Nothing reaches a printer.")
    print(f"Use it with: --printer-url {fake.url}")
    with contextlib.suppress(KeyboardInterrupt):
        fake.serve_forever()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="print-copilot",
        description="Prepare Snapmaker U1 prints with the Snapmaker Orca slicer.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--orca-data", help="Snapmaker Orca data dir (default %%APPDATA%%)")
    common.add_argument("--orca-exe", help="path to snapmaker-orca.exe")
    common.add_argument(
        "--vendor",
        default=DEFAULT_VENDOR,
        help=f"system preset vendor folder (default {DEFAULT_VENDOR!r})",
    )
    common.add_argument("--json", action="store_true", help="print JSON instead of text")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("presets", parents=[common], help="list Snapmaker Orca presets")
    p.add_argument("--kind", choices=KINDS, help="only this preset kind")
    p.add_argument(
        "--printer",
        help="only process/filament presets compatible with this machine preset",
    )
    p.add_argument(
        "--include-abstract",
        action="store_true",
        help="also list base presets that the app hides (instantiation = false)",
    )
    p.add_argument(
        "--all-vendors", action="store_true", help="include presets from every vendor folder"
    )
    p.set_defaults(func=cmd_presets)

    st = sub.add_parser(
        "setup",
        parents=[common],
        help="show the printer, presets and colours selected in Snapmaker Orca",
    )
    st.add_argument(
        "--printer", help="show the stored selection of this machine instead of the current one"
    )
    st.set_defaults(func=cmd_setup)

    slicing = argparse.ArgumentParser(add_help=False)
    slicing.add_argument("model", help="STL/3MF model to slice")
    slicing.add_argument(
        "--printer", help="machine preset name (default: the one selected in Snapmaker Orca)"
    )
    slicing.add_argument(
        "--process",
        help="process preset name (default: the one stored in Snapmaker Orca for the printer)",
    )
    slicing.add_argument(
        "--filament",
        action="append",
        help="filament preset per tool head, in order T0, T1, ... (give one to use it on "
        "every head; default: the ones stored in Snapmaker Orca for the printer)",
    )
    running = argparse.ArgumentParser(add_help=False)
    running.add_argument("--work-dir", help="parent folder for run directories")
    running.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="slicer timeout in seconds"
    )

    s = sub.add_parser("slice", parents=[common, slicing, running], help="slice a model headlessly")
    s.add_argument(
        "--set",
        action="append",
        metavar="KEY=VALUE",
        help="override a setting for this run: a process key, or machine.KEY / filament.KEY "
        "(filament overrides apply to every head); repeatable",
    )
    s.set_defaults(func=cmd_slice)

    c = sub.add_parser(
        "compare",
        parents=[common, slicing, running],
        help="slice several override sets of one model and compare the measured results",
    )
    c.add_argument(
        "--variant",
        action="append",
        metavar="KEY=VALUE;...",
        help="one variant: overrides separated by ';' (same keys as slice --set); "
        "an empty string slices the presets as they are; repeatable",
    )
    c.add_argument(
        "--variants-file",
        help='JSON list of {"label": "...", "overrides": {"KEY": VALUE, ...}}',
    )
    c.set_defaults(func=cmd_compare)

    threshold = argparse.ArgumentParser(add_help=False)
    threshold.add_argument(
        "--threshold",
        type=float,
        help="support threshold angle in degrees from horizontal "
        "(default: support_threshold_angle of the process preset)",
    )
    threshold.add_argument(
        "--protect",
        action="append",
        metavar="JSON",
        help='surface to keep clean, e.g. \'{"label": "face", "direction": [0, -1, 0], '
        '"box_min_mm": [-8, -20, 30], "box_max_mm": [8, 0, 45]}\' (model coordinates as '
        "loaded); repeatable",
    )

    goal = argparse.ArgumentParser(add_help=False)
    goal.add_argument(
        "--goal",
        choices=list(GOALS),
        default="speed",
        help="what the ranking optimises (default speed; quality keeps protected surfaces clean)",
    )

    a = sub.add_parser(
        "analyze",
        parents=[common, slicing, threshold, goal],
        help="rank candidate orientations with the overhang heuristic (no slicing)",
    )
    a.add_argument(
        "--no-presets",
        action="store_true",
        help="do not read Snapmaker Orca presets (no bed-fit check, threshold 30 degrees)",
    )
    a.set_defaults(func=cmd_analyze)

    o = sub.add_parser(
        "orient",
        parents=[common, slicing, running, threshold, goal],
        help="slice the best candidate orientations with each support mode and recommend one",
    )
    o.add_argument(
        "--top",
        type=int,
        default=actions.DEFAULT_TOP_CANDIDATES,
        help=f"how many candidates to slice (default {actions.DEFAULT_TOP_CANDIDATES})",
    )
    o.add_argument(
        "--support",
        action="append",
        choices=list(actions.SUPPORT_MODES),
        help="support mode to slice each candidate with; repeatable "
        f"(default: {' and '.join(actions.DEFAULT_SUPPORT_MODES)})",
    )
    o.set_defaults(func=cmd_orient)

    r = sub.add_parser(
        "render",
        parents=[common, slicing, threshold],
        help="render labelled views of a model to a PNG, highlighting supports and "
        "protected surfaces",
    )
    r.add_argument(
        "--down",
        metavar="X,Y,Z",
        help="show the print orientation in which this model direction faces the bed",
    )
    r.add_argument(
        "--view",
        action="append",
        choices=list(VIEWS),
        help=f"view to render; repeatable (default: {', '.join(actions.DEFAULT_VIEWS)})",
    )
    r.add_argument("--no-highlight", action="store_true", help="plain grey model")
    r.add_argument("--out", help="PNG path (default: a new file under the work dir)")
    r.add_argument(
        "--no-presets",
        action="store_true",
        help="do not read Snapmaker Orca presets (support threshold 30 degrees)",
    )
    r.set_defaults(func=cmd_render)

    project_opts = argparse.ArgumentParser(add_help=False)
    project_opts.add_argument(
        "--plate", type=int, help="plate number (1-based; needed when the project has several)"
    )
    project_opts.add_argument(
        "--map",
        action="append",
        metavar="FILAMENT=T<head>",
        help="put a project filament on a tool head, e.g. 5=T1; several filaments may share a "
        "head; repeatable (default: nearest loaded colour)",
    )
    project_opts.add_argument(
        "--reduce-colours",
        action="store_true",
        help="when a plate uses more filaments than heads, merge the nearest colours (heuristic)",
    )
    project_opts.add_argument(
        "--head",
        metavar="T<n>",
        help="print the whole plate (or the STL) with this tool head; required for a "
        "one-colour plate",
    )
    project_opts.add_argument(
        "--no-printer",
        action="store_true",
        help="do not read the loaded colours from the U1's RFID readers (use the app's selection)",
    )

    pj = sub.add_parser(
        "project",
        parents=[common, slicing, project_opts],
        help="show plates, objects and filaments of a 3MF project and the proposed tool heads",
    )
    pj.add_argument(
        "--no-presets", action="store_true", help="do not read Snapmaker Orca (no head proposal)"
    )
    pj.set_defaults(func=cmd_project)

    sp = sub.add_parser(
        "slice-project",
        parents=[common, slicing, running, project_opts],
        help="slice one plate of a 3MF project on the U1 and measure tool changes",
    )
    sp.add_argument("--set", action="append", metavar="KEY=VALUE", help="as for slice")
    sp.set_defaults(func=cmd_slice_project)

    sg = sub.add_parser(
        "strategies",
        parents=[common, slicing, running, project_opts],
        help="compare prime tower / print order strategies for one plate of a 3MF project",
    )
    sg.add_argument(
        "--variant",
        action="append",
        metavar="KEY=VALUE;...",
        help="custom strategy instead of the default set (as for compare); repeatable",
    )
    sg.add_argument("--variants-file", help="as for compare")
    sg.set_defaults(func=cmd_strategies)

    ep = sub.add_parser(
        "export-project",
        parents=[common, slicing, running, project_opts],
        help="write one plate as a new 3MF project for Snapmaker Orca with the U1 presets",
    )
    ep.add_argument("--out", required=True, help="new .3mf file to write (never overwritten)")
    ep.add_argument("--set", action="append", metavar="KEY=VALUE", help="as for slice")
    ep.add_argument("--open", action="store_true", help="open the written project in the app")
    ep.add_argument(
        "--no-slice",
        action="store_true",
        help="only write the project (default: also slice it into a .gcode next to it)",
    )
    ep.set_defaults(func=cmd_export_project)

    q = sub.add_parser(
        "quality",
        parents=[common, slicing, running, project_opts],
        help="audit effective settings, slice quality candidates and create visual evidence",
    )
    q.add_argument("--no-compare", action="store_true", help="audit only the baseline")
    q.add_argument("--variant", action="append", help="custom comparison overrides, as for compare")
    q.add_argument("--variants-file", help="custom variants JSON, as for compare")
    q.add_argument(
        "--max-segments", type=int, default=1_000_000, help="explicit preview resource limit"
    )
    q.set_defaults(func=cmd_quality)

    pv = sub.add_parser("preview", parents=[common], help="preview actual G-code layers offline")
    pv.add_argument("gcode", help="sliced G-code file")
    pv.add_argument("--layer", type=int, action="append", help="layer number (1-based); up to six")
    pv.add_argument("--work-dir", help="parent folder for preview artifacts")
    pv.add_argument("--max-segments", type=int, default=1_000_000)
    pv.set_defaults(func=cmd_preview)

    qr = sub.add_parser("quality-review", parents=[common], help="record an explicit visual review")
    qr.add_argument("report", help="quality report.json to review")
    qr.add_argument("--variant", required=True, help="exact variant label from the report")
    qr.add_argument("--decision", required=True, choices=["accept_with_limitations", "revise"])
    qr.add_argument(
        "--observations", required=True, help="what you inspected and observed visually"
    )
    qr.add_argument(
        "--accept-finding", action="append", help="acknowledge a warning/unverified finding code"
    )
    qr.set_defaults(func=cmd_quality_review)

    pr = sub.add_parser("printer", help="talk to the U1 through Moonraker")
    psub = pr.add_subparsers(dest="printer_command", required=True)
    net = argparse.ArgumentParser(add_help=False)
    net.add_argument(
        "--printer-url",
        help=f"printer address (or {ENV_PRINTER_URL}); default: find the U1 through mDNS",
    )
    net.add_argument("--json", action="store_true", help="print JSON instead of text")
    pd = psub.add_parser("discover", help="list Snapmaker printers answering on the network")
    pd.add_argument("--timeout", type=float, default=discovery.DEFAULT_TIMEOUT_S, help="seconds")
    pd.add_argument("--json", action="store_true", help="print JSON instead of text")
    pd.set_defaults(func=cmd_printer_discover)
    ps = psub.add_parser("status", parents=[net], help="state, temperatures, heads and spools")
    ps.set_defaults(func=cmd_printer_status)
    pf = psub.add_parser("files", parents=[net], help="G-code files stored on the printer")
    pf.set_defaults(func=cmd_printer_files)
    pu = psub.add_parser("upload", parents=[net], help="copy a G-code file; never starts it")
    pu.add_argument("gcode", help="G-code sliced for the Snapmaker U1")
    pu.add_argument("--name", help="file name on the printer (default: local name)")
    pu.add_argument("--overwrite", action="store_true", help="replace a file of the same name")
    pu.set_defaults(func=cmd_printer_upload)
    pst = psub.add_parser(
        "start", parents=[net], help="start a stored file after showing the plan and confirming"
    )
    pst.add_argument("filename", help="file name on the printer")
    pst.add_argument("--dry-run", action="store_true", help="show the plan only")
    pst.add_argument("--yes", action="store_true", help="confirm without the typed prompt")
    pst.set_defaults(func=cmd_printer_start)
    pc = psub.add_parser("cancel", parents=[net], help="cancel the running print (not gated)")
    pc.set_defaults(func=cmd_printer_cancel)
    ph = psub.add_parser("heaters-off", parents=[net], help="turn every heater off (not gated)")
    ph.set_defaults(func=cmd_printer_heaters_off)
    pk = psub.add_parser("fake", help="run a fake U1 Moonraker on this computer")
    pk.add_argument("--port", type=int, default=7125, help="port on 127.0.0.1 (default 7125)")
    pk.set_defaults(func=cmd_printer_fake)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Preset names contain characters such as "@" and the console may not be UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PrintCopilotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
