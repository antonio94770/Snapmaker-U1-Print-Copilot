# Snapmaker Print Copilot

An MCP server and command line that help prepare prints for the
**Snapmaker U1** toolchanger, using **Snapmaker Orca** as the slicing engine.
An AI agent (Claude Code, Codex, Claude Desktop, ...) or a person at the
terminal can list presets, slice a model headlessly and compare the measured
results.

Status: early development (milestone M4, "U1 specifics"): presets, headless
slicing, orientation search and multi-colour 3MF projects work; printer
control is planned, see `HANDOFF.md`.

To use it from Claude Code, Codex or Claude Desktop, see
[docs/mcp-setup.md](docs/mcp-setup.md).

## Requirements

- Windows with Snapmaker Orca installed (verified with 01.10.01.50 and 2.4.1 at
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
print-copilot slice research/cli-probe/cube.stl --printer "Snapmaker U1 (0.4 nozzle)" --process "0.20mm High Quality @Snapmaker U1 (0.4 nozzle)" --filament "Generic PLA"
```

The command above uses the bundled 2.4.1 process name. On 01.10.01.50 the
0.20 mm process was named `Standard`; use `presets` to check your installed names.

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

## Multi-colour projects on the U1

3MF projects saved by Snapmaker Orca or Bambu Studio (for example downloads
from MakerWorld or Printables) are read with their plates, per-object and
per-part filaments and painted colours. Each filament a plate uses is put on
the U1 head whose loaded colour is closest (CIE76 delta E, a heuristic);
filaments of the same colour share a head, and you can fix any of them with
`--map FILAMENT=T<head>`:

```bash
print-copilot project mask.3mf
print-copilot slice-project mask.3mf --plate 2
print-copilot slice-project mask.3mf --plate 1 --map 1=T2 --map 8=T3
print-copilot strategies mask.3mf --plate 7
print-copilot export-project mask.3mf --plate 2 --out mask-plate2-u1.3mf --open
```

`slice-project` reports the slicer's time and grams per head plus what it
measures in the G-code: tool changes, layers with changes, prime tower and
purge grams. `strategies` slices the same plate as is, without the prime tower
and one object at a time. `export-project` writes a new 3MF with the U1
presets and opens it in Snapmaker Orca. A plate that needs more colours than
the four heads stops with the closest colour pairs; `--map` or
`--reduce-colours` decide which colours share a head.

On the U1 every head has its own nozzle, so the purge matrix does not change
the slice (verified); what costs time and material is the number of tool
changes and the prime tower. After the painting-remapping fix, the Spider-Man
project (four filaments in two colours, identical colours sharing a head) was
measured with 0.20 mm Standard layers and Generic PLA on 01.10.01.50:
4 h 42 min, 79.9 g and 353 tool changes, with the prime tower enabled.
The earlier 0.08 mm strategy comparison predates that fix and has been removed;
it must be measured again before being used as a performance example.
Print quality without the prime tower and the firmware's own pre-extrusion
consumption remain unverified on hardware.

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
  the G-code;
- 3MF projects are sliced from a copy without plates and without the
  `Application` tag, with the objects of one plate centred on the bed and
  `--arrange 0`. The plate-only crash is fixed in 2.4.1, but partial project
  metadata can still crash it and a full project probe silently disabled the
  prime tower. The copy workaround remains necessary;
- each head's colour is passed in its filament preset, otherwise the slicer
  drops painted colours;
- if the prime tower collides with an object, it is moved to a free corner
  and a note says so.

System presets are read from the user data's `system` folder when present;
otherwise they are read from `resources/profiles` beside the selected executable
(verified with Windows 2.4.1). User presets and selections still come from
`%APPDATA%\Snapmaker_Orca`. Preset names are never substituted automatically.
Your Snapmaker Orca installation and presets are only read, never modified.

## Development

```bash
ruff check . && ruff format --check .
pytest
```

The integration tests slice a 20 mm cube, a T-shaped part and a synthetic
multi-colour project, and are skipped when Snapmaker Orca is not installed.
Tests on real downloaded projects run when `PRINT_COPILOT_TEST_PROJECTS`
points to a folder filled by `python research/m4-probe/download_projects.py <folder>`
(the files are not in the repository; their licences vary).

## License

Not yet decided (GPL-3.0-or-later is planned). Snapmaker Orca is AGPL-3.0 and
is only invoked as an external program.
