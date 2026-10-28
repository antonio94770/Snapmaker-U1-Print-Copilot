"""`print-copilot` command line. Same features as the MCP tools, no AI needed."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from print_copilot import __version__
from print_copilot.errors import PrintCopilotError
from print_copilot.orca.install import locate, locate_data_dir
from print_copilot.orca.presets import DEFAULT_VENDOR, KINDS, PresetLibrary
from print_copilot.orca.slicer import DEFAULT_TIMEOUT_S, SliceRun, slice_model, tool_count
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
    kinds = [args.kind] if args.kind else list(KINDS)
    listing: dict[str, list[dict[str, Any]]] = {}
    for kind in kinds:
        found = library.list_presets(
            kind, printer=args.printer, include_abstract=args.include_abstract
        )
        presets = found.presets
        for problem in found.broken:
            print(f"warning: skipped broken preset: {problem}", file=sys.stderr)
        # A --printer filter already narrows process/filament presets to the
        # compatible ones, which may live in other vendor folders.
        if not args.all_vendors and not (args.printer and kind != "machine"):
            presets = [p for p in presets if p.ref.source == "user" or p.ref.vendor == args.vendor]
        listing[kind] = [
            {
                "name": p.name,
                "source": p.ref.source,
                "vendor": p.ref.vendor,
                "inherits": p.ref.inherits,
                "path": str(p.ref.path),
            }
            for p in presets
        ]

    if args.json:
        _print_json(listing)
        return 0
    for kind, entries in listing.items():
        print(f"{kind} ({len(entries)})")
        for entry in entries:
            marker = " [user]" if entry["source"] == "user" else ""
            print(f"  {entry['name']}{marker}")
    return 0


def _print_slice(run: SliceRun) -> None:
    r = run.result
    print(f"Model sliced: {run.gcode_path}")
    print(f"  printer   : {run.machine}")
    print(f"  process   : {run.process}")
    for index, name in enumerate(run.filaments):
        grams = r.filament_used_g[index] if index < len(r.filament_used_g) else 0.0
        print(f"  T{index}        : {name}  ({grams:.2f} g)")
    print(f"  time      : {format_duration(r.estimated_time_s)} ({r.estimated_time_s} s)")
    print(f"  filament  : {r.total_filament_g:.2f} g")
    print(f"  layers    : {r.layer_count}")
    for key, value in r.settings.items():
        print(f"  {key} = {value}")
    print(f"  run dir   : {run.run_dir}")


def cmd_slice(args: argparse.Namespace) -> int:
    install = locate(args.orca_exe, args.orca_data)
    library = PresetLibrary(install.data_dir, vendor=args.vendor)
    machine = library.resolve("machine", args.printer)
    process = library.resolve("process", args.process)
    filaments = [library.resolve("filament", name) for name in args.filament]

    heads = tool_count(machine)
    if len(filaments) == 1 and heads > 1:
        # One filament given: load it on every head. The slicer needs one
        # filament per head (HANDOFF.md section 4, gotcha 7).
        filaments = filaments * heads
        print(f"note: using {args.filament[0]!r} on all {heads} tool heads", file=sys.stderr)

    run = slice_model(
        install,
        args.model,
        machine,
        process,
        filaments,
        work_dir=args.work_dir,
        timeout_s=args.timeout,
    )
    if args.json:
        _print_json(run)
    else:
        _print_slice(run)
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

    s = sub.add_parser("slice", parents=[common], help="slice a model headlessly")
    s.add_argument("model", help="STL/3MF model to slice")
    s.add_argument("--printer", required=True, help="machine preset name")
    s.add_argument("--process", required=True, help="process preset name")
    s.add_argument(
        "--filament",
        required=True,
        action="append",
        help="filament preset per tool head, in order T0, T1, ... "
        "(give one to use it on every head)",
    )
    s.add_argument("--orca-exe", help="path to snapmaker-orca.exe")
    s.add_argument("--work-dir", help="parent folder for run directories")
    s.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="slicer timeout in seconds"
    )
    s.set_defaults(func=cmd_slice)
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
