from pathlib import Path

import pytest

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
