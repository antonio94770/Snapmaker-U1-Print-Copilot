import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from print_copilot import cli, connect
from print_copilot.connect import SERVER_NAME, ConnectError


@pytest.fixture
def exe(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "venv" / "Scripts" / "print-copilot-mcp.exe"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    monkeypatch.setattr(connect, "server_exe", lambda: path)
    return path


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    return home


def test_vscode_file_is_created_then_left_alone(home, exe) -> None:
    result = connect.connect("vscode")
    path = home / ".copilot" / "mcp-config.json"
    assert (result.action, result.target, result.backup) == ("added", str(path), None)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"mcpServers": {SERVER_NAME: {"command": str(exe), "args": []}}}
    assert connect.connect("vscode").action == "already configured"


def test_other_servers_are_kept_and_a_backup_written(home, exe) -> None:
    path = home / ".cursor" / "mcp.json"
    path.parent.mkdir()
    original = {"mcpServers": {"other": {"command": "npx", "args": ["x"]}}, "keep": 1}
    path.write_text(json.dumps(original), encoding="utf-8")
    result = connect.connect("cursor")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["keep"] == 1
    assert data["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert data["mcpServers"][SERVER_NAME] == {"type": "stdio", "command": str(exe), "args": []}
    assert json.loads(result.backup.read_text(encoding="utf-8")) == original


def test_a_different_entry_needs_force(home, exe) -> None:
    path = home / ".copilot" / "mcp-config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {SERVER_NAME: {"command": "old.exe"}}}))
    with pytest.raises(ConnectError, match="--force"):
        connect.connect("vscode")
    assert connect.connect("vscode", dry_run=True, force=True).action == "would add"
    assert "old.exe" in path.read_text()
    connect.connect("vscode", force=True)
    assert json.loads(path.read_text())["mcpServers"][SERVER_NAME]["command"] == str(exe)


def test_same_server_written_with_other_slashes_is_recognised(home, exe) -> None:
    path = home / ".copilot" / "mcp-config.json"
    path.parent.mkdir()
    entry = {"command": str(exe).replace("\\", "/").upper(), "args": [], "env": {"A": "1"}}
    path.write_text(json.dumps({"mcpServers": {SERVER_NAME: entry}}))
    assert connect.connect("vscode").action == "already configured"


def test_unreadable_json_is_not_touched(home, exe) -> None:
    path = home / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text("{ // comment\n}", encoding="utf-8")
    with pytest.raises(ConnectError, match="not plain JSON"):
        connect.connect("cursor")
    assert path.read_text(encoding="utf-8") == "{ // comment\n}"


def test_claude_desktop_must_be_installed(home, exe) -> None:
    with pytest.raises(ConnectError, match="install Claude Desktop"):
        connect.connect("claude-desktop")
    (home / "AppData" / "Roaming" / "Claude").mkdir(parents=True)
    result = connect.connect("claude-desktop")
    assert result.target.endswith("claude_desktop_config.json")


def test_codex_table_is_appended(home, exe) -> None:
    path = home / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('model = "x"\n[mcp_servers.other]\ncommand = "y"\n', encoding="utf-8")
    result = connect.connect("codex")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == "x" and data["mcp_servers"]["other"] == {"command": "y"}
    assert data["mcp_servers"][SERVER_NAME] == {
        "command": str(exe),
        "args": [],
        "tool_timeout_sec": 600,
    }
    assert result.backup.read_text(encoding="utf-8").startswith('model = "x"')
    assert connect.connect("codex").action == "already configured"


def test_codex_conflict_is_left_to_the_user(home, exe) -> None:
    path = home / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text(f"[mcp_servers.{SERVER_NAME}]\ncommand = 'old.exe'\n", encoding="utf-8")
    with pytest.raises(ConnectError, match="by hand"):
        connect.connect("codex", force=True)


def test_agent_commands(exe, monkeypatch) -> None:
    result = connect.connect("gemini", dry_run=True)
    assert result.action == "would run"
    assert result.target.startswith("gemini mcp add --scope user --timeout 600000 ")
    monkeypatch.setattr(connect.shutil, "which", lambda name: None)
    with pytest.raises(ConnectError, match="claude is not on PATH"):
        connect.connect("claude-code")
    ran = []

    def fake_run(command, **kwargs):
        ran.append(command)
        return subprocess.CompletedProcess(command, 0, "Added", "")

    monkeypatch.setattr(connect.shutil, "which", lambda name: f"C:/bin/{name}.cmd")
    monkeypatch.setattr(connect.subprocess, "run", fake_run)
    assert connect.connect("claude-code").action == "added"
    assert ran == [
        ["C:/bin/claude.cmd", "mcp", "add", "--scope", "user", SERVER_NAME, "--", str(exe)]
    ]


def test_cli_connect(home, exe, capsys) -> None:
    assert cli.main(["connect", "vscode", "--dry-run"]) == 0
    assert "Would add snapmaker-print-copilot" in capsys.readouterr().out
    assert not (home / ".copilot").exists()
    assert cli.main(["connect", "vscode"]) == 0
    assert "Restart vscode" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["connect", "nope"])
