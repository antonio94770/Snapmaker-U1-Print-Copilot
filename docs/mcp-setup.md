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

None of these tools talk to the printer. Overrides use preset JSON keys
(`sparse_infill_density`, `wall_loops`, ...). A key without a prefix is a
process setting; `machine.<key>` and `filament.<key>` target the machine and
filament presets (filament overrides apply to every head). Keys the preset does
not define are rejected.

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
- "Compare 15%, 25% and 40% infill and 2 vs 4 walls for the bracket and
  recommend the cheapest variant that keeps 4 walls."
