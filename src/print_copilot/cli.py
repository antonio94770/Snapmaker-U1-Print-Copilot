"""`print-copilot` command line. Same features as the MCP tools, no AI needed."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from print_copilot import __version__, actions
from print_copilot.errors import PrintCopilotError, SliceError
from print_copilot.orca.install import locate, locate_data_dir
from print_copilot.orca.presets import DEFAULT_VENDOR, KINDS, PresetLibrary
from print_copilot.orca.slicer import DEFAULT_TIMEOUT_S, SliceRun
from print_copilot.serialize import to_jsonable


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
    library = PresetLibrary(locate_data_dir(args.orca_data), vendor=args.vendor)
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
    library = PresetLibrary(install.data_dir, vendor=args.vendor)
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
    library = PresetLibrary(install.data_dir, vendor=args.vendor)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="print-copilot",
        description="Prepare Snapmaker U1 prints with the Snapmaker Orca slicer.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--orca-data", help="Snapmaker Orca data dir (default %%APPDATA%%)")
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
    slicing.add_argument("--orca-exe", help="path to snapmaker-orca.exe")
    slicing.add_argument("--work-dir", help="parent folder for run directories")
    slicing.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="slicer timeout in seconds"
    )

    s = sub.add_parser("slice", parents=[common, slicing], help="slice a model headlessly")
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
        parents=[common, slicing],
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
