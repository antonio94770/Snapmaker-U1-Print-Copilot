# Using Print Copilot from an AI agent (MCP)

`print-copilot-mcp` is an MCP server that talks over **stdio**. Any MCP client
can start it; this page covers Claude Code, Codex and Claude Desktop.

## 1. Install

From the repository folder (Windows, PowerShell or Git Bash):

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e .
```

The server executable is then
`<repo>\.venv\Scripts\print-copilot-mcp.exe`. Use its **absolute path** in the
client configs below; the examples assume the repository is at
`C:\Users\you\Snapmaker_Print_Copilot`.

Optional environment variables (the same ones the CLI reads):

| Variable | Default | Meaning |
| --- | --- | --- |
| `PRINT_COPILOT_ORCA_EXE` | `C:\Program Files\Snapmaker_Orca\snapmaker-orca.exe` | Snapmaker Orca executable |
| `PRINT_COPILOT_ORCA_DATA` | `%APPDATA%\Snapmaker_Orca` | Snapmaker Orca presets and `Snapmaker_Orca.conf` |
| `PRINT_COPILOT_WORK_DIR` | `%LOCALAPPDATA%\print-copilot\runs` | where each slice gets its run folder |

## 2. Tools

| Tool | What it does | CLI equivalent |
| --- | --- | --- |
| `list_presets` | machine / process / filament presets, optionally only those compatible with a printer | `print-copilot presets` |
| `get_current_setup` | printer, process, filament and colour per tool head, purge matrix, as selected in Snapmaker Orca | `print-copilot setup` |
| `slice` | slices a model headlessly with optional setting overrides and returns the slicer's time, grams per head and layers | `print-copilot slice` |
| `compare_variants` | slices one model once per override set and returns a comparison table | `print-copilot compare` |
| `analyze_model` | ranks candidate orientations of an STL/3MF with a geometric overhang heuristic (no slicing) | `print-copilot analyze` |
| `optimize_orientation` | slices the best candidate orientations with each support mode (none / normal / tree) and recommends one for the goal (speed, quality, min_support) | `print-copilot orient` |
| `render_model` | returns a PNG of labelled views (as loaded or in a print orientation) with bed contact, supports and protected surfaces coloured, so the agent can look at the model | `print-copilot render` |
| `inspect_project` | reads a 3MF project (Snapmaker Orca, Bambu Studio, PrusaSlicer): plates, objects, the filaments each plate uses, and the U1 tool head proposed for each from the loaded colours | `print-copilot project` |
| `slice_project` | slices one plate of a project on the U1 with that head assignment and returns time, grams per head, tool changes, prime tower and purge grams | `print-copilot slice-project` |
| `compare_strategies` | slices one plate with the prime tower on/off and one object at a time, and compares the measured results | `print-copilot strategies` |
| `export_project` | writes the plate as a new 3MF for Snapmaker Orca with the U1 presets and heads, and can open it in the app | `print-copilot export-project` |

None of these tools talk to the printer. Overrides use preset JSON keys
(`sparse_infill_density`, `wall_loops`, ...). A key without a prefix is a
process setting; `machine.<key>` and `filament.<key>` target the machine and
filament presets (filament overrides apply to every head). Keys the preset does
not define are rejected.

`analyze_model` numbers (overhang area, support volume) are a labelled
heuristic. `optimize_orientation` reports the slicer's time and grams and the
support material measured from the G-code's `;TYPE:Support` toolpaths; it
writes each rotated model as an STL you can open in Snapmaker Orca.

For models where one surface matters (a miniature's face, text), the agent
renders the model, chooses `protect` regions from the image (direction and/or
a box in mm read off the grid), runs `optimize_orientation` with
`goal="quality"` and renders the recommended `down_vector` to check it.

Multi-colour projects: the agent starts with `inspect_project`, tells the user
which colour to load on which head when the loaded ones differ (the notes
give the CIE76 colour distance, a heuristic), slices or compares strategies,
and finishes with `export_project` (`open_in_app=true`) so the user sees the
result in Snapmaker Orca. A plate that needs more colours than the U1 has
heads fails with the closest colour pairs; `mapping` or `reduce_colours`
decide which colours share a head.

Slicing a small model takes about a second, but large models can take minutes.
Raise the client's tool timeout if slices get cut off (see below).

## 3. Claude Code

```bash
claude mcp add --scope user snapmaker-print-copilot -- "C:\Users\you\Snapmaker_Print_Copilot\.venv\Scripts\print-copilot-mcp.exe"
```

Add `--env PRINT_COPILOT_WORK_DIR=D:\print-runs` (repeatable) before the
server name to set environment variables. Check with `claude mcp list`, or
`/mcp` inside a session. For long slices, start Claude Code with a larger tool
timeout, e.g. `MCP_TOOL_TIMEOUT=600000` (milliseconds).

## 4. Codex

Add to `~/.codex/config.toml` (`%USERPROFILE%\.codex\config.toml` on Windows):

```toml
[mcp_servers.snapmaker-print-copilot]
command = 'C:\Users\you\Snapmaker_Print_Copilot\.venv\Scripts\print-copilot-mcp.exe'
args = []
tool_timeout_sec = 600

# Optional:
# [mcp_servers.snapmaker-print-copilot.env]
# PRINT_COPILOT_WORK_DIR = 'D:\print-runs'
```

Single quotes make TOML keep the backslashes as they are. Restart Codex and
check the server with `/mcp`.

## 5. Claude Desktop

Settings → Developer → Edit Config opens
`%APPDATA%\Claude\claude_desktop_config.json`. Add:

```json
{
  "mcpServers": {
    "snapmaker-print-copilot": {
      "command": "C:\\Users\\you\\Snapmaker_Print_Copilot\\.venv\\Scripts\\print-copilot-mcp.exe",
      "args": [],
      "env": {}
    }
  }
}
```

Restart Claude Desktop; the tools appear under the tools menu of a new chat.

## 6. Checking the server without an AI client

The MCP Inspector (needs Node.js) starts the server and lets you call each
tool by hand:

```bash
npx @modelcontextprotocol/inspector "C:\Users\you\Snapmaker_Print_Copilot\.venv\Scripts\print-copilot-mcp.exe"
```

## 7. Example prompts

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
