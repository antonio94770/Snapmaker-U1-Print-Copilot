"""Slice a real model with the installed Snapmaker Orca. Skipped when it is absent."""

from pathlib import Path

import pytest

from print_copilot.errors import OrcaNotFoundError
from print_copilot.orca.install import locate
from print_copilot.orca.presets import PresetLibrary
from print_copilot.orca.slicer import slice_model

CUBE = Path(__file__).parents[1] / "research" / "cli-probe" / "cube.stl"


@pytest.fixture(scope="module")
def real_install():
    try:
        return locate()
    except OrcaNotFoundError as exc:
        pytest.skip(f"Snapmaker Orca not installed: {exc}")


@pytest.mark.integration
def test_slice_cube_u1_standard_pla(real_install, tmp_path) -> None:
    library = PresetLibrary(real_install.data_dir)
    machine = library.resolve("machine", "Snapmaker U1 (0.4 nozzle)")
    process = library.resolve("process", "0.20mm Standard @Snapmaker U1 (0.4 nozzle)")
    filaments = [library.resolve("filament", "Generic PLA")] * 4

    run = slice_model(real_install, CUBE, machine, process, filaments, work_dir=tmp_path)
    result = run.result

    # Reference on Snapmaker Orca 01.10.01.50: 15m 1s and 3.65 g (HANDOFF.md section 4).
    # The range allows small drift so a minor slicer update does not break the test.
    assert 600 <= result.estimated_time_s <= 1500
    assert 3.0 <= result.total_filament_g <= 4.5
    assert result.layer_count == 100  # 20 mm cube at 0.2 mm layers
    assert result.tool_count == 4
    assert result.tools_used == [0]
    assert result.settings["printer_model"] == "Snapmaker U1"
    assert result.settings["layer_height"] == "0.2"
    assert run.gcode_path.is_file()
    assert run.log_path.is_file()
