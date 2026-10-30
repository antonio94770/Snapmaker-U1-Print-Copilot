# Using Print Copilot from an AI agent (MCP)

`print-copilot-mcp` is an MCP server that talks over **stdio**. Any MCP client
can start it; this page covers Claude Code, Codex, Claude Desktop, VS Code
(GitHub Copilot), Cursor and Gemini CLI, and the generic form for other
clients.

## 1. Install

Install as described in the README's [Install](../README.md#install) section:

```powershell
uv tool install https://github.com/antonio94770/Snapmaker-U1-Print-Copilot/archive/refs/heads/main.zip
```

The easy way to connect an agent is one command, for example
`print-copilot connect claude-code` (also `codex`, `claude-desktop`,
`vscode`, `cursor`, `gemini`). It writes what the sections below describe,
with the right path, keeps the other servers and a `.bak` copy of the file;
`--dry-run` shows the change without making it.

To do it by hand: `print-copilot connect cursor --dry-run` prints the path of
the server executable. With uv's default folders it is
`%APPDATA%\uv\tools\snapmaker-print-copilot\Scripts\print-copilot-mcp.exe`;
from a source checkout it is `<repo>\.venv\Scripts\print-copilot-mcp.exe`.
Use its **absolute path** in the client configs below; the examples assume
the uv install for a user called `you`.

Optional environment variables (the same ones the CLI reads):

| Variable | Default | Meaning |
| --- | --- | --- |
| `PRINT_COPILOT_ORCA_EXE` | `C:\Program Files\Snapmaker_Orca\snapmaker-orca.exe` | Snapmaker Orca executable |
| `PRINT_COPILOT_ORCA_DATA` | `%APPDATA%\Snapmaker_Orca` | Snapmaker Orca presets and `Snapmaker_Orca.conf` |
| `PRINT_COPILOT_WORK_DIR` | `%LOCALAPPDATA%\print-copilot\runs` | where each slice gets its run folder |
| `PRINT_COPILOT_EXPORT_DIR` | `%LOCALAPPDATA%\print-copilot\exports` | where `export_project` writes when no output path is given |
| `PRINT_COPILOT_PRINTER_URL` | found through mDNS | fixed printer address, e.g. `http://192.168.1.50`; only needed when discovery does not work on your network |
| `PRINT_COPILOT_PRINTER_NAME` | none | device name of the U1 to use when several answer |

## 2. Tools

| Tool | What it does | CLI equivalent |
| --- | --- | --- |
| `list_presets` | machine / process / filament presets, optionally only those compatible with a printer | `print-copilot presets` |
| `get_current_setup` | printer, process, filament and colour per tool head, purge matrix, as selected in Snapmaker Orca | `print-copilot setup` |
| `slice` | slices a model headlessly with optional setting overrides and returns the slicer's time, grams per head and layers | `print-copilot slice` |
| `compare_variants` | slices one model once per override set and returns a comparison table | `print-copilot compare` |
| `analyze_model` | ranks candidate orientations of an STL/3MF with a geometric overhang heuristic (no slicing) | `print-copilot analyze` |
| `suggest_settings` | proposes the few process settings the model's shape and purpose bear on (thin features: wall generator; tall on a small base: brim; visual: layer height; functional: walls and infill), with the evidence for each, as variants for `review_print` (heuristic, no slicing) | `print-copilot suggest` |
| `optimize_orientation` | slices the best candidate orientations with each support mode (none / normal / tree) and recommends one for the goal (speed, quality, min_support) | `print-copilot orient` |
| `render_model` | returns a PNG of labelled views (as loaded or in a print orientation) with bed contact, supports and protected surfaces coloured, so the agent can look at the model | `print-copilot render` |
| `inspect_project` | reads a 3MF project (Snapmaker Orca, Bambu Studio, PrusaSlicer): plates, objects and their size in mm, whether each plate fits the bed, the filaments each plate uses, the U1 tool head proposed for each from the loaded colours, settings the objects set themselves, and saved settings that differ from the process preset | `print-copilot project` |
| `slice_project` | slices one plate of a project on the U1 with that head assignment and returns time, grams per head, tool changes, prime tower, purge and support grams | `print-copilot slice-project` |
| `compare_strategies` | slices one plate with the prime tower on/off and one object at a time (or any variants given), and compares the measured results | `print-copilot strategies` |
| `export_project` | writes the plate (or an STL) as a new 3MF for Snapmaker Orca with the U1 presets and heads, slices that 3MF into a `.gcode` next to it, and opens it in the app (default) | `print-copilot export-project` (`--open`) |
| `open_in_orca` | opens an existing 3MF in Snapmaker Orca | `print-copilot open` |

Printer tools (Moonraker on the U1; the printer is found on the network through
mDNS, once per server process, and found again if it stops answering):

| Tool | What it does | CLI equivalent |
| --- | --- | --- |
| `discover_printers` | Snapmaker printers answering on the LAN, with their current address | `print-copilot printer discover` |
| `printer_status` | state, temperatures, active head, the spool each head's RFID reader identified, and while printing the layer, Z and an estimate of the time left | `print-copilot printer status` |
| `list_printer_files` | G-code files stored on the printer | `print-copilot printer files` |
| `upload_to_printer` | copies a G-code file sliced for the U1; refuses other printers and overwrites; never starts it | `print-copilot printer upload` |
| `plan_print_start` | sends nothing: returns the exact start request, the file's slicer figures, per-head checks, whether a recorded quality review (`review_report`) covers the file, and a one-time token | `print-copilot printer start --dry-run [--review-report report.json]` |
| `start_print` | starts a planned file; asks the user again through MCP elicitation when the client supports it | `print-copilot printer start` (typed confirmation) |
| `cancel_print`, `heaters_off` | safety actions, never gated | `print-copilot printer cancel`, `printer heaters-off` |

Without a printer, `print-copilot printer fake` runs a fake U1 Moonraker on
`http://127.0.0.1:7125`; point `PRINT_COPILOT_PRINTER_URL` at it.

The slicing tools never talk to the printer. The project tools (`inspect_project`,
`slice_project`, `compare_strategies`, `export_project`, `review_print`) read
the spool colours from the U1's RFID readers (read-only; `use_printer=false`
skips it) and fall back to the app's selection with a note. Overrides use preset JSON keys
(`sparse_infill_density`, `wall_loops`, ...). A key without a prefix is a
process setting; `machine.<key>` and `filament.<key>` target the machine and
filament presets (filament overrides apply to every head). Keys the preset does
not define are rejected.

`analyze_model` numbers (overhang area, support volume) are a labelled
heuristic, and so are the `suggest_settings` proposals: they say which
settings are worth slicing, and `review_print` measures what they cost. `optimize_orientation` reports the slicer's time and grams and the
support material measured from the G-code's `;TYPE:Support` toolpaths; it
writes each rotated model as an STL you can open in Snapmaker Orca.

For models where one surface matters (a miniature's face, text), the agent
renders the model, chooses `protect` regions from the image (direction and/or
a box in mm read off the grid), runs `optimize_orientation` with
`goal="quality"` and renders the recommended `down_vector` to check it.

Multi-colour projects: the agent starts with `inspect_project`, tells the user
which colour to load on which head when the loaded ones differ (the notes
give the CIE76 colour distance, a heuristic), slices or compares strategies,
and finishes with `export_project` (it opens the project by default) so the user sees the
result in Snapmaker Orca. A one-colour plate or an STL is never put on a head
automatically: the tool error lists the loaded spools, the agent asks the
user which one to use and passes `head`. `export_project` slices the project
it writes; the agent uploads that `.gcode` and tells the user the `.3mf` is
the file to reopen for what was printed. A plate that needs more colours than the U1 has
heads fails with the closest colour pairs; `mapping` or `reduce_colours`
decide which colours share a head.

Slicing a small model takes about a second, but large models can take minutes.
Raise the client's tool timeout if slices get cut off (see below).

## 3. Claude Code

```bash
claude mcp add --scope user snapmaker-print-copilot -- "C:\Users\you\AppData\Roaming\uv\tools\snapmaker-print-copilot\Scripts\print-copilot-mcp.exe"
```

Add `--env PRINT_COPILOT_WORK_DIR=D:\print-runs` (repeatable) before the
server name to set environment variables. Check with `claude mcp list`, or
`/mcp` inside a session. For long slices, start Claude Code with a larger tool
timeout, e.g. `MCP_TOOL_TIMEOUT=600000` (milliseconds).

## 4. Codex

Add to `~/.codex/config.toml` (`%USERPROFILE%\.codex\config.toml` on Windows):

```toml
[mcp_servers.snapmaker-print-copilot]
command = 'C:\Users\you\AppData\Roaming\uv\tools\snapmaker-print-copilot\Scripts\print-copilot-mcp.exe'
args = []
tool_timeout_sec = 600

# Optional:
# [mcp_servers.snapmaker-print-copilot.env]
# PRINT_COPILOT_WORK_DIR = 'D:\print-runs'
```

Single quotes make TOML keep the backslashes as they are. Restart Codex and
check the server with `/mcp`.

## 5. Claude Desktop

Settings → Developer → Edit Config opens `claude_desktop_config.json`. The
Microsoft Store build (4.29, checked 2026-10-29) keeps it in
`%LOCALAPPDATA%\Claude-Data\claude_<id>\` and leaves only a
`.user-data-left.*` marker in `%APPDATA%\Claude`, which it no longer reads;
older builds use `%APPDATA%\Claude\claude_desktop_config.json`. `connect`
picks the folder the installed build uses. Add:

```json
{
  "mcpServers": {
    "snapmaker-print-copilot": {
      "command": "C:\\Users\\you\\AppData\\Roaming\\uv\\tools\\snapmaker-print-copilot\\Scripts\\print-copilot-mcp.exe",
      "args": [],
      "env": {}
    }
  }
}
```

Restart Claude Desktop; the tools appear under the tools menu of a new chat.

## 6. VS Code (GitHub Copilot)

VS Code reads two formats (see its
[MCP configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)).
For every workspace, use the Copilot user file `~/.copilot/mcp-config.json`
(`%USERPROFILE%\.copilot\mcp-config.json`, or `mcp-config.json` in
`COPILOT_HOME` when that is set), which the GitHub Copilot CLI reads too:

```json
{
  "mcpServers": {
    "snapmaker-print-copilot": {
      "command": "C:\\Users\\you\\AppData\\Roaming\\uv\\tools\\snapmaker-print-copilot\\Scripts\\print-copilot-mcp.exe",
      "args": [],
      "env": {}
    }
  }
}
```

The VS Code format (`.vscode/mcp.json` or the user-profile `mcp.json`) uses a
top-level `servers` object instead, with `"type": "stdio"` in the entry. The
command **MCP: Add Server** writes either one for you. Check the server with
**MCP: List Servers**, then use Copilot Chat in agent mode.

## 7. Cursor

Add to `~/.cursor/mcp.json` (`%USERPROFILE%\.cursor\mcp.json`) for every
project, or to `.cursor/mcp.json` in one project:

```json
{
  "mcpServers": {
    "snapmaker-print-copilot": {
      "type": "stdio",
      "command": "C:\\Users\\you\\AppData\\Roaming\\uv\\tools\\snapmaker-print-copilot\\Scripts\\print-copilot-mcp.exe",
      "args": [],
      "env": {}
    }
  }
}
```

The server then appears in Cursor's MCP settings, where it can be turned on
and off. See [Cursor's MCP documentation](https://cursor.com/docs/mcp).

## 8. Gemini CLI

```bash
gemini mcp add --scope user --timeout 600000 snapmaker-print-copilot "C:\Users\you\AppData\Roaming\uv\tools\snapmaker-print-copilot\Scripts\print-copilot-mcp.exe"
```

This writes the server into `~/.gemini/settings.json` (`--scope project`
writes `.gemini/settings.json` instead). `--timeout` is in milliseconds; add
`-e KEY=value` for environment variables. Do not add `--trust`: it skips
Gemini's confirmation of each tool call. Gemini CLI turns MCP servers off in
folders you have not trusted ("MCP servers are configured but disabled because
this folder is untrusted"): accept its trust prompt in the folder you start it
from. Check with `/mcp` inside a session.
See the [Gemini CLI MCP documentation](https://geminicli.com/docs/tools/mcp-server/).

## 9. Other clients

Most other MCP clients take the same `mcpServers` block as Claude Desktop
above: a stdio server whose `command` is the absolute path of
`print-copilot-mcp.exe`, with no arguments. Look up where your client keeps
that file, and raise its tool timeout if it has one.

## 10. Checking the server without an AI client

The MCP Inspector (needs Node.js) starts the server and lets you call each
tool by hand:

```bash
npx @modelcontextprotocol/inspector "C:\Users\you\AppData\Roaming\uv\tools\snapmaker-print-copilot\Scripts\print-copilot-mcp.exe"
```

## 11. Example prompts

- "What printer and filaments do I have selected in Snapmaker Orca?"
- "Slice `C:\models\bracket.stl` with my current setup and tell me time and
  grams per tool head."
- "Find the best orientation for `C:\models\bracket.stl`, compare tree and
  normal supports, and tell me how much support material each needs."
- "This is a miniature: look at it, find the face, and pick the
  orientation that keeps the face free of support marks."
- "Compare 15%, 25% and 40% infill and 2 vs 4 walls for the bracket and
  recommend the cheapest variant that keeps 4 walls."
- "I downloaded `C:\models\mask.3mf` from MakerWorld. Which plates can my U1
  print with the colours I have loaded, and what should I swap?"
- "Slice plate 2 of the mask, compare it with and without the prime tower,
  then write the faster version as a project and open it in Snapmaker Orca."

## Quality workflow

Before presenting a setup as reviewed, use `review_print` to create the
effective-settings audit, comparison and image evidence. Inspect the model
and toolpath images; `preview_gcode` can render specific critical layers.
Call `record_quality_review` only after an actual visual inspection, with
written observations and explicit acknowledgement of remaining warning and
unverified finding codes. It is not a physical print confirmation.

The same actions are available as `quality`, `preview` and `quality-review`
from the CLI. [Quality review details](quality-review.md) describe the exact
coverage and limitations. Do not claim that inventoried settings were all
validated or that physical surface quality has been measured.
