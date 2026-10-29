# Command line reference

Every MCP tool has a `print-copilot` command, so everything below works
without an AI agent. Add `--json` to any command for machine-readable output,
and `--help` to any command for all its options.

- [Presets, slicing and variants](#presets-slicing-and-variants)
- [Multi-colour projects on the U1](#multi-colour-projects-on-the-u1)
- [Quality review before printing](#quality-review-before-printing)
- [Printer (Moonraker)](#printer-moonraker)
- [How slicing works](#how-slicing-works)

## Presets, slicing and variants

List presets (Snapmaker vendor presets plus your own):

```bash
print-copilot presets --kind process
print-copilot presets --kind filament --printer "Snapmaker U1 (0.4 nozzle)"
```

Slice a model. Give one `--filament` per tool head (T0..T3), or a single one to
use it on every head:

```bash
print-copilot slice model.stl --printer "Snapmaker U1 (0.4 nozzle)" --process "0.20mm High Quality @Snapmaker U1 (0.4 nozzle)" --filament "Generic PLA"
```

The command above uses the bundled 2.4.1 process name. On 01.10.01.50 the
0.20 mm process was named `Standard`; use `presets` to check your installed names.

Without `--printer`, `--process` or `--filament`, the selection stored in
Snapmaker Orca is used (a note says so). Override settings for one run with
`--set` (process keys, or `machine.KEY` / `filament.KEY`); unknown keys are
rejected:

```bash
print-copilot slice model.stl --set sparse_infill_density=30% --set wall_loops=3
```

Show what is selected in Snapmaker Orca (printer, process, filament and colour
per tool head, purge volumes):

```bash
print-copilot setup
```

Compare variants of one model (each `--variant` is a `;`-separated list of
overrides, an empty one is the presets as they are):

```bash
print-copilot compare model.stl --variant "" --variant "sparse_infill_density=30%"
```

Every slice runs in a fresh folder under `%LOCALAPPDATA%\print-copilot\runs` (override with
`--work-dir` or `PRINT_COPILOT_WORK_DIR`) that keeps the flattened presets,
the exact command line, the slicer log, the G-code and `result.json`.

Other locations can be set with `--orca-exe` / `PRINT_COPILOT_ORCA_EXE` and
`--orca-data` / `PRINT_COPILOT_ORCA_DATA`.

Find a print orientation. `analyze` ranks candidate orientations (as loaded,
the six axes, the largest flat faces) with a geometric overhang heuristic and
does not slice; `orient` slices the best ones without and with supports and
recommends one by the slicer's measured time:

```bash
print-copilot analyze model.stl
print-copilot orient model.stl --top 3 --support none --support tree
```

The orientation table shows heuristic numbers (overhang area, a support
volume that overestimates); the slice table shows the slicer's own time and
grams, plus the support filament measured from the G-code's support
toolpaths. Each rotated model is saved as an STL in the run folder.

Choose the settings the model's shape bears on. `suggest` measures thin
features (horizontal sections, local thickness), surfaces where layer steps
show and the bed contact, and proposes a few process settings for the print's
purpose: variable-width walls for thin features, an outer brim for a tall part
on a small base, finer layers for a visual print, more walls and infill for a
functional one. It does not slice; the proposals are a labelled heuristic,
written as variants that `quality` slices and compares. In a 3MF project,
settings an object sets itself win over these proposals for that object, and
`suggest` names them:

```bash
print-copilot suggest model.stl --purpose visual --variants-out variants.json
print-copilot quality model.stl --head 1 --variants-file variants.json
```

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
`--map FILAMENT=T<head>`. The loaded colours come from the U1's RFID readers
when the printer answers (`--no-printer` skips it), otherwise from the
selection saved in Snapmaker Orca; the notes say which.

An STL is handled as a one-object project. A plate or STL in one colour is
never put on a head automatically: the command stops, lists the loaded spools
and asks for `--head T<n>`.

```bash
print-copilot export-project funnel.stl --head T1 --out funnel-black.3mf
```

`export-project` writes the project, slices that same project and puts the
G-code next to it (`funnel-black.gcode`, unless `--no-slice`). Upload that
G-code; `funnel-black.3mf` is the file to reopen in Snapmaker Orca to see or
change exactly what was printed.

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
and one object at a time. `export-project --open` also opens the new 3MF in
Snapmaker Orca. A plate that needs more colours than
the four heads stops with the closest colour pairs; `--map` or
`--reduce-colours` decide which colours share a head.

On the U1 every head has its own nozzle, so the purge matrix does not change
the slice (verified); what costs time and material is the number of tool
changes and the prime tower. For example, a Spider-Man project (four filaments
in two colours, identical colours sharing a head) measured with 0.20 mm
Standard layers and Generic PLA on 01.10.01.50: 4 h 42 min, 79.9 g and 353
tool changes, with the prime tower enabled. Print quality without the prime
tower and the firmware's own pre-extrusion consumption remain unverified on
hardware.

## Quality review before printing

`quality` audits the **effective G-code settings**, renders model and toolpath
views, and compares the unchanged setup with a finer-layer candidate within
the machine preset's limits. Every effective parameter is inventoried with
its actual automatic-check coverage. Unverified calibration and hardware
conditions stay visible; there is no promise of defect-free prints.

```powershell
print-copilot quality model.stl
print-copilot quality painted-project.3mf --plate 1
print-copilot quality plate_1.gcode
print-copilot preview plate_1.gcode --layer 1 --layer 50
```

Open the generated `report.html` to compare measured slicing results and
inspect all layers in the offline viewer. The initial status is pending visual
review. `quality-review` records written observations and explicit acceptance
of remaining findings; changed evidence invalidates the review. It never
authorizes a physical print. MCP equivalents: `review_print`, `preview_gcode`,
`record_quality_review`. See [quality-review.md](quality-review.md)
for the checks, limitations and review procedure.

## Printer (Moonraker)

The U1 runs Moonraker on the LAN (port 80 and 7125, no login) and announces
itself over mDNS, so its address is found on every run and never stored.
`printer discover` lists what answered; `--printer-url` or
`PRINT_COPILOT_PRINTER_URL` overrides discovery, and `PRINT_COPILOT_PRINTER_NAME`
picks one of several U1 printers by device name:

```bash
print-copilot printer discover
print-copilot printer status
print-copilot printer upload part.gcode
print-copilot printer start part.gcode --dry-run
```

`status` shows the state, temperatures and, per head, the spool its RFID
reader identified. `upload` only copies G-code sliced for the U1 and never
starts it. `start` first prints the plan (the exact request, the slicer's
time and grams stored in the file, per-head checks against nozzle and RFID
spools) and sends only after you type `start` (or pass `--yes`); `--dry-run`
sends nothing. `cancel` and `heaters-off` are never gated. Without a printer,
`print-copilot printer fake` runs a fake U1 Moonraker on this computer.

## How slicing works

The Snapmaker Orca command line needs a few workarounds, found by testing
(details in [verified-facts.md](verified-facts.md) section 2):

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
