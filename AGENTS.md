# AGENTS.md

Guidance for AI coding agents (and humans) working on **Snapmaker Print Copilot**.

Start with `HANDOFF.md`: it holds the verified findings, the open questions and
the milestone plan. This file holds the rules that do not change.

## What this project is

An open-source MCP server (plus a plain CLI) that lets any MCP client — Claude
Code, Codex, Claude Desktop, others — prepare prints for the **Snapmaker U1**
toolchanger with **Snapmaker Orca** as the slicing engine:

- read the user's Snapmaker Orca presets and projects;
- analyse a model and score candidate orientations and support strategies;
- slice variants headlessly with the Snapmaker Orca command line and compare
  time, filament, supports, tool changes and purge;
- write the chosen setup as a `.3mf` project and open it in Snapmaker Orca so the
  user sees and adjusts it in the app;
- talk to the printer through Moonraker (status, upload, start), only with
  explicit user confirmation.

The value is **U1-specific knowledge** (four tool heads, purge matrix, U1
profiles). A generic "OrcaSlicer MCP" already exists several times over; do not
build another generic one.

## Non-negotiable rules

1. **Do not invent Snapmaker U1 or Snapmaker Orca details.** Ports, endpoints,
   macro names, preset keys, file locations, CLI behaviour and firmware behaviour
   must be verified (on the real printer, in the installed app, or in Snapmaker's
   public source). Unverified values must be configurable and marked with the
   exact string `TO VERIFY ON HARDWARE` (printer) or `TO VERIFY IN SNAPMAKER ORCA`
   (slicer) in a comment next to them.
2. **Nothing physical happens without confirmation.** Starting a print, heating,
   moving axes, sending G-code or changing printer config must go through a
   confirmation gate that shows exactly what will be sent. Read-only tools need no
   gate. Cancel and heater-off are never gated.
3. **Never modify the user's Snapmaker Orca installation or presets in place.**
   Read from `%APPDATA%\Snapmaker_Orca`, write only to a project-owned working
   directory or to new files the user asked for. Never overwrite a user `.3mf`.
4. **Report measured numbers only.** Times, grams, support volume and purge come
   from the slicer output or the G-code, never from estimates the code invented.
   Label anything that is a heuristic score as such.
5. **Fail loudly.** If the slicer crashes, a preset cannot be resolved, or a
   result fails a sanity check, return a clear error with the reason. Never fall
   back silently to a different preset or printer.
6. **Usable without AI.** Every MCP tool has an equivalent CLI command, so judges
   and users can try the project without an AI subscription.
7. **English only** for code, comments, docstrings, docs and commit messages.
   (The maintainer talks Italian in chat; the repository stays English.)
8. **No AI attribution in git history.** No `Co-Authored-By` trailers,
   "Generated with ..." lines or other AI-tool attribution in commits, PRs or
   GitHub content.
9. **License:** GPL-3.0-or-later (to be confirmed by the maintainer before the
   first public push). Snapmaker Orca is AGPL-3.0: invoke it as an external
   program, do not copy its source into this repository. Do not add dependencies
   with incompatible licenses.

## Planned repository layout

```
src/print_copilot/
  orca/          locate install, read + flatten presets, CLI runner, 3mf writer
  model/         mesh loading, orientation candidates, overhang/support scoring
  gcode/         parse slicer header + body: time, grams, tool changes, temps
  printer/       Moonraker client, fake Moonraker, confirmation gates
  mcp_server.py  MCP tools (thin wrappers over the modules above)
  cli.py         `print-copilot` command line, same features as the MCP tools
tests/           pytest, fast; slicer-dependent tests skip when Orca is absent
research/        probes and notes from verification sessions (not shipped)
docs/            usage, MCP client setup, verified facts, hardware TODO
```

MCP tools are thin: all logic lives in the modules so the CLI and tests use the
same code paths.

## Conventions

- Python ≥ 3.11, `pyproject.toml`, ruff for lint + format, pytest.
- Paths passed to `snapmaker-orca.exe` must be Windows-style (`C:/...` or
  `C:\...`). MSYS paths (`/c/...`) are rejected by the CLI.
- Units: millimetres, seconds, grams. Name ambiguous variables with the unit
  (`time_s`, `filament_g`, `purge_mm3`).
- Results are dataclasses or pydantic models, not bare dicts.
- Every slicer run gets its own fresh output directory and keeps its log, so a
  failure can be reproduced.

## Commands (once the package exists)

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
print-copilot --help
```

Before you finish a change, run ruff and pytest locally.
