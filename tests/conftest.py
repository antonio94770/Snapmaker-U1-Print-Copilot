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
    With `enable_support = 1` it adds 100 mm of `Support` extrusion (+0.30 g,
    +60 s). Process values are echoed into the config block like the real
    slicer, which also replaces an unknown `support_type` with `normal(auto)`.
    """

    SUPPORT_TYPES = ("normal(auto)", "tree(auto)", "normal(manual)", "tree(manual)")

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
        if process.get("enable_support") == "1":
            old = text.split("total filament used [g] = ")[1].split()[0]
            new = f"{float(old) + 0.3:.2f}"
            text = (
                text.replace(
                    "G1 X10 Y10 E1225.34", "G1 X10 Y10 E1225.34\n;TYPE:Support\nG1 X12 Y10 E100"
                )
                .replace("filament used [mm] = 1225.34,", "filament used [mm] = 1325.34,")
                .replace(f"filament used [g] = {old},", f"filament used [g] = {new},")
                .replace(f"total filament used [g] = {old}", f"total filament used [g] = {new}")
                .replace("(normal mode) = 15m", "(normal mode) = 16m")
            )
        if process.get("support_type") not in (None, *self.SUPPORT_TYPES):
            process["support_type"] = "normal(auto)"
        lines = []
        for line in text.splitlines():
            key, sep, _ = line[2:].partition(" = ")
            if line.startswith("; ") and sep and isinstance(process.get(key), str):
                line = f"; {key} = {process[key]}"
            lines.append(line)
        text = "\n".join(lines) + "\n"
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
