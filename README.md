# Snapmaker Print Copilot

An MCP server and command line that help prepare prints for the
**Snapmaker U1** toolchanger, using **Snapmaker Orca** as the slicing engine.
An AI agent (Claude Code, Codex, Claude Desktop, ...) or a person at the
terminal can list presets, slice a model headlessly and compare the measured
results.

Status: early development (milestone M1, "Orca core"). The MCP server, model
analysis, `.3mf` project writing and printer control are planned, see
`HANDOFF.md`.

## Requirements

- Windows with Snapmaker Orca installed (verified with 01.10.01.50 at
  `C:\Program Files\Snapmaker_Orca\`) and started at least once, so that
  `%APPDATA%\Snapmaker_Orca` exists.
- Python 3.11 or newer.

## Install

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -e ".[dev]"
```

## Usage

List presets (Snapmaker vendor presets plus your own):

```bash
print-copilot presets --kind process
print-copilot presets --kind filament --printer "Snapmaker U1 (0.4 nozzle)"
```

Slice a model. Give one `--filament` per tool head (T0..T3), or a single one to
use it on every head:

```bash
print-copilot slice research/cli-probe/cube.stl --printer "Snapmaker U1 (0.4 nozzle)" --process "0.20mm Standard @Snapmaker U1 (0.4 nozzle)" --filament "Generic PLA"
```

Add `--json` to either command for machine-readable output. Every slice runs
in a fresh folder under `%LOCALAPPDATA%\print-copilot\runs` (override with
`--work-dir` or `PRINT_COPILOT_WORK_DIR`) that keeps the flattened presets,
the exact command line, the slicer log, the G-code and `result.json`.

Other locations can be set with `--orca-exe` / `PRINT_COPILOT_ORCA_EXE` and
`--orca-data` / `PRINT_COPILOT_ORCA_DATA`.

## How slicing works

The Snapmaker Orca command line needs a few workarounds, found by testing
(details in `HANDOFF.md` section 4):

- system presets are flattened (their `inherits` chain merged) before use;
- the machine preset is marked `"from": "system"`;
- the `wipe_tower_filament` process key is removed (it crashes the CLI);
- paths are passed Windows-style;
- one filament preset is passed per tool head.

Your Snapmaker Orca installation and presets are only read, never modified.

## Development

```bash
ruff check . && ruff format --check .
pytest
```

The integration test slices a 20 mm cube and is skipped when Snapmaker Orca is
not installed.

## License

Not yet decided (GPL-3.0-or-later is planned). Snapmaker Orca is AGPL-3.0 and
is only invoked as an external program.
