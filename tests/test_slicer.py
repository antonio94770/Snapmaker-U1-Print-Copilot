import json
import shutil
import subprocess
from pathlib import Path

import pytest

from print_copilot.errors import SliceError
from print_copilot.orca.install import OrcaInstall
from print_copilot.orca.presets import PresetLibrary
from print_copilot.orca.slicer import (
    SettingsOverrides,
    check_overrides_applied,
    prepare_cli_settings,
    slice_model,
)

U1 = "Snapmaker U1 (0.4 nozzle)"
U1_PROCESS = "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"


@pytest.fixture
def presets(library: PresetLibrary):
    machine = library.resolve("machine", U1)
    process = library.resolve("process", U1_PROCESS)
    filaments = [library.resolve("filament", "Generic PLA")] * 4
    return machine, process, filaments


@pytest.fixture
def install(tmp_path: Path, orca_data: Path) -> OrcaInstall:
    exe = tmp_path / "snapmaker-orca.exe"
    exe.write_bytes(b"")
    return OrcaInstall(exe=exe, data_dir=orca_data)


@pytest.fixture
def model(tmp_path: Path) -> Path:
    path = tmp_path / "cube.stl"
    path.write_text("solid cube\nendsolid cube\n")
    return path


def fake_slicer(gcode: Path | None, returncode: int = 0, log: str = "slicer output\n"):
    """A stand-in for `subprocess.run` that behaves like the Snapmaker Orca CLI."""
    calls = []

    def run(command, stdout, **kwargs):
        calls.append(command)
        stdout.write(log.encode())
        out_dir = Path(command[command.index("--outputdir") + 1])
        if gcode is not None:
            shutil.copy(gcode, out_dir / "plate_1.gcode")
        return subprocess.CompletedProcess(command, returncode)

    return run, calls


def test_workarounds_applied(presets) -> None:
    machine, process, filaments = presets
    machine_s, process_s, filament_s = prepare_cli_settings(machine, process, filaments)
    assert machine_s["from"] == "system"
    assert process_s["from"] == "User"
    assert "wipe_tower_filament" not in process_s
    assert len(filament_s) == 4
    assert all(f["from"] == "User" for f in filament_s)
    # The resolved presets themselves are untouched.
    assert process.settings["wipe_tower_filament"] == "0"


def test_filament_count_must_match_tool_heads(presets) -> None:
    machine, process, filaments = presets
    with pytest.raises(SliceError, match="4 tool heads"):
        prepare_cli_settings(machine, process, filaments[:2])


def test_slice_writes_reproducible_run_dir(
    monkeypatch, tmp_path, presets, install, model, cube_gcode
) -> None:
    run, calls = fake_slicer(cube_gcode)
    monkeypatch.setattr(subprocess, "run", run)

    sliced = slice_model(install, model, *presets, work_dir=tmp_path / "runs")

    assert sliced.result.estimated_time_s == 901
    assert sliced.result.total_filament_g == pytest.approx(3.65)
    assert sliced.run_dir.parent == tmp_path / "runs"
    for name in ("machine.json", "process.json", "filament_1.json", "filament_4.json"):
        assert (sliced.run_dir / name).is_file()
    assert (sliced.run_dir / "slicer.log").read_text() == "slicer output\n"
    assert "--load-filaments" in (sliced.run_dir / "command.txt").read_text()
    saved = json.loads((sliced.run_dir / "result.json").read_text(encoding="utf-8"))
    assert saved["result"]["estimated_time_s"] == 901

    command = calls[0]
    assert command[1:3] == ["--slice", "0"]
    settings = command[command.index("--load-settings") + 1].split(";")
    assert [Path(p).name for p in settings] == ["machine.json", "process.json"]
    assert len(command[command.index("--load-filaments") + 1].split(";")) == 4
    # Windows-style paths with forward slashes, never MSYS `/c/...`.
    for arg in [*settings, command[-1]]:
        assert arg[1] == ":"
        assert "\\" not in arg


def test_each_slice_gets_a_fresh_dir(
    monkeypatch, tmp_path, presets, install, model, cube_gcode
) -> None:
    monkeypatch.setattr(subprocess, "run", fake_slicer(cube_gcode)[0])
    first = slice_model(install, model, *presets, work_dir=tmp_path)
    second = slice_model(install, model, *presets, work_dir=tmp_path)
    assert first.run_dir != second.run_dir


def test_crash_is_reported(monkeypatch, tmp_path, presets, install, model) -> None:
    # 0xC0000005 (access violation) as a signed Windows exit code.
    run, _ = fake_slicer(None, returncode=-1073741819, log="boom\n")
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SliceError, match=r"0xC0000005.*access violation(?s:.*)boom"):
        slice_model(install, model, *presets, work_dir=tmp_path)


def test_missing_gcode_is_reported(monkeypatch, tmp_path, presets, install, model) -> None:
    monkeypatch.setattr(subprocess, "run", fake_slicer(None, log="can not find setting file")[0])
    with pytest.raises(SliceError, match=r"wrote no G-code(?s:.*)can not find setting file"):
        slice_model(install, model, *presets, work_dir=tmp_path)


def test_timeout_is_reported(monkeypatch, tmp_path, presets, install, model) -> None:
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SliceError, match="did not finish within 5 s"):
        slice_model(install, model, *presets, work_dir=tmp_path, timeout_s=5)


def test_wrong_printer_in_gcode_is_reported(
    monkeypatch, tmp_path, presets, install, model, cube_gcode
) -> None:
    other = tmp_path / "other.gcode"
    other.write_text(
        cube_gcode.read_text(encoding="utf-8").replace(
            "printer_settings_id = Snapmaker U1 (0.4 nozzle)",
            "printer_settings_id = Some Other Printer",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(subprocess, "run", fake_slicer(other)[0])
    with pytest.raises(SliceError, match="sliced for printer 'Some Other Printer'"):
        slice_model(install, model, *presets, work_dir=tmp_path / "runs")


def test_missing_model_fails(tmp_path, presets, install) -> None:
    with pytest.raises(SliceError, match="Model file not found"):
        slice_model(install, tmp_path / "nope.stl", *presets, work_dir=tmp_path)


def test_silently_replaced_override_is_reported(fake_orca, presets, install, model, tmp_path):
    # The real CLI slices `support_type = bogus` as normal(auto) without an error.
    overrides = SettingsOverrides.from_flat({"enable_support": True, "support_type": "bogus"})
    with pytest.raises(SliceError, match=r"did not apply the override support_type = 'bogus'"):
        slice_model(install, model, *presets, work_dir=tmp_path, overrides=overrides)


@pytest.mark.parametrize(
    ("value", "written", "ok"),
    [
        ("0.20", "0.2", True),
        ("30%", "30%", True),
        ("30%", "30", False),
        (["215"], "215,215,215,215", True),
        (["215", "220"], "215,220", True),
        (["215", "220"], "215,215", False),
        ("a,b", "a,b", True),
        ("tree(auto)", "normal(auto)", False),
    ],
)
def test_check_overrides_applied(value, written, ok) -> None:
    overrides = SettingsOverrides.from_flat({"key": value})
    if ok:
        check_overrides_applied({"key": written}, overrides)
    else:
        with pytest.raises(SliceError, match="did not apply"):
            check_overrides_applied({"key": written}, overrides)


def test_overrides_missing_from_config_block_are_not_checked() -> None:
    check_overrides_applied({}, SettingsOverrides.from_flat({"key": "1"}))


def test_too_long_work_dir_fails_before_slicing(fake_orca, presets, install, model, tmp_path):
    deep = tmp_path / ("x" * 240)
    with pytest.raises(SliceError, match="too long"):
        slice_model(install, model, *presets, work_dir=deep)
    assert fake_orca.commands == []
