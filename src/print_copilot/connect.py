"""Register the Print Copilot MCP server with an AI agent.

`connect` writes the server into the agent's own configuration with the
absolute path of the `print-copilot-mcp.exe` installed next to the running
Python, so nobody has to edit paths by hand. File locations and formats follow
each client's documentation (checked 2026-10-29, see docs/mcp-setup.md):

    claude-desktop  %APPDATA%\\Claude\\claude_desktop_config.json   mcpServers
    vscode          ~/.copilot/mcp-config.json ($COPILOT_HOME)      mcpServers
    cursor          ~/.cursor/mcp.json                              mcpServers, type stdio
    codex           ~/.codex/config.toml                            [mcp_servers.<name>]
    claude-code     `claude mcp add --scope user`
    gemini          `gemini mcp add --scope user`

A JSON or TOML file is changed only by adding the server: other entries are
kept, the previous file is copied to `<file>.bak`, and an existing entry with
a different command is refused unless `force` is set.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from print_copilot.errors import PrintCopilotError

SERVER_NAME = "snapmaker-print-copilot"
SERVER_EXE = "print-copilot-mcp.exe"
# Seconds; slicing a large model can take minutes.
TOOL_TIMEOUT_S = 600

CLIENTS: tuple[str, ...] = ("claude-code", "codex", "claude-desktop", "vscode", "cursor", "gemini")


class ConnectError(PrintCopilotError):
    """An agent's configuration cannot be found, read or safely changed."""


@dataclass
class ConnectResult:
    client: str
    server_exe: Path
    target: str  # the file written or the command run
    action: str  # "added", "already configured", "would add", "would run"
    backup: Path | None = None
    notes: list[str] = field(default_factory=list)


def server_exe() -> Path:
    """The MCP server installed with this Python (`.venv\\Scripts`)."""
    path = Path(sys.executable).resolve().parent / SERVER_EXE
    if not path.is_file():
        raise ConnectError(
            f"{SERVER_EXE} is not next to {sys.executable}. Run this command with the "
            "print-copilot of the environment Print Copilot was installed in."
        )
    return path


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def config_path(client: str) -> Path:
    if client == "claude-desktop":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise ConnectError("APPDATA is not set; cannot find Claude Desktop's settings.")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    if client == "vscode":
        base = os.environ.get("COPILOT_HOME")
        return (Path(base) if base else _home() / ".copilot") / "mcp-config.json"
    if client == "cursor":
        return _home() / ".cursor" / "mcp.json"
    if client == "codex":
        return _home() / ".codex" / "config.toml"
    raise ConnectError(f"{client} is configured through its own command, not a file.")


def connect(client: str, *, dry_run: bool = False, force: bool = False) -> ConnectResult:
    if client not in CLIENTS:
        raise ConnectError(f"Unknown client {client!r}; choose from {', '.join(CLIENTS)}.")
    exe = server_exe()
    if client in ("claude-code", "gemini"):
        return _run_cli(client, exe, dry_run=dry_run)
    path = config_path(client)
    if client == "codex":
        return _write_toml(path, exe, dry_run=dry_run, force=force)
    entry: dict[str, object] = {"command": str(exe), "args": []}
    if client == "cursor":
        entry = {"type": "stdio", **entry}
    return _write_json(client, path, entry, dry_run=dry_run, force=force)


def _same_exe(command: object, exe: Path) -> bool:
    """Same program, whatever the slashes or letter case of its path."""
    if not isinstance(command, str) or not command:
        return False
    return os.path.normcase(os.path.normpath(command)) == os.path.normcase(str(exe))


def _backup(path: Path) -> Path:
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    return backup


def _write_json(
    client: str, path: Path, entry: dict[str, object], *, dry_run: bool, force: bool
) -> ConnectResult:
    if client == "claude-desktop" and not path.parent.is_dir():
        raise ConnectError(
            f"{path.parent} does not exist: install Claude Desktop and start it once, or "
            "use Settings → Developer → Edit Config and add the server by hand."
        )
    config: dict[str, object] = {}
    if path.is_file():
        try:
            config = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise ConnectError(
                f"{path} is not plain JSON ({exc}); add the server by hand as shown in "
                "docs/mcp-setup.md."
            ) from exc
        if not isinstance(config, dict):
            raise ConnectError(f"{path} does not hold a JSON object; add the server by hand.")
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ConnectError(f"mcpServers in {path} is not an object; add the server by hand.")
    current = servers.get(SERVER_NAME)
    if (
        isinstance(current, dict)
        and _same_exe(current.get("command"), Path(str(entry["command"])))
        and {k: v for k, v in current.items() if k not in ("command", "env")}
        == {k: v for k, v in entry.items() if k != "command"}
    ):
        return ConnectResult(client, Path(str(entry["command"])), str(path), "already configured")
    if current is not None and not force:
        raise ConnectError(
            f"{path} already has a {SERVER_NAME!r} server with another setup: {current}. "
            "Pass --force to replace it (the old file is kept as .bak)."
        )
    if dry_run:
        return ConnectResult(client, Path(str(entry["command"])), str(path), "would add")
    servers[SERVER_NAME] = entry
    backup = _backup(path) if path.is_file() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return ConnectResult(client, Path(str(entry["command"])), str(path), "added", backup)


def _toml_block(exe: Path) -> str:
    # Single-quoted TOML strings keep Windows backslashes as they are.
    return (
        f"[mcp_servers.{SERVER_NAME}]\n"
        f"command = '{exe}'\n"
        "args = []\n"
        f"tool_timeout_sec = {TOOL_TIMEOUT_S}\n"
    )


def _write_toml(path: Path, exe: Path, *, dry_run: bool, force: bool) -> ConnectResult:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConnectError(f"{path} is not valid TOML ({exc}); fix it first.") from exc
    current = config.get("mcp_servers", {}).get(SERVER_NAME)
    if current is not None:
        if _same_exe(current.get("command"), exe):
            return ConnectResult("codex", exe, str(path), "already configured")
        # Rewriting one table of a hand-edited TOML file safely needs a TOML
        # writer this project does not depend on.
        raise ConnectError(
            f"{path} already has [mcp_servers.{SERVER_NAME}] with command "
            f"{current.get('command')!r}. Change its command to '{exe}' by hand."
            + (" (--force does not rewrite TOML.)" if force else "")
        )
    if dry_run:
        return ConnectResult("codex", exe, str(path), "would add")
    backup = _backup(path) if path.is_file() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    separator = (
        "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    )
    path.write_text(text + separator + _toml_block(exe), encoding="utf-8")
    return ConnectResult("codex", exe, str(path), "added", backup)


def cli_command(client: str, exe: Path) -> list[str]:
    if client == "claude-code":
        return ["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--", str(exe)]
    return [
        "gemini", "mcp", "add", "--scope", "user",
        "--timeout", str(TOOL_TIMEOUT_S * 1000), SERVER_NAME, str(exe),
    ]  # fmt: skip


def _run_cli(client: str, exe: Path, *, dry_run: bool) -> ConnectResult:
    command = cli_command(client, exe)
    shown = subprocess.list2cmdline(command)
    notes = []
    if client == "claude-code":
        notes.append(
            f"For long slices start Claude Code with MCP_TOOL_TIMEOUT={TOOL_TIMEOUT_S * 1000}."
        )
    else:
        # Seen with `gemini mcp list` on 2026-10-29: "MCP servers are configured
        # but disabled because this folder is untrusted".
        notes.append(
            "Gemini CLI turns MCP servers off in folders you have not trusted: accept its "
            "trust prompt in the folder you start it from."
        )
    if dry_run:
        return ConnectResult(client, exe, shown, "would run", notes=notes)
    program = shutil.which(command[0])
    if program is None:
        raise ConnectError(
            f"{command[0]} is not on PATH. Install it, or run this yourself:\n{shown}"
        )
    done = subprocess.run(
        [program, *command[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if done.returncode != 0:
        output = (done.stderr or done.stdout).strip()
        raise ConnectError(f"{shown} failed (exit {done.returncode}): {output}")
    return ConnectResult(client, exe, shown, "added", notes=notes)
