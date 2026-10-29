"""Run the Snapmaker Orca command-line slicer on a model.

Each run gets a fresh directory that keeps everything needed to reproduce it:

    <run_dir>/machine.json, process.json, filament_<n>.json   flattened presets sent to the CLI
    <run_dir>/overrides.json                                 setting overrides (if any)
    <run_dir>/command.txt                                    exact command line
    <run_dir>/slicer.log                                     stdout + stderr of the slicer
    <run_dir>/out/plate_1.gcode                              slicer output

The CLI workarounds below were all verified on Snapmaker Orca 01.10.01.50
and rechecked on installed Windows 2.4.1 (docs/verified-facts.md sections 2 and 4).
Re-verify them on every new Snapmaker Orca release.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from print_copilot.errors import SliceError
from print_copilot.gcode.header import SliceResult, parse_gcode, read_config_block
from print_copilot.orca.install import OrcaInstall, cli_path, to_windows_path
from print_copilot.orca.presets import Preset
from print_copilot.serialize import to_jsonable

ENV_WORK_DIR = "PRINT_COPILOT_WORK_DIR"
ENV_EXPORT_DIR = "PRINT_COPILOT_EXPORT_DIR"
DEFAULT_TIMEOUT_S = 600.0

# Longest `--outputdir` that still works: at 230 characters the CLI fails with
# "GCode processor post process export failed. Cannot open file for writing."
# (bisected on 01.10.01.50 with the default plate_1.gcode name; the Windows
# 260-character path limit minus the files the CLI writes there).
MAX_OUTPUT_DIR_CHARS = 229

# Process keys removed before slicing because they crash the CLI.
# `wipe_tower_filament = "0"` in the U1 process presets segfaults the CLI
# (found by key bisection, docs/verified-facts.md section 2, gotcha 5). Dropping it is
# harmless: the G-code config block of multi-tool slices still shows
# `wipe_tower_filament = 0`, the default (verified on 01.10.01.50).
CRASHING_PROCESS_KEYS: tuple[str, ...] = ("wipe_tower_filament",)

# Without `filament_colour` in the filament presets the CLI keeps a single
# default colour, and the multi-colour segmentation then drops every painted
# state above 1 ("dropping painted states above segmentation range ...
# physical_filaments=1" in the log; MultiMaterialSegmentation.cpp counts
# filaments by `filament_colour`). Verified on 01.10.01.50: a painted project
# printed only T0 until each head's colour was set.
FILAMENT_COLOUR_KEY = "filament_colour"

# Process keys the U1 presets leave at the slicer default but that can be set
# for a run: the prime tower position (one value per plate). Passing them in
# the process JSON works: the G-code config block shows the new values
# (verified on 01.10.01.50).
PROCESS_EXTRA_KEYS: frozenset[str] = frozenset({"wipe_tower_x", "wipe_tower_y"})

# Keys that identify a preset or are set by the CLI workarounds. Overriding
# them would make a run misreport which presets it used.
PROTECTED_KEYS: frozenset[str] = frozenset(
    {"name", "inherits", "from", "instantiation", "setting_id", "type"}
)

OverrideValue = str | int | float | bool | list[str]

# Flat override keys may carry a section prefix, e.g. `machine.nozzle_diameter`.
# No preset key contains a dot (checked on 01.10.01.50), so this is unambiguous.
SECTIONS: tuple[str, ...] = ("process", "machine", "filament")


@dataclass(frozen=True)
class SettingsOverrides:
    """Key/value overrides applied to the flattened presets before slicing.

    `filament` overrides apply to the filament preset of every tool head.
    """

    process: dict[str, OverrideValue] = field(default_factory=dict)
    machine: dict[str, OverrideValue] = field(default_factory=dict)
    filament: dict[str, OverrideValue] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.process or self.machine or self.filament)

    @classmethod
    def from_flat(cls, values: Mapping[str, OverrideValue] | None) -> SettingsOverrides:
        """Build overrides from `{key: value}`. A key without a prefix is a process
        key; `machine.<key>` and `filament.<key>` target the other presets."""
        sections: dict[str, dict[str, OverrideValue]] = {s: {} for s in SECTIONS}
        for raw_key, value in (values or {}).items():
            section, dot, key = raw_key.partition(".")
            if not dot:
                section, key = "process", raw_key
            if section not in sections or not key or "." in key:
                raise SliceError(
                    f"Invalid override key {raw_key!r}: use 'key', 'machine.key' or 'filament.key'."
                )
            sections[section][key] = value
        return cls(**sections)

    def to_flat(self) -> dict[str, OverrideValue]:
        flat: dict[str, OverrideValue] = dict(self.process)
        flat.update({f"machine.{k}": v for k, v in self.machine.items()})
        flat.update({f"filament.{k}": v for k, v in self.filament.items()})
        return flat


def _setting_text(key: str, value: Any) -> str:
    if isinstance(value, bool):
        # Preset JSON stores booleans as "0" / "1".
        return "1" if value else "0"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return value
    raise SliceError(f"Override {key!r} must be a string, number, boolean or list of those.")


def apply_overrides(
    settings: dict[str, Any],
    overrides: Mapping[str, OverrideValue],
    *,
    section: str,
    preset_name: str,
) -> None:
    """Apply overrides to a flattened settings dict in place.

    Only keys the flattened preset defines can be overridden, so a typo fails
    instead of reaching the slicer and being ignored. A single value given for
    a list setting (one entry per extruder or filament) fills every entry.
    """
    for key, value in overrides.items():
        if key in PROTECTED_KEYS:
            raise SliceError(f"The {section} setting {key!r} cannot be overridden.")
        if section == "process" and key in CRASHING_PROCESS_KEYS:
            raise SliceError(
                f"The process setting {key!r} cannot be overridden: it is removed before "
                "slicing because it crashes the Snapmaker Orca CLI."
            )
        if key not in settings:
            raise SliceError(
                f"Unknown {section} setting {key!r}: the flattened preset {preset_name!r} "
                "does not define it. Check the spelling against the preset JSON."
            )
        current = settings[key]
        if isinstance(value, list):
            if not isinstance(current, list):
                raise SliceError(
                    f"The {section} setting {key!r} holds a single value in "
                    f"{preset_name!r}; give a single value, not a list."
                )
            settings[key] = [_setting_text(key, v) for v in value]
        elif isinstance(current, list):
            settings[key] = [_setting_text(key, value)] * max(len(current), 1)
        else:
            settings[key] = _setting_text(key, value)


def _same_setting(expected: str, written: str) -> bool:
    expected, written = expected.strip().strip('"'), written.strip().strip('"')
    if expected == written:
        return True
    try:
        return float(expected.rstrip("%")) == float(written.rstrip("%")) and (
            expected.endswith("%") == written.endswith("%")
        )
    except ValueError:
        return False


def check_overrides_applied(config: Mapping[str, str], overrides: SettingsOverrides) -> None:
    """Fail if the G-code config block does not show an override's value.

    The CLI silently replaces values it does not accept: `support_type = bogus`
    is sliced as `normal(auto)` (verified on 01.10.01.50). Keys missing from the
    config block and multi-line values are not checked.
    """
    for key, value in {**overrides.machine, **overrides.filament, **overrides.process}.items():
        if key not in config:
            continue
        expected = [_setting_text(key, v) for v in value] if isinstance(value, list) else None
        if expected is None:
            expected = [_setting_text(key, value)]
        if any("\n" in e or ";" in e for e in expected):
            continue
        written = config[key].split(",")
        if _same_setting(",".join(expected), config[key]):
            ok = True
        elif len(set(expected)) == 1:
            ok = all(_same_setting(expected[0], w) for w in written)
        else:
            ok = len(expected) == len(written) and all(
                _same_setting(e, w) for e, w in zip(expected, written, strict=True)
            )
        if not ok:
            raise SliceError(
                f"Snapmaker Orca did not apply the override {key} = {value!r}: the G-code "
                f"says {key} = {config[key]}. The value is probably not one the slicer "
                "accepts (it falls back silently); check the allowed values in the app."
            )


def default_work_dir() -> Path:
    env = os.environ.get(ENV_WORK_DIR)
    if env:
        return to_windows_path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "print-copilot" / "runs"


def default_export_dir() -> Path:
    """Where exported projects go when no output path is given."""
    env = os.environ.get(ENV_EXPORT_DIR)
    if env:
        return to_windows_path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "print-copilot" / "exports"


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
    machine: Preset,
    process: Preset,
    filaments: list[Preset],
    overrides: SettingsOverrides | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Return copies of the flattened presets with the overrides applied first,
    then the CLI workarounds. The presets passed in are not modified."""
    overrides = overrides or SettingsOverrides()
    expected = tool_count(machine)
    if len(filaments) != expected:
        raise SliceError(
            f"{machine.name!r} has {expected} tool heads, so the slicer needs "
            f"{expected} filament presets (one per head); got {len(filaments)}."
        )

    machine_s = copy.deepcopy(machine.settings)
    apply_overrides(machine_s, overrides.machine, section="machine", preset_name=machine.name)
    # With "from": "User" the CLI reports "process not compatible with printer"
    # (docs/verified-facts.md section 2, gotcha 4).
    machine_s["from"] = "system"

    process_s = copy.deepcopy(process.settings)
    apply_overrides(process_s, overrides.process, section="process", preset_name=process.name)
    process_s["from"] = "User"
    for key in CRASHING_PROCESS_KEYS:
        process_s.pop(key, None)

    filament_s = []
    for preset in filaments:
        settings = copy.deepcopy(preset.settings)
        apply_overrides(settings, overrides.filament, section="filament", preset_name=preset.name)
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
    # Overrides applied, in flat form (`key`, `machine.key`, `filament.key`).
    overrides: dict[str, OverrideValue] = field(default_factory=dict)


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


# Log lines that carry no reason for a failure.
_LOG_NOISE = ("[trace]", "[debug]", "[info]", "sentry_init", "Sentry session", "Initializing")


def _log_tail(log_path: Path, lines: int = 15) -> str:
    """The last lines of the slicer log, skipping trace and Sentry noise so
    the reason (e.g. "... is too close to others") is visible."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    useful = [
        line
        for line in text.strip().splitlines()
        if line.strip()
        and not any(n in line for n in _LOG_NOISE)
        and "calc_exclude_triangles" not in line
    ]
    return "\n".join((useful or text.strip().splitlines())[-lines:])


def slice_model(
    install: OrcaInstall,
    model: str | os.PathLike[str],
    machine: Preset,
    process: Preset,
    filaments: list[Preset],
    *,
    work_dir: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    overrides: SettingsOverrides | None = None,
    filament_colours: list[str] | None = None,
    extra_args: list[str] | None = None,
    process_extra: Mapping[str, list[str]] | None = None,
) -> SliceRun:
    """Slice `model` with the given presets and parse the resulting G-code.

    `overrides` change settings of the flattened presets for this run only.
    `filament_colours` (one per head) is written into each filament preset;
    painted models need it. `extra_args` go before the model path.
    `process_extra` adds process keys the preset does not define (only
    `PROCESS_EXTRA_KEYS`); they are checked in the G-code config block.

    Raises `SliceError` on a crash, a non-zero exit, a timeout or missing output.
    """
    model_path = to_windows_path(model)
    if not model_path.is_file():
        raise SliceError(f"Model file not found: {model_path}")

    overrides = overrides or SettingsOverrides()
    machine_s, process_s, filament_s = prepare_cli_settings(machine, process, filaments, overrides)
    if filament_colours is not None:
        if len(filament_colours) != len(filament_s):
            raise SliceError(
                f"Got {len(filament_colours)} filament colours for {len(filament_s)} tool heads."
            )
        for settings, colour in zip(filament_s, filament_colours, strict=True):
            settings[FILAMENT_COLOUR_KEY] = [colour]
    for key, value in (process_extra or {}).items():
        if key not in PROCESS_EXTRA_KEYS:
            raise SliceError(f"{key!r} cannot be added to the process preset.")
        process_s[key] = list(value)

    base_dir = to_windows_path(work_dir) if work_dir else default_work_dir()
    expected_out = len(str(base_dir / "20261028-120000-abcdef" / "out"))
    if expected_out > MAX_OUTPUT_DIR_CHARS:
        raise SliceError(
            f"The work dir {base_dir} is too long: Snapmaker Orca cannot write G-code into an "
            f"output folder longer than {MAX_OUTPUT_DIR_CHARS} characters (this one would be "
            f"{expected_out}). Use a shorter --work-dir or {ENV_WORK_DIR}."
        )
    run_dir = _new_run_dir(base_dir)
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
    if not overrides.is_empty():
        _write_json(run_dir / "overrides.json", to_jsonable(overrides))

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
        *(extra_args or []),
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
    checked = overrides
    if process_extra:
        checked = SettingsOverrides(
            process={**overrides.process, **{k: v[0] for k, v in process_extra.items()}},
            machine=overrides.machine,
            filament=overrides.filament,
        )
    if not checked.is_empty():
        try:
            check_overrides_applied(read_config_block(gcode_path), checked)
        except SliceError as exc:
            raise SliceError(f"{exc} Run dir: {run_dir}") from exc

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
        overrides=overrides.to_flat(),
    )
    (run_dir / "result.json").write_text(
        json.dumps(to_jsonable(run), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return run
