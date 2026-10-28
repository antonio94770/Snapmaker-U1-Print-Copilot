# Quality review before printer integration

The quality workflow runs against the official Snapmaker Orca installation.
It audits the effective settings in the generated G-code, creates inspectable
model/toolpath views, and compares measured slicing results. It does not
predict physical roughness or certify that a print will succeed.

## Commands and MCP tools

| CLI | MCP | Purpose |
| --- | --- | --- |
| `quality` | `review_print` | Slice a model/one project plate, audit, compare and generate a report |
| `preview` | `preview_gcode` | Inspect specific G-code layers or representative layers |
| `quality-review` | `record_quality_review` | Record explicit visual observations and acknowledge remaining findings |

Existing `.gcode` can be audited without an Orca installation or preset
selection. Model inputs use the same preset resolution and U1 head/painting
handling as `slice` and `slice-project`. Nothing is sent to the printer.

```powershell
print-copilot quality tests/fixtures/tee.stl --printer "Snapmaker U1 (0.4 nozzle)" --process "0.20mm High Quality @Snapmaker U1 (0.4 nozzle)" --filament "Generic PLA"
print-copilot quality painted-project.3mf --plate 1
print-copilot quality plate_1.gcode --work-dir runs/quality
print-copilot preview plate_1.gcode --layer 1 --layer 101 --layer 125
```

Use the process names available in your installed release. The default model
comparison keeps the unchanged setup and adds one candidate with half the
nominal layer height, clamped to the machine preset's declared lower limit.
If the limits are unavailable or the current height is already at that limit,
only the baseline is reviewed. No temperature, flow, pressure advance,
retraction or cooling value is automatically tuned.

Use `--no-compare` for a baseline-only review, or repeat `--variant` to supply
up to four custom candidates. A baseline is always retained. Overrides have
the same syntax and G-code verification as the existing comparison commands:

```powershell
print-copilot quality tests/fixtures/tee.stl --variant "enable_support=1;support_type=tree(auto)"
```

That comparison changes support strategy. It does not prove that the support
contact marks or bridging will meet the desired finish. An existing G-code
file cannot be re-sliced by supplying preset names or overrides.

## What is checked

Every `CONFIG_BLOCK` key appears in the report inventory. Each entry is
labelled `rule_checked` or `listed_only`; the latter has no automatic
validation rule. An inventory is not a claim that every parameter is optimal.

Implemented rules cover:

- U1 printer identity, four nozzle slots and used tool indexes;
- nominal layer height against the used heads' own preset limits;
- explicit nozzle temperature targets during extrusion against the filament
  preset's declared temperature range;
- peak commanded extrusion demand against the filament volumetric limit,
  allowing 1% for G-code rounding; achieved flow/acceleration is not simulated;
- missing or disabled walls/top/bottom shells, intentional fuzzy skin and seam
  choices that need appearance review;
- the presence of bridge, overhang and support-interface toolpaths;
- geometric overhangs with supports disabled (heuristic, including spans that
  may be intended as bridges);
- project/head material mismatches, preserving head-assignment and slice notes;
- a disabled tower when multiple U1 heads extrude;
- explicit non-Custom extrusion endpoints against the printable polygon and
  height, and layer-marker count against the slicer summary.

Missing limits are marked unverified. They are not replaced with guessed
U1 temperatures, speeds or calibration values. Errors block acceptance;
warnings require inspection; heuristic findings identify risks to review.

The recommendation is explicitly a heuristic: among candidates without
detected errors, prefer fewer configuration warnings/unverified checks, then
the smaller nominal layer height, then slicer time. Feature-presence inspection
and unavoidable physical-calibration findings do not rank candidates.
The report shows the measured time/material cost of that preference. A failed
baseline is never replaced by a successful candidate, and failed candidates
retain their error and logs. No physical quality winner is claimed.

## Inspect the visual evidence

Open the generated `report.html` locally. For each successful variant it links
to an offline `toolpaths.html`, shows a PNG contact sheet of selected layers,
and includes model geometry views for model inputs. The interactive viewer
contains every parsed layer and path, with feature filters and colour modes
for feature, head and commanded speed. It needs no external service.

The PNG includes the first, middle and last nonempty layers plus layers with
the most bridge, overhang and support-interface segments when present. It is
explicitly a selection. Use `preview` / `preview_gcode` to inspect other
critical layers. Black dots indicate outer-wall path starts: seam candidates,
not a verified seam prediction. XY geometry is to scale; positions and Z are
in millimetres.

The parser handles linear motion, relative/absolute coordinates and extrusion,
G92 offsets and XY arcs. Display arcs use chords with 0.05 mm tolerance;
extrusion demand uses analytic arc length. Native `G3 ... P1` spiral travel
lifts are accepted. Multi-turn extrusion arcs, non-XY extrusion arcs,
unsupported coordinate transforms and incomplete geometry fail visibly.
Startup extrusion before the first layer and travel moves are excluded.
Firmware macros are opaque, so this is commanded geometry rather than a
simulation of the printer's actual movements. A resource limit stops with an
error rather than silently dropping paths; increase `--max-segments` explicitly
when appropriate. The offline payload has a 100 MB limit.

## Finish a review honestly

Successful analysis starts in `visual_review_pending`; an error produces
`blocked`. An MCP client must actually inspect the images and relevant layers
before calling `record_quality_review`. Merely running the tool does not
constitute a visual inspection.

To request changes after inspection:

```powershell
print-copilot quality-review <report.json> --variant baseline --decision revise --observations "The cap overhang needs a support/orientation comparison before acceptance."
```

Acceptance uses `--decision accept_with_limitations` and requires written
observations plus a repeated `--accept-finding CODE` for **every** warning and
unverified code on the chosen variant. Detected errors cannot be waived.
It records `reviewed_with_limitations`, with physical quality still unverified
and print authorization false. This is an observer's attestation, not an
automatic proof that someone viewed every layer.

Input, G-code and preview SHA-256 hashes and the sealed report inputs are
checked before recording a review. Editing the source, findings, PNG or
interactive toolpaths invalidates the review; generate a fresh report. Keep
the report and its evidence in place. New slicing runs always get new folders.

## What still needs physical evidence

Dryness, bed/nozzle condition, adhesion, ringing, layer shifts, U1 tool offsets,
real seams and roughness require hardware inspection and/or a test print.
Flow ratio, pressure advance, temperatures, cooling and retraction cannot be
called calibrated just because a preset contains values. U1 firmware macro
pre-extrusion is absent from file-only consumption measurements:
`TO VERIFY ON HARDWARE`.

The distinction follows the primary references checked on 2026-10-28:

- [Official OrcaSlicer calibration guide](https://github.com/OrcaSlicer/OrcaSlicer/wiki/calibration_guide)
  describes filament-specific physical calibration tests.
- [Official OrcaSlicer layer-height settings](https://github.com/OrcaSlicer/OrcaSlicer/wiki/quality_settings_layer_height)
  and [fuzzy-skin settings](https://github.com/OrcaSlicer/OrcaSlicer/wiki/others_settings_fuzzy_skin)
  explain geometric detail and deliberately textured surfaces.
- [Snapmaker U1 first-layer troubleshooting](https://wiki.snapmaker.com/en/snapmaker_u1/troubleshooting/first_layer_printing_troubleshooting_guide)
  identifies hardware, filament and calibration checks that files cannot establish.

Automatic photo diagnosis, mechanical/collision simulation, proof of physical
surface finish, variable-layer validation and an exhaustive search of all
settings are not implemented. Existing orientation and protected-region tools
remain available as separate actions; this workflow reviews the supplied
orientation and never silently rotates a painted project.
