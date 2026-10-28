import json
import subprocess
from pathlib import Path

import pytest

from print_copilot.orca.install import OrcaInstall
from print_copilot.orca.presets import PresetLibrary

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def orca_data() -> Path:
    return FIXTURES / "orca_data"


@pytest.fixture
def library(orca_data: Path) -> PresetLibrary:
    return PresetLibrary(orca_data)


@pytest.fixture
def cube_gcode() -> Path:
    return FIXTURES / "cube_u1_020_pla.gcode"


class FakeOrca:
    """Stands in for `subprocess.run` of the Snapmaker Orca CLI.

    Writes the cube G-code fixture. When the process sent to the CLI has
    `sparse_infill_density = 30%`, the G-code reports the numbers measured for
    that setting on the real slicer (15m 8s, 4.71 g) instead of 15m 1s, 3.65 g.
    """

    def __init__(self, gcode: Path) -> None:
        self.gcode = gcode.read_text(encoding="utf-8")
        self.commands: list[list[str]] = []

    def __call__(self, command, stdout, **kwargs):
        self.commands.append(command)
        stdout.write(b"slicer output\n")
        settings = command[command.index("--load-settings") + 1].split(";")
        process = json.loads(Path(settings[1]).read_text(encoding="utf-8"))
        text = self.gcode
        if process.get("sparse_infill_density") == "30%":
            text = (
                text.replace("filament used [g] = 3.65,", "filament used [g] = 4.71,")
                .replace("total filament used [g] = 3.65", "total filament used [g] = 4.71")
                .replace("(normal mode) = 15m 1s", "(normal mode) = 15m 8s")
            )
        out_dir = Path(command[command.index("--outputdir") + 1])
        (out_dir / "plate_1.gcode").write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)


@pytest.fixture
def fake_orca(monkeypatch, cube_gcode: Path) -> FakeOrca:
    fake = FakeOrca(cube_gcode)
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


@pytest.fixture
def fake_install(tmp_path: Path, orca_data: Path) -> OrcaInstall:
    exe = tmp_path / "snapmaker-orca.exe"
    exe.write_bytes(b"")
    return OrcaInstall(exe=exe, data_dir=orca_data)


@pytest.fixture
def model(tmp_path: Path) -> Path:
    path = tmp_path / "cube.stl"
    path.write_text("solid cube\nendsolid cube\n")
    return path
