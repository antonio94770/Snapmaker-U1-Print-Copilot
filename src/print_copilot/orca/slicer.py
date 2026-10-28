"""Run the Snapmaker Orca command-line slicer on a model.

Each run gets a fresh directory that keeps everything needed to reproduce it:

    <run_dir>/machine.json, process.json, filament_<n>.json   flattened presets sent to the CLI
    <run_dir>/command.txt                                    exact command line
    <run_dir>/slicer.log                                     stdout + stderr of the slicer
    <run_dir>/out/plate_1.gcode                              slicer output

The CLI workarounds below were all verified on Snapmaker Orca 01.10.01.50
(HANDOFF.md section 4). Re-verify them on every new Snapmaker Orca release.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from print_copilot.errors import SliceError
from print_copilot.gcode.header import SliceResult, parse_gcode
from print_copilot.orca.install import OrcaInstall, cli_path, to_windows_path
from print_copilot.orca.presets import Preset
from print_copilot.serialize import to_jsonable

ENV_WORK_DIR = "PRINT_COPILOT_WORK_DIR"
DEFAULT_TIMEOUT_S = 600.0

# Process keys removed before slicing because they crash the CLI.
# `wipe_tower_filament = "0"` in the U1 process presets segfaults the CLI
# (found by key bisection, HANDOFF.md section 4, gotcha 5). The effect of
# dropping it on multi-colour prints is TO VERIFY IN SNAPMAKER ORCA.
CRASHING_PROCESS_KEYS: tuple[str, ...] = ("wipe_tower_filament",)


def default_work_dir() -> Path:
    env = os.environ.get(ENV_WORK_DIR)
    if env:
        return to_windows_path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "print-copilot" / "runs"


def tool_count(machine: Preset) -> int:
    """Number of tool heads, taken from the machine's `nozzle_diameter` list."""
    nozzles = machine.settings.get("nozzle_diameter")
    if not isinstance(nozzles, list) or not nozzles:
        raise SliceError(
            f"Machine preset {machine.name!r} has no nozzle_diameter list; "
            "cannot tell how many tool heads it has."
        )
    return len(nozzles)


def prepare_cli_settings(
    machine: Preset, process: Preset, filaments: list[Preset]
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Return copies of the flattened presets with the CLI workarounds applied."""
    expected = tool_count(machine)
    if len(filaments) != expected:
        raise SliceError(
            f"{machine.name!r} has {expected} tool heads, so the slicer needs "
            f"{expected} filament presets (one per head); got {len(filaments)}."
        )

    machine_s = copy.deepcopy(machine.settings)
    # With "from": "User" the CLI reports "process not compatible with printer"
    # (HANDOFF.md section 4, gotcha 4).
    machine_s["from"] = "system"

    process_s = copy.deepcopy(process.settings)
    process_s["from"] = "User"
    for key in CRASHING_PROCESS_KEYS:
        process_s.pop(key, None)

    filament_s = []
    for preset in filaments:
        settings = copy.deepcopy(preset.settings)
        settings["from"] = "User"
        filament_s.append(settings)
    return machine_s, process_s, filament_s


@dataclass
class SliceRun:
    """A finished slice: where its files are and what the G-code reports."""

    run_dir: Path
    gcode_path: Path
    log_path: Path
    command: list[str]
    machine: str
    process: str
    filaments: list[str]
    result: SliceResult
    duration_s: float = field(default=0.0)


def _new_run_dir(work_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = work_dir / f"{stamp}-{uuid.uuid4().hex[:6]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def _describe_exit(code: int) -> str:
    unsigned = code & 0xFFFFFFFF
    hint = ""
    if unsigned == 0xC0000005 or code == 139:
        hint = " (crash: access violation / segfault)"
    return f"exit code {code} (0x{unsigned:08X}){hint}"


def _log_tail(log_path: Path, lines: int = 15) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def slice_model(
    install: OrcaInstall,
    model: str | os.PathLike[str],
    machine: Preset,
    process: Preset,
    filaments: list[Preset],
    *,
    work_dir: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> SliceRun:
    """Slice `model` with the given presets and parse the resulting G-code.

    Raises `SliceError` on a crash, a non-zero exit, a timeout or missing output.
    """
    model_path = to_windows_path(model)
    if not model_path.is_file():
        raise SliceError(f"Model file not found: {model_path}")

    machine_s, process_s, filament_s = prepare_cli_settings(machine, process, filaments)

    run_dir = _new_run_dir(to_windows_path(work_dir) if work_dir else default_work_dir())
    out_dir = run_dir / "out"
    out_dir.mkdir()
    machine_path = run_dir / "machine.json"
    process_path = run_dir / "process.json"
    _write_json(machine_path, machine_s)
    _write_json(process_path, process_s)
    filament_paths = []
    for index, settings in enumerate(filament_s, start=1):
        path = run_dir / f"filament_{index}.json"
        _write_json(path, settings)
        filament_paths.append(path)

    command = [
        str(install.exe),
        "--slice",
        "0",
        "--load-settings",
        f"{cli_path(machine_path)};{cli_path(process_path)}",
        "--load-filaments",
        ";".join(cli_path(p) for p in filament_paths),
        "--outputdir",
        cli_path(out_dir),
        cli_path(model_path),
    ]
    (run_dir / "command.txt").write_text(subprocess.list2cmdline(command) + "\n", encoding="utf-8")

    log_path = run_dir / "slicer.log"
    started = datetime.now()
    try:
        with log_path.open("wb") as log:
            completed = subprocess.run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=timeout_s,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise SliceError(
            f"Snapmaker Orca did not finish within {timeout_s:.0f} s. Run dir: {run_dir}"
        ) from exc
    except OSError as exc:
        raise SliceError(f"Could not start Snapmaker Orca ({install.exe}): {exc}") from exc
    duration_s = (datetime.now() - started).total_seconds()

    if completed.returncode != 0:
        raise SliceError(
            f"Snapmaker Orca failed with {_describe_exit(completed.returncode)}. "
            f"Run dir: {run_dir}\nLog tail:\n{_log_tail(log_path)}"
        )

    gcode_files = sorted(out_dir.glob("*.gcode"))
    if not gcode_files:
        raise SliceError(
            f"Snapmaker Orca exited 0 but wrote no G-code. Run dir: {run_dir}\n"
            f"Log tail:\n{_log_tail(log_path)}"
        )
    gcode_path = gcode_files[0]
    result = parse_gcode(gcode_path)

    if result.tool_count != len(filaments):
        raise SliceError(
            f"G-code reports {result.tool_count} filament slots, expected {len(filaments)}. "
            f"Run dir: {run_dir}"
        )
    if result.printer_settings_id and result.printer_settings_id != machine.name:
        raise SliceError(
            f"G-code was sliced for printer {result.printer_settings_id!r}, "
            f"expected {machine.name!r}. Run dir: {run_dir}"
        )
    if result.print_settings_id and result.print_settings_id != process.name:
        raise SliceError(
            f"G-code was sliced with process {result.print_settings_id!r}, "
            f"expected {process.name!r}. Run dir: {run_dir}"
        )

    run = SliceRun(
        run_dir=run_dir,
        gcode_path=gcode_path,
        log_path=log_path,
        command=command,
        machine=machine.name,
        process=process.name,
        filaments=[f.name for f in filaments],
        result=result,
        duration_s=duration_s,
    )
    (run_dir / "result.json").write_text(
        json.dumps(to_jsonable(run), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return run
