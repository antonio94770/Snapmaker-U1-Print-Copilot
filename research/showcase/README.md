# Complex-model showcase

The [novice workflow trial](../../docs/novice-workflow-trial.md) completed
automatic preparation but **failed GUI delivery**: the selected four walls /
25% infill appeared in project JSON and CLI G-code, while Orca opened and
re-sliced with two walls / 15%. Preserve that failure when evaluating the
product; the preceding measured optimization demos do not establish that
the novice-facing workflow is ready.

The follow-up [optimization results](../../docs/optimization-results.md)
restore the saved Haku process, compare four process variants and compare
four housing orientation/support combinations. The controlled Haku candidate
saves 20.80 g and 39 min 35 s. See the linked report for baseline definitions,
path checks, exported review projects and unresolved quality findings.

Status: both inputs acquired and six real slices completed on 2026-10-29.
The 49 planetarium STL files were inventoried; four representative parts were
sliced on T0 white. Haku was sliced at 0.20 and 0.16 mm with tree supports.
See [measured results and limitations](../../docs/complex-model-trials.md).

The 42-second GIF and MP4 are in `docs/images/complex-models.*`. They are
assembled from actual CLI measurements and renders. The Haku quality reviews
remain blocked: conflicting layer metadata at 0.20 mm, and a full preview
larger than 100 MB at 0.16 mm. No physical print was performed.

## Mechanical Planetarium - Tuned

- Creator: HackerJack42, based on the original design by Zippitybamba.
- Source: https://www.printables.com/model/47796-mechanical-planetarium-tuned/files
- License displayed on the source page: CC BY-NC-SA 4.0.
- Observed on 2026-10-29: 52 model files grouped into Source Files, Base,
  Misc, Mercury, Venus, Earth & Moon, Mars, Jupiter and Saturn. The page offers
  a 9 MB model archive and a separate batch-file download.
- The batch file is source material only; do not execute downloaded scripts.
- Test purpose: functional parts, small gear teeth, thin axles and the effect
  of orientation and process settings on toolpaths.
- Completed: archive extraction, inventory, four T0 slices, quality reports
  and inspection of selected layers. Assembly tolerances remain unverified.

## Haku Dragon Diorama (Spirited Away)

- Creator: Lynns.
- Source: https://space.snapmaker.com/en/model/45404-haku-dragon-diorama-spirited-away#profileId-49095
- License displayed on the source page: CC BY-NC-ND 4.0.
- Observed on 2026-10-29: one four-colour print profile, named
  `Basic model - multicolor`; downloading its 3MF requires login.
- The description recommends a 0.4 mm nozzle, 0.16 or 0.12 mm layers, three
  walls, 10–15% gyroid infill, tree supports and a 5 mm outer brim. These are
  author recommendations, not verified effective settings from the 3MF.
- Test purpose: imported project settings, four-head assignment, supports
  around delicate features, tool changes and prime-tower comparison.
- Completed: user-supplied download, inspection, explicit T0-T3 mapping,
  measured layer-height comparison and selected-layer views. The creator's
  text differs from the saved project settings; this trial preserves the
  saved settings except for the compared nominal layer height.

## Evidence and media

Keep downloaded models and generated 3MF/G-code out of the GPL source tree's
distributed assets; preserve their original licenses and attribution. Before
publishing media, check its permitted use under each model's license.

For each actual slice retain the input hash, exact CLI command, preset names,
effective settings, output directory, slicer log and measured metrics. Use
actual model renders and toolpath previews for the demonstration. Label
author-provided figures separately from local measurements, heuristic scores
as heuristics, and slicing results as unverified by a physical print.

The README demo shows model inspection, settings differences, measured
variant comparisons and selected critical layers. Do
not use the presence of a preview or a quality review as permission to start
a printer. This showcase request does not authorize physical printer actions.
