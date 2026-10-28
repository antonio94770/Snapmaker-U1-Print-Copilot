"""Features shared by the CLI and the MCP server.

Both front ends are thin: they parse their input, call one function here and
format the returned dataclass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from print_copilot.errors import SliceError
from print_copilot.orca.conf import CurrentSetup, current_setup, default_conf_path
from print_copilot.orca.install import OrcaInstall
from print_copilot.orca.presets import KINDS, Preset, PresetKind, PresetLibrary
from print_copilot.orca.slicer import (
    DEFAULT_TIMEOUT_S,
    OverrideValue,
    SettingsOverrides,
    SliceRun,
    prepare_cli_settings,
    slice_model,
    tool_count,
)

CONF_SOURCE = "Snapmaker_Orca.conf"


@dataclass
class PresetEntry:
    name: str
    source: str
    vendor: str | None
    inherits: str | None
    path: Path


@dataclass
class PresetCatalog:
    presets: dict[str, list[PresetEntry]]
    broken: list[str] = field(default_factory=list)


def list_presets(
    library: PresetLibrary,
    kind: PresetKind | None = None,
    *,
    printer: str | None = None,
    include_abstract: bool = False,
    all_vendors: bool = False,
) -> PresetCatalog:
    """Presets per kind. Without `all_vendors`, only user presets and the
    library's vendor are listed, except for process/filament presets filtered
    by `printer`, which may live in other vendor folders."""
    catalog = PresetCatalog(presets={})
    for k in [kind] if kind else list(KINDS):
        found = library.list_presets(k, printer=printer, include_abstract=include_abstract)
        catalog.broken.extend(found.broken)
        presets = found.presets
        if not all_vendors and not (printer and k != "machine"):
            presets = [
                p for p in presets if p.ref.source == "user" or p.ref.vendor == library.vendor
            ]
        catalog.presets[k] = [
            PresetEntry(
                name=p.name,
                source=p.ref.source,
                vendor=p.ref.vendor,
                inherits=p.ref.inherits,
                path=p.ref.path,
            )
            for p in presets
        ]
    return catalog


def get_current_setup(data_dir: Path, machine: str | None = None) -> CurrentSetup:
    return current_setup(default_conf_path(data_dir), machine)


@dataclass
class Selection:
    """Resolved presets for a slice, and where each choice came from."""

    machine: Preset
    process: Preset
    filaments: list[Preset]
    notes: list[str] = field(default_factory=list)


def select_presets(
    library: PresetLibrary,
    *,
    printer: str | None = None,
    process: str | None = None,
    filaments: list[str] | None = None,
) -> Selection:
    """Resolve the presets for a slice.

    Anything not given is taken from the selection stored in
    `Snapmaker_Orca.conf` for that printer (the one the app shows), and a note
    says so. One filament given for a multi-head machine is used on every head.
    """
    notes: list[str] = []
    setup: CurrentSetup | None = None
    if printer is None or process is None or not filaments:
        setup = get_current_setup(library.data_dir, printer)
        if printer is None:
            notes.append(f"printer {setup.machine!r} from {CONF_SOURCE}")
        printer = setup.machine
    if process is None:
        assert setup is not None
        if not setup.process:
            raise SliceError(f"{CONF_SOURCE} has no process selected for {printer!r}.")
        process = setup.process
        notes.append(f"process {process!r} from {CONF_SOURCE}")
    if not filaments:
        assert setup is not None
        if not setup.filaments:
            raise SliceError(f"{CONF_SOURCE} has no filaments selected for {printer!r}.")
        filaments = setup.filaments
        notes.append(f"filaments {filaments} from {CONF_SOURCE}")

    machine_p = library.resolve("machine", printer)
    process_p = library.resolve("process", process)
    filament_p = [library.resolve("filament", name) for name in filaments]
    heads = tool_count(machine_p)
    if len(filament_p) == 1 and heads > 1:
        # The slicer needs one filament per head (HANDOFF.md section 4, gotcha 7).
        filament_p = filament_p * heads
        notes.append(f"using {filaments[0]!r} on all {heads} tool heads")
    return Selection(machine=machine_p, process=process_p, filaments=filament_p, notes=notes)


def run_slice(
    install: OrcaInstall,
    model: str | os.PathLike[str],
    selection: Selection,
    *,
    overrides: dict[str, OverrideValue] | None = None,
    work_dir: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> SliceRun:
    return slice_model(
        install,
        model,
        selection.machine,
        selection.process,
        selection.filaments,
        work_dir=work_dir,
        timeout_s=timeout_s,
        overrides=SettingsOverrides.from_flat(overrides),
    )


@dataclass
class Variant:
    overrides: dict[str, OverrideValue]
    label: str | None = None

    def display_label(self) -> str:
        if self.label:
            return self.label
        if not self.overrides:
            return "presets as is"
        return ", ".join(f"{k}={v}" for k, v in self.overrides.items())


@dataclass
class VariantResult:
    """Measured numbers of one variant. Deltas are against the first variant."""

    label: str
    overrides: dict[str, OverrideValue]
    estimated_time_s: int
    total_filament_g: float
    layer_count: int
    filament_used_g: list[float]  # index i is tool head T<i>
    time_delta_s: int
    filament_delta_g: float
    run_dir: Path


@dataclass
class Comparison:
    model: Path
    machine: str
    process: str
    filaments: list[str]
    variants: list[VariantResult]
    notes: list[str] = field(default_factory=list)


def compare_variants(
    install: OrcaInstall,
    model: str | os.PathLike[str],
    selection: Selection,
    variants: list[Variant],
    *,
    work_dir: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Comparison:
    """Slice one model once per variant and tabulate the measured results.

    Every variant's overrides are checked before the first slice, so a typo in
    the last variant does not waste the earlier runs. Any failed slice aborts
    the comparison with the variant's label in the error.
    """
    if not variants:
        raise SliceError("compare needs at least one variant.")
    parsed = []
    for variant in variants:
        try:
            overrides = SettingsOverrides.from_flat(variant.overrides)
            prepare_cli_settings(
                selection.machine, selection.process, selection.filaments, overrides
            )
        except SliceError as exc:
            raise SliceError(f"Variant {variant.display_label()!r}: {exc}") from exc
        parsed.append(overrides)

    results: list[VariantResult] = []
    for variant, overrides in zip(variants, parsed, strict=True):
        try:
            run = slice_model(
                install,
                model,
                selection.machine,
                selection.process,
                selection.filaments,
                work_dir=work_dir,
                timeout_s=timeout_s,
                overrides=overrides,
            )
        except SliceError as exc:
            raise SliceError(f"Variant {variant.display_label()!r}: {exc}") from exc
        r = run.result
        base = results[0] if results else None
        results.append(
            VariantResult(
                label=variant.display_label(),
                overrides=run.overrides,
                estimated_time_s=r.estimated_time_s,
                total_filament_g=r.total_filament_g,
                layer_count=r.layer_count,
                filament_used_g=r.filament_used_g,
                time_delta_s=r.estimated_time_s - base.estimated_time_s if base else 0,
                filament_delta_g=round(r.total_filament_g - base.total_filament_g, 2)
                if base
                else 0.0,
                run_dir=run.run_dir,
            )
        )
    return Comparison(
        model=Path(model),
        machine=selection.machine.name,
        process=selection.process.name,
        filaments=[f.name for f in selection.filaments],
        variants=results,
        notes=list(selection.notes),
    )
