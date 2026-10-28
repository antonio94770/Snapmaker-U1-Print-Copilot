# Snapmaker Print Copilot

An MCP server and command line that help prepare prints for the
**Snapmaker U1** toolchanger, using **Snapmaker Orca** as the slicing engine.
An AI agent (Claude Code, Codex, Claude Desktop, ...) or a person at the
terminal can list presets, slice a model headlessly and compare the measured
results.

Status: early development (milestone M3, "model analysis"). U1 tool assignment,
`.3mf` project writing and printer control are planned, see `HANDOFF.md`.

To use it from Claude Code, Codex or Claude Desktop, see
[docs/mcp-setup.md](docs/mcp-setup.md).

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

Without `--printer`, `--process` or `--filament`, the selection stored in
Snapmaker Orca is used (a note says so). Override settings for one run with
`--set` (process keys, or `machine.KEY` / `filament.KEY`); unknown keys are
rejected:

```bash
print-copilot slice research/cli-probe/cube.stl --set sparse_infill_density=30% --set wall_loops=3
```

Show what is selected in Snapmaker Orca (printer, process, filament and colour
per tool head, purge volumes):

```bash
print-copilot setup
```

Compare variants of one model (each `--variant` is a `;`-separated list of
overrides, an empty one is the presets as they are):

```bash
print-copilot compare research/cli-probe/cube.stl --variant "" --variant "sparse_infill_density=30%"
```

Add `--json` to any command for machine-readable output. Every slice runs
in a fresh folder under `%LOCALAPPDATA%\print-copilot\runs` (override with
`--work-dir` or `PRINT_COPILOT_WORK_DIR`) that keeps the flattened presets,
the exact command line, the slicer log, the G-code and `result.json`.

Other locations can be set with `--orca-exe` / `PRINT_COPILOT_ORCA_EXE` and
`--orca-data` / `PRINT_COPILOT_ORCA_DATA`.

Find a print orientation. `analyze` ranks candidate orientations (as loaded,
the six axes, the largest flat faces) with a geometric overhang heuristic and
does not slice; `orient` slices the best ones without and with supports and
recommends one by the slicer's measured time:

```bash
print-copilot analyze tests/fixtures/tee.stl
print-copilot orient tests/fixtures/tee.stl --top 3 --support none --support tree
```

The orientation table shows heuristic numbers (overhang area, a support
volume that overestimates); the slice table shows the slicer's own time and
grams, plus the support filament measured from the G-code's support
toolpaths. Each rotated model is saved as an STL in the run folder.

Some surfaces matter more than others: a miniature's face, a logo, the side
that will be seen. Render the model, mark those surfaces as protected and rank
for quality instead of speed:

```bash
print-copilot render model.stl --out views.png
print-copilot orient model.stl --goal quality --protect "{\"label\": \"face\", \"direction\": [0, -1, 0], \"box_min_mm\": [-7, -13, 19], \"box_max_mm\": [7, -3, 32]}"
print-copilot render model.stl --down 0,0,-1 --protect "{...same...}" --out check.png
```

`render` draws labelled views (grid in mm, axes) and colours the faces that
rest on the bed, need support, are protected, or are protected and need
support. A protected region is a direction cone, a box in the model's
coordinates as loaded, or both. The `quality` goal ranks by a weighted surface
cost (support scars and bed marks on protected surfaces weigh ten times more
than elsewhere, layer steps less) and puts orientations that stand on an edge
or a point last. An AI agent uses the same steps through MCP: it looks at the
rendered image, decides which surface is the face, and checks the result.

## How slicing works

The Snapmaker Orca command line needs a few workarounds, found by testing
(details in `HANDOFF.md` section 4):

- system presets are flattened (their `inherits` chain merged) before use;
- the machine preset is marked `"from": "system"`;
- the `wipe_tower_filament` process key is removed (it crashes the CLI);
- paths are passed Windows-style;
- one filament preset is passed per tool head;
- every override is checked against the config block of the G-code, because
  the CLI silently replaces values it does not accept;
- the output folder must stay within 229 characters, or the CLI cannot write
  the G-code.

Your Snapmaker Orca installation and presets are only read, never modified.

## Development

```bash
ruff check . && ruff format --check .
pytest
```

The integration tests slice a 20 mm cube and a T-shaped part and are skipped when Snapmaker Orca is
not installed.

## License

Not yet decided (GPL-3.0-or-later is planned). Snapmaker Orca is AGPL-3.0 and
is only invoked as an external program.
