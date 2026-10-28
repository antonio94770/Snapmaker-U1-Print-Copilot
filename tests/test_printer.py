"""Moonraker client, gated printer actions, MCP tools and CLI against the fake U1."""

import asyncio
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from print_copilot import cli, mcp_server
from print_copilot.errors import ConfirmationError, PrinterError
from print_copilot.printer import actions, discovery, moonraker
from print_copilot.printer.fake import FakeMoonraker, FakeState
from print_copilot.printer.moonraker import (
    ENV_PRINTER_URL,
    MoonrakerClient,
    check_remote_name,
    resolve_url,
)


@pytest.fixture
def fake():
    with FakeMoonraker() as server:
        yield server


@pytest.fixture
def client(fake) -> MoonrakerClient:
    return MoonrakerClient(fake.url)


@pytest.fixture
def stored(client, cube_gcode) -> str:
    return actions.upload_gcode(client, cube_gcode).name


def _gcode(tmp_path: Path, cube_gcode: Path, **replace: str) -> Path:
    text = cube_gcode.read_text(encoding="utf-8")
    for key, value in replace.items():
        text = "\n".join(
            f"; {key} = {value}" if line.startswith(f"; {key} = ") else line
            for line in text.splitlines()
        )
    path = tmp_path / "variant.gcode"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_real_discovery(monkeypatch):
    """Tests never send mDNS queries to the real network."""
    moonraker.forget_discovered()

    def offline(timeout_s: float = 0) -> list:
        raise AssertionError("real mDNS discovery attempted in a test")

    monkeypatch.setattr(discovery, "discover", offline)
    yield
    moonraker.forget_discovered()


def _mdns_answer(*printers: dict[str, str]) -> bytes:
    """A response packet shaped like the U1's answer to the _snapmaker._tcp query."""

    def name(text: str) -> bytes:
        return b"".join(bytes([len(p)]) + p.encode() for p in text.split(".")) + b"\0"

    records = []
    for values in printers:
        instance = f"U1-SERIAL.{discovery.SERVICE}"
        records.append(name(discovery.SERVICE) + struct.pack(">HHIH", 12, 1, 120, 0))
        rdata = b"".join(bytes([len(f"{k}={v}")]) + f"{k}={v}".encode() for k, v in values.items())
        records.append(name(instance) + struct.pack(">HHIH", 16, 0x8001, 120, len(rdata)) + rdata)
    header = struct.pack(">HHHHHH", 0, 0x8400, 0, len(records), 0, 0)
    return header + b"".join(records)


U1_TXT = {
    "device_name": "U1",
    "ip": "192.168.1.77",
    "machine_type": "Snapmaker U1",
    "sn": "SERIAL",
    "userid": "123",
    "version": "2.0.0",
}


def test_mdns_answer_parses_without_identifiers() -> None:
    txt, _ = discovery.parse_response(_mdns_answer(U1_TXT))
    [found] = discovery.printers_from(txt, "192.168.1.99")
    assert found == discovery.DiscoveredPrinter("U1", "Snapmaker U1", "2.0.0", "192.168.1.77")
    assert found.url == "http://192.168.1.77:80"
    assert "SERIAL" not in repr(found) and "123" not in repr(found)
    # A query (not a response) is ignored.
    assert discovery.parse_response(discovery.build_query()) == ({}, {})


def test_find_u1_fails_loudly_on_none_or_several(monkeypatch) -> None:
    one = discovery.DiscoveredPrinter("U1", "Snapmaker U1", "2.0.0", "10.0.0.5")
    two = discovery.DiscoveredPrinter("Garage", "Snapmaker U1", "2.0.0", "10.0.0.6")
    monkeypatch.setattr(discovery, "discover", lambda timeout_s=0: [])
    with pytest.raises(PrinterError, match="No Snapmaker U1 answered"):
        discovery.find_u1()
    monkeypatch.setattr(discovery, "discover", lambda timeout_s=0: [one, two])
    with pytest.raises(PrinterError, match=r"Several U1 printers answered: U1 \(10.0.0.5\)"):
        discovery.find_u1()
    assert discovery.find_u1("Garage") == two


def test_resolve_url(monkeypatch) -> None:
    monkeypatch.delenv(ENV_PRINTER_URL, raising=False)
    monkeypatch.delenv(moonraker.ENV_PRINTER_NAME, raising=False)
    found = discovery.DiscoveredPrinter("U1", "Snapmaker U1", "2.0.0", "10.0.0.5")
    calls = []
    monkeypatch.setattr(discovery, "discover", lambda timeout_s=0: calls.append(1) or [found])
    assert resolve_url(None) == "http://10.0.0.5:80"
    assert resolve_url(None) == "http://10.0.0.5:80"
    assert len(calls) == 1  # cached for the process
    assert resolve_url("192.168.1.50") == "http://192.168.1.50"
    assert resolve_url("http://u1.local:7125/") == "http://u1.local:7125"
    monkeypatch.setenv(ENV_PRINTER_URL, "http://10.0.0.9")
    assert resolve_url(None) == "http://10.0.0.9"
    with pytest.raises(PrinterError, match="without a path"):
        resolve_url("http://10.0.0.9/printer")


def test_remote_names() -> None:
    assert check_remote_name("sub/part.gcode") == "sub/part.gcode"
    for bad in ("../x.gcode", "/abs.gcode", "part.3mf", ""):
        with pytest.raises(PrinterError):
            check_remote_name(bad)


def test_unreachable_printer_fails_loudly() -> None:
    client = MoonrakerClient("http://127.0.0.1:9", timeout_s=1)
    with pytest.raises(PrinterError, match="Cannot reach the printer"):
        actions.printer_status(client)


def test_status_reads_heads_and_rfid_spools(client) -> None:
    status = actions.printer_status(client)
    assert status.machine_type == "Snapmaker U1"
    assert status.ready_to_start
    assert [h.object_name for h in status.heads] == [
        "extruder",
        "extruder1",
        "extruder2",
        "extruder3",
    ]
    assert status.heads[0].state == "ACTIVATE"
    assert status.heads[0].rfid_filament.type == "PLA"
    assert status.heads[0].rfid_filament.colour == "#E2DEDB"
    assert status.heads[2].rfid_filament is None


def test_upload_never_starts_and_refuses_overwrite(fake, client, cube_gcode) -> None:
    result = actions.upload_gcode(client, cube_gcode)
    assert result.size_bytes == cube_gcode.stat().st_size
    assert fake.sent == ["POST /server/files/upload"]
    assert fake.state.print_state == "standby"
    with pytest.raises(PrinterError, match="already exists"):
        actions.upload_gcode(client, cube_gcode)
    actions.upload_gcode(client, cube_gcode, overwrite=True)
    assert [f.name for f in actions.list_files(client)] == [cube_gcode.name]


def test_upload_refuses_gcode_for_another_printer(fake, client, cube_gcode, tmp_path) -> None:
    other = _gcode(tmp_path, cube_gcode, printer_model="Bambu Lab X1 Carbon")
    with pytest.raises(PrinterError, match="sliced for 'Bambu Lab X1 Carbon'"):
        actions.upload_gcode(client, other)
    assert fake.sent == []


def test_plan_sends_nothing_and_lists_the_request(fake, client, stored) -> None:
    fake.sent.clear()
    plan = actions.plan_start(client, stored)
    assert fake.sent == []
    assert not plan.blocked
    assert plan.request == f"POST {fake.url}/printer/print/start?filename={stored}"
    assert plan.estimated_time_s == 901
    assert [h.index for h in plan.heads] == [0]
    assert plan.heads[0].loaded_type == "PLA"


def test_start_needs_a_pending_token_and_works_once(fake, client, stored) -> None:
    plan = actions.plan_start(client, stored)
    sent = actions.execute_start(client, actions.take_plan(plan.token), confirmed_by="test")
    assert sent.request.endswith(f"/printer/print/start?filename={stored}")
    assert fake.state.print_state == "printing"
    with pytest.raises(ConfirmationError, match="already used"):
        actions.take_plan(plan.token)


def test_expired_plan_is_refused(client, stored) -> None:
    plan = actions.plan_start(client, stored, now=1000.0)
    with pytest.raises(ConfirmationError, match="expired"):
        actions.take_plan(plan.token, now=1000.0 + actions.PLAN_TTL_S + 1)


def test_busy_printer_blocks_the_plan(fake, client, stored) -> None:
    fake.state.print_state = "printing"
    plan = actions.plan_start(client, stored)
    assert plan.blocked
    with pytest.raises(ConfirmationError):
        actions.take_plan(plan.token)


def test_changes_after_planning_send_nothing(fake, client, stored) -> None:
    plan = actions.take_plan(actions.plan_start(client, stored).token)
    fake.state.files[stored].modified += 5
    fake.sent.clear()
    with pytest.raises(ConfirmationError, match="changed on the printer"):
        actions.execute_start(client, plan, confirmed_by="test")
    plan = actions.take_plan(actions.plan_start(client, stored).token)
    fake.state.print_state = "printing"
    with pytest.raises(ConfirmationError, match="no longer idle"):
        actions.execute_start(client, plan, confirmed_by="test")
    assert fake.sent == []


def test_nozzle_mismatch_blocks_and_spool_mismatch_warns(cube_gcode, tmp_path) -> None:
    state = FakeState(nozzle_mm=(0.6, 0.4, 0.4, 0.4))
    state.tags[0]["MAIN_TYPE"] = "PETG"
    with FakeMoonraker(state=state) as fake:
        client = MoonrakerClient(fake.url)
        name = actions.upload_gcode(client, cube_gcode).name
        plan = actions.plan_start(client, name)
    messages = [(c.level, c.message) for c in plan.checks]
    assert ("error", "Head 1 (T0): file expects a 0.4 mm nozzle, printer reports 0.6 mm.") in (
        messages
    )
    assert ("warning", "Head 1 (T0): file uses PLA, RFID spool is PETG.") in messages
    assert plan.blocked


def test_missing_rfid_spool_is_a_warning(cube_gcode) -> None:
    state = FakeState()
    state.tags[0] = dict(state.tags[2])
    with FakeMoonraker(state=state) as fake:
        client = MoonrakerClient(fake.url)
        plan = actions.plan_start(client, actions.upload_gcode(client, cube_gcode).name)
    assert not plan.blocked
    assert plan.checks[0].level == "warning"
    assert "no RFID spool read" in plan.checks[0].message


def test_different_spool_colour_is_a_warning(client, cube_gcode, tmp_path) -> None:
    red = tmp_path / "red.gcode"
    red.write_text(
        cube_gcode.read_text(encoding="utf-8") + "\n; filament_colour = #FB0207;#000000\n",
        encoding="utf-8",
    )
    plan = actions.plan_start(client, actions.upload_gcode(client, red).name)
    assert not plan.blocked
    assert [c.message for c in plan.checks] == [
        "Head 1 (T0): file colour #FB0207, RFID spool colour #E2DEDB (delta E 106.6)."
    ]


def test_safety_actions_are_not_gated(fake, client, stored) -> None:
    fake.state.targets_c["extruder"] = 220.0
    sent = actions.heaters_off(client)
    assert sent.request == f"POST {fake.url}/printer/gcode/script?script=TURN_OFF_HEATERS"
    assert fake.state.targets_c["extruder"] == 0.0
    actions.execute_start(
        client, actions.take_plan(actions.plan_start(client, stored).token), confirmed_by="t"
    )
    actions.cancel_print(client)
    assert fake.state.print_state == "cancelled"


# MCP tools


def _ctx(can_elicit: bool, answer: bool | None):
    class Session:
        def check_client_capability(self, capability):
            return can_elicit

    async def elicit(message, schema):
        if answer is None:
            return SimpleNamespace(action="decline", data=None)
        return SimpleNamespace(action="accept", data=schema(start=answer))

    return SimpleNamespace(request_context=SimpleNamespace(session=Session()), elicit=elicit)


@pytest.fixture
def mcp_env(monkeypatch, fake):
    monkeypatch.setenv(ENV_PRINTER_URL, fake.url)
    return fake


def test_mcp_status_upload_and_files(mcp_env, cube_gcode) -> None:
    assert mcp_server.printer_status()["ready_to_start"] is True
    uploaded = mcp_server.upload_to_printer(str(cube_gcode))
    assert uploaded["started"] is False
    assert mcp_server.list_printer_files()[0]["name"] == cube_gcode.name


def test_mcp_start_with_elicitation(mcp_env, cube_gcode) -> None:
    mcp_server.upload_to_printer(str(cube_gcode))
    plan = mcp_server.plan_print_start(cube_gcode.name)
    sent = asyncio.run(mcp_server.start_print(plan["token"], _ctx(True, True)))
    assert sent["confirmed_by"] == "user via MCP elicitation"
    assert mcp_env.state.print_state == "printing"


@pytest.mark.parametrize("answer", [False, None])
def test_mcp_declined_elicitation_sends_nothing(mcp_env, cube_gcode, answer) -> None:
    mcp_server.upload_to_printer(str(cube_gcode))
    plan = mcp_server.plan_print_start(cube_gcode.name)
    mcp_env.sent.clear()
    with pytest.raises(ToolError, match="did not confirm"):
        asyncio.run(mcp_server.start_print(plan["token"], _ctx(True, answer)))
    assert mcp_env.sent == []
    with pytest.raises(ToolError, match="already used"):
        asyncio.run(mcp_server.start_print(plan["token"], _ctx(True, True)))


def test_mcp_start_without_elicitation_uses_the_token(mcp_env, cube_gcode) -> None:
    mcp_server.upload_to_printer(str(cube_gcode))
    plan = mcp_server.plan_print_start(cube_gcode.name)
    sent = asyncio.run(mcp_server.start_print(plan["token"], _ctx(False, None)))
    assert sent["confirmed_by"].startswith("plan token")


def test_mcp_blocked_plan_has_no_token(mcp_env, cube_gcode) -> None:
    mcp_server.upload_to_printer(str(cube_gcode))
    mcp_env.state.klippy_state = "shutdown"
    plan = mcp_server.plan_print_start(cube_gcode.name)
    assert plan["blocked"] is True
    assert plan["token"] is None


# CLI


def test_cli_status_and_dry_run(capsys, fake, client, stored) -> None:
    assert cli.main(["printer", "status", "--printer-url", fake.url]) == 0
    out, _ = capsys.readouterr()
    assert "Head 1 (active)" in out
    assert "Polymaker PLA SnapSpeed #E2DEDB" in out
    assert "Ready to start a print: yes" in out
    fake.sent.clear()
    assert cli.main(["printer", "start", stored, "--printer-url", fake.url, "--dry-run"]) == 0
    out, _ = capsys.readouterr()
    assert "Would send: POST" in out
    assert fake.sent == []


def test_cli_start_needs_a_confirmation(capsys, monkeypatch, fake, stored) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert cli.main(["printer", "start", stored, "--printer-url", fake.url]) == 1
    assert "Pass --yes" in capsys.readouterr().err
    assert fake.state.print_state == "standby"
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert cli.main(["printer", "start", stored, "--printer-url", fake.url]) == 1
    assert fake.state.print_state == "standby"
    assert cli.main(["printer", "start", stored, "--printer-url", fake.url, "--yes"]) == 0
    assert fake.state.print_state == "printing"
