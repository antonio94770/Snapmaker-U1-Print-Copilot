# Changelog

All notable changes to Snapmaker Print Copilot. Versions follow
[Semantic Versioning](https://semver.org/); before 1.0, minor versions may
change tool and command behaviour.

## [0.1.0] - 2026-10-31

First release. Windows, Snapmaker Orca 2.4.1, Snapmaker U1 firmware 2.0.0.

### Added

- **Snapmaker Orca core:** finds the installation and user data, reads and
  flattens machine, process and filament presets (including the presets
  bundled with 2.4.1), slices headlessly in a fresh folder per run with its
  log, and reads time, grams and filament from the G-code. Preset keys can be
  overridden; unknown or silently replaced values are rejected.
- **Model analysis:** STL and 3MF meshes, candidate orientations with a
  labelled overhang heuristic, protected regions for surfaces that must print
  cleanly, rendered views for the agent, and an orientation search that
  slices the best candidates with and without supports.
- **U1 multi-colour projects:** reads 3MF projects from Snapmaker Orca, Bambu
  Studio, MakerWorld and Printables (plates, object and part filaments,
  painting), assigns filaments to the four heads from the loaded colours,
  measures tool changes, prime tower, purge and support material from the
  toolpaths, compares strategies, and reports settings that objects and
  parts set themselves.
- **Settings from the model:** proposes a few settings worth slicing as
  variants (Arachne walls, finer layers, walls and infill, brim) from the
  model's shape and the print's purpose. The proposals are heuristics.
- **Quality review:** audit of the effective settings, model and toolpath
  images, layer previews with an offline HTML viewer, and a recorded review
  that is never permission to print.
- **Export:** writes the chosen setup as a new 3MF, slices that same file,
  puts the G-code next to it and opens it in Snapmaker Orca with the chosen
  settings.
- **Printer (Moonraker):** mDNS discovery, status with the spools read by
  each head's RFID reader, file list, upload, a start plan with per-head
  checks and a single-use token, start only after confirmation, cancel and
  heaters-off without a gate, and a fake U1 for testing.
- **24 MCP tools** and a `print-copilot` command for each, plus
  `print-copilot connect` for Claude Code, Codex, Claude Desktop, VS Code,
  Cursor and Gemini CLI.

### Verified

- Upload, start and RFID head mapping on a real U1 (two prints). Cancel and
  heaters-off have not been sent to a printer yet. See
  [docs/verified-facts.md](docs/verified-facts.md).

[0.1.0]: https://github.com/antonio94770/Snapmaker-U1-Print-Copilot/releases/tag/v0.1.0
