"""`Snapmaker_Orca.conf` parsing. The fixture is the maintainer's real conf with
personal data (paths, UUID) removed and the 0.4 nozzle process set to one the
preset fixtures define."""

from pathlib import Path

import pytest

from print_copilot.errors import ConfError
from print_copilot.orca.conf import current_setup, load_conf, parse_flush_matrix

U1 = "Snapmaker U1 (0.4 nozzle)"


@pytest.fixture
def conf(orca_data: Path) -> Path:
    return orca_data / "Snapmaker_Orca.conf"


def test_selected_machine_setup(conf) -> None:
    setup = current_setup(conf)
    assert setup.machine == U1
    assert setup.process == "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"
    assert setup.filaments == ["Generic PLA"] * 4
    assert setup.filament_colors == ["#FFFFFF", "#000000", "#F4E2C1", "#ED1C24"]
    assert setup.curr_bed_type == "4"
    assert setup.app_version == "01.10.01.50"
    assert setup.known_machines == ["Default Printer", "Snapmaker U1 (0.2 nozzle)", U1]


def test_flush_matrix_rows_are_from_tool(conf) -> None:
    matrix = current_setup(conf).flush_volumes_matrix_mm3
    assert matrix is not None
    assert len(matrix) == 4 and all(len(row) == 4 for row in matrix)
    assert [matrix[i][i] for i in range(4)] == [0.0] * 4
    assert matrix[0][1] == 223.0  # T0 (white) -> T1 (black)
    assert matrix[1][0] == 703.0  # T1 (black) -> T0 (white)


def test_other_machine(conf) -> None:
    setup = current_setup(conf, "Snapmaker U1 (0.2 nozzle)")
    assert setup.process == "0.08mm High Quality @Snapmaker U1 (0.2 nozzle)"
    assert setup.filaments == ["Generic PLA @U1 0.2 nozzle"] * 4


def test_single_extruder_entry_has_no_matrix(conf) -> None:
    setup = current_setup(conf, "Default Printer")
    assert setup.filaments == ["AliZ PLA @System"]
    assert setup.flush_volumes_matrix_mm3 is None


def test_unknown_machine(conf) -> None:
    with pytest.raises(ConfError, match="no stored preset selection for machine 'Nope'"):
        current_setup(conf, "Nope")


def test_checksum_trailer_is_ignored(conf) -> None:
    assert "orca_presets" in load_conf(conf)


def test_unexpected_trailer_is_an_error(tmp_path) -> None:
    path = tmp_path / "Snapmaker_Orca.conf"
    path.write_text('{"presets": {}}\nnot a comment\n', encoding="utf-8")
    with pytest.raises(ConfError, match="unexpected content"):
        load_conf(path)


def test_missing_conf(tmp_path) -> None:
    with pytest.raises(ConfError, match="Cannot read"):
        current_setup(tmp_path / "Snapmaker_Orca.conf")


def test_no_selected_machine(tmp_path) -> None:
    path = tmp_path / "Snapmaker_Orca.conf"
    path.write_text('{"presets": {"machine": ""}, "orca_presets": []}', encoding="utf-8")
    with pytest.raises(ConfError, match="no selected machine"):
        current_setup(path)


def test_filament_gap_is_an_error(tmp_path) -> None:
    path = tmp_path / "Snapmaker_Orca.conf"
    path.write_text(
        '{"presets": {"machine": "M"}, "orca_presets": '
        '[{"machine": "M", "filament": "A", "filament_02": "C"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ConfError, match="gaps at tool heads \\[1\\]"):
        current_setup(path)


def test_flush_matrix_must_be_square() -> None:
    with pytest.raises(ConfError, match="not a square number"):
        parse_flush_matrix("0|1|2")
    assert parse_flush_matrix("") is None
    assert parse_flush_matrix("0|5|7|0") == [[0.0, 5.0], [7.0, 0.0]]
