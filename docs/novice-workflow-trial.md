# Novice workflow trial: GUI delivery failed, then fixed

On 2026-10-29 the housing STL was replayed through the existing product
commands using the brief: **"I need this component to assemble the
mechanism."** The result is a partial success followed by a failure at the
point where a beginner would open and use the delivered project in Orca.
The cause was then found and fixed; see
[Causes found and fix](#causes-found-and-fix-gui-check-passed). The sections
before it describe the original failed run as recorded.

## Scope and protocol

This is a known-case workflow trial, not a blind test, an independent expert
review, or a real beginner usability study. The model had already been
investigated in the preceding experiments. The replay copied the unmodified
STL to an anonymous `input.stl`; it did not supply a rotation, support answer
or numeric process override from those earlier experiments.

The brief was interpreted as functional use. U1 0.4 mm, the selected PLA
profiles and the user's previously authorized T0 white choice were already
available as context. A read-only printer discovery failed, so all preparation
continued explicitly offline. No loaded-spool RFID state was claimed.

A protocol with acceptance criteria was saved and hashed **before** running
the commands. Product source hashes and the input hash were recorded. No
production module or quality gate was modified to make the trial pass.
The research harness coordinates existing CLI commands, which share the MCP
tools' implementation; it is not a new shipped one-command preparation feature.

## What the workflow achieved

1. Read the selected U1 and filament profiles.
2. Called the product's orientation search with the quality goal and a
   two-candidate search budget. The product ranked and sliced the candidates,
   including the source orientation, with and without tree supports.
3. The returned recommendation was `+Z down`: a 180-degree rotation placing
   the housing's broad closed face on the bed. No rotation was passed in by
   the orchestrator. Fit, geometric stability and preservation of the rigidly
   transformed mesh were checked.
4. Called `suggest --purpose functional` on the recommended oriented STL.
   The existing advice module proposed **four walls and 25% infill**. These
   are labelled rules of thumb, not measured strength or load-specific design.
5. Sliced and audited the proposed configuration. The quality comparison
   recommended the functional candidate. Automatic supports remained enabled
   but generated **0.00 g** in the chosen orientation.
6. Exported a fresh 3MF, sliced the exported project itself and audited its
   paired G-code. The result was **3 h 33 min 55 s and 113.11 g**, with
   `wall_loops = 4`, `sparse_infill_density = 25%`, and only T0 used.

The exported G-code has no error-level audit findings and reaches
`visual_review_pending`. Warnings about bridges, commanded flow and physical
calibration remain. The peak flow warning is on a **0.004 mm inner-wall
segment at layer 19**, where the calculated demand is 23.451 mm³/s against a
20 mm³/s preset limit. Achieved flow/acceleration are not simulated; that
observation does not waive the warning. Layer 19 was included in an additional
preview for inspection.

No new technical answers were requested from the user during this replay.
That does not mean a first-time session could omit selecting an available
material or clarifying the intended use.

## Where it failed

The actual exported `functional-component.3mf` was opened using the product's
`open_in_orca` tool. Direct Windows accessibility and screenshot inspection
confirmed the project title, correct orientation, U1 and 0.16 mm process.
Only the Strength tab and Slice Plate controls were used; **no process values
were manually edited**.

| Evidence | Walls | Infill | Filament / time |
| --- | ---: | ---: | --- |
| Saved `Metadata/project_settings.config` | 4 | 25% | Not a measurement |
| Paired CLI G-code effective config | 4 | 25% | 113.11 g / 3 h 33 min 55 s |
| Orca's visible global process panel | 2 | 15% | Not a measurement |
| Slice performed in that Orca window | 2 in visible panel | 15% in visible panel | 96.20 g / about 2 h 56 min displayed |

The project's object/part metadata contains **no wall or infill overrides**
that could explain the global-panel difference. The GUI slice's materially
different summary confirms that this is more than an assumption based on a
window title or on the ZIP contents. The cause of the export/import mismatch
has not yet been isolated; no claim is made about which component resets the
settings. The GUI's time display has minute precision and is not presented as
an exact number of seconds.

**The novice delivery criterion fails.** A user opening and re-slicing the
delivered 3MF would not obtain the configuration the preparation workflow
selected. Opening a file successfully and finding the expected JSON inside it
are insufficient to declare the project ready for a beginner.

The paired CLI G-code's settings and measurements remain valid evidence for
that file, but do not repair the GUI delivery failure. The trial is not a
validation of physical strength, fit, assembly or the globally best possible
configuration. No print was started.

## Retained evidence and reproduction

Evidence is under `runs/novice-trial-20261029-162644/` (ignored by Git):

- `protocol.json`, its recorded SHA-256 and product/input hashes.
- `commands.json` and raw stdout/stderr for each step.
- Orientation ranking, four actual orientation/support slices and the
  product's recommendation.
- Functional proposals, candidate comparison, exported-file audit and
  complete offline previews.
- `exports/functional-component.3mf` and its paired G-code.
- `ui-observation.json`: explicitly labelled transcription of direct UI
  observation, not a fabricated automated UI assertion.
- `flow-warning-detail.json`: exact commanded segments behind the warning.
- `result.json`: final overall status **failed**, retaining the successful
  CLI steps and the unsuccessful GUI delivery separately.

Replay the command workflow from the repository root:

```powershell
.venv/Scripts/python.exe research/showcase/novice_trial.py research/showcase/models/planetarium/Base/base_housing.stl
```

The harness intentionally leaves the GUI outcome pending. It must be checked
in the real app; a passing CLI run alone is not a passing end-to-end trial.
`record_novice_ui_observation.py` records this specific observed session and
must not be used as an automatic verifier for a new replay.

GUI reproduction procedure after a fresh export:

1. Export an STL with `--head T0 --set wall_loops=4 --set sparse_infill_density=25%`.
2. Open the new 3MF in Snapmaker Orca.
3. Compare its visible Strength settings with the saved project JSON and
   paired G-code configuration.
4. Slice in the GUI and compare its output. Do not change the settings to
   conceal a mismatch, and do not print as part of this check.

The acceptance target for a fix is that the opened project and its GUI slice
retain the selected settings, not merely that the metadata contains them.
The original failing case should be preserved and a fresh post-fix replay
recorded separately. Unit tests for ZIP contents alone cannot lock down this
failure at the GUI import boundary.

Ruff and the 66 targeted mesh/G-code/rendering/settings tests passed after
recording the trial. That automated coverage does not verify GUI import and
does not change the failed end-to-end verdict. The previously recorded full
pytest MCP stall remains unresolved.

## Causes found and fix (GUI check passed)

Two defects in the generated `project_settings.config` combined:

1. **No `different_settings_to_system`.** When the app opens a project,
   `PresetBundle::load_config_file_config` passes each preset's entry of that
   key to `PresetCollection::load_external_preset` (Snapmaker Orca v2.4.1),
   which resets every key not listed there to the named preset's value. This
   is what reset the 25% infill to 15%.
2. **`version` written as a per-filament list.** The app log for the opened
   project reports `load_from_json ... type must be string, but is array`.
   The loader reads keys in sorted order and stops there, so keys sorted
   after `version`, such as `wall_loops`, are not loaded at all. With only the
   first fix applied, the GUI showed 25% infill but still 2 walls.

`project_settings` now lists the keys that differ from the named presets in
`different_settings_to_system` and writes `version` as one string (from the
process or printer preset), not a per-filament list.

Post-fix replay of the same oriented STL with the same overrides
(`runs/novice-fix-20261029/v2/`, ignored by Git):

| Evidence | Walls | Infill | Filament / time |
| --- | ---: | ---: | --- |
| Saved project settings | 4 | 25% | Not a measurement |
| Paired CLI G-code | 4 | 25% | 113.11 g / 3 h 33 min 55 s |
| Orca's visible Strength panel (UI Automation read) | 4, marked modified | 25%, marked modified | Not a measurement |
| G-code from the GUI's own slice (Slice plate, no edits) | 4 | 25% | 113.11 g / 3 h 33 min 55 s |

The app log shows no config parse error for this project. The GUI slice's
G-code was taken from the app's temporary folder and kept as `gui-slice.gcode`.
The 3D view could not be screen-captured in this session (masked), so the
values come from UI Automation and the GUI's G-code, not from a screenshot.

**The novice delivery criterion now passes for this case:** the reopened
project and its GUI slice keep the configuration the workflow chose. A
separate repeat of the CLI export measured 113.03 g / 12824 s with identical
geometry and effective configuration, so expect small run-to-run variation
between slices. This remains a single known-case replay, not a user study,
and no print was started.

## Visual toolpath review of the delivered setup

After the fix, the delivered G-code (`v2/functional-component.gcode`) was
reviewed with `quality` and `preview` (layers 1, 19, 25, 26, 31, 101, 140,
199, 200), and outer-wall start points were extracted from every layer. Then
three alternatives were sliced on top of the chosen setup with `quality
--variants-file` (`runs/novice-fix-20261029/review/`). This is a review of
commanded toolpaths; nothing was printed, so surface finish, ringing and
calibration remain unverified.

What the toolpaths show:

- **Seam.** `seam_position = aligned` puts the outer-wall start of the
  external contour in the lower concave notch where the two circles meet, on
  every layer (it drifts from X 169.6 / Y 98 at layer 1 to X 172.4 / Y 88 at
  layer 200 as the notch moves). That is the most hidden place on this
  outline; the inside contour's seam sits in the upper notch from layer 180
  up. The four small tabs at layers 120-160 get their own starts.
- **Side walls are about 2.0 mm thick.** At layer 101 the outer-wall
  centrelines are 1.6 mm apart (0.42 mm line width). With the classic wall
  generator each layer is one outer and one inner wall per side plus a
  gap-infill strip in the middle (557 mm of gap infill per layer, 67.8 m in
  total). `wall_loops = 4` therefore only adds walls where the part is
  thicker (floor and rim); on the sides it cannot fit.
- **Bridges.** Only layer 25 has bridging: an internal bridge over the
  sparse infill of the 5 mm floor, covered by solid layers and the top surface
  at layer 31. It is not an exposed surface.
- **Flow warning.** Unchanged in every variant (23.45 mm3/s peak against
  20 mm3/s, on a 0.004 mm inner-wall segment at layer 19).
- **Brim.** `brim_type = auto_brim` adds a 5 mm brim on layer 1. Because the
  part is flipped, that layer is the housing's closed outer face, so the brim
  must be removed from its visible edge.

Measured alternatives (slicer figures):

| Variant | Time | Filament | Gap infill | Note |
| --- | ---: | ---: | ---: | --- |
| Chosen (4 walls, 25%) | 12835 s | 113.11 g | 67.8 m | Delivered setup |
| + `wall_generator = arachne` | 12987 s | 112.92 g | 0.2 m | Sides become continuous variable-width walls; seam unchanged |
| + `brim_type = no_brim` | 12605 s | 111.87 g | 67.8 m | Removes the brim from the visible face edge; adhesion not tested |
| + `seam_slope_type = external` | not sliced | | | Rejected: the U1 process preset does not define the key |

Arachne removes the gap-infill strips from the side walls at +152 s, and its
previews show clean continuous walls; whether that is visibly or
mechanically better needs a print. Removing the brim is a judgement about
adhesion on a 14650 mm2 contact face; it was not tried physically. The
delivered project was not changed by this review.

## Delivered with Arachne walls

On the maintainer's choice, the project was re-exported with
`wall_generator = arachne` added and the brim left on
(`runs/novice-fix-20261029/v3-arachne/`). The saved settings list
`enable_support;sparse_infill_density;wall_generator;wall_loops` as changed
from the process preset, and the app log shows no config parse error.

| Evidence | Wall generator | Walls | Infill | Filament / time |
| --- | --- | ---: | ---: | --- |
| Paired CLI G-code | Arachne | 4 | 25% | 112.92 g / 3 h 36 min 27 s |
| Orca's Quality and Strength panels (UI Automation read) | Arachne | 4 | 25% | Not a measurement |
| G-code from the GUI's own slice (Slice plate, no edits) | Arachne | 4 | 25% | 112.92 g / 3 h 36 min 27 s |

The reopened project keeps the selected configuration, and the GUI slice
measures the same as the CLI. No print was started.
