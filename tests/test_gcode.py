import pytest

from print_copilot.errors import GcodeParseError
from print_copilot.gcode.header import parse_duration_s, parse_gcode, parse_gcode_text


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("19s", 19), ("15m 1s", 901), ("2h 0m 5s", 7205), ("1d 1h 1m 1s", 90061), ("3m", 180)],
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration_s(text) == seconds


@pytest.mark.parametrize("text", ["", "soon", "15 minutes", "1x"])
def test_parse_duration_rejects_garbage(text: str) -> None:
    with pytest.raises(GcodeParseError):
        parse_duration_s(text)


def test_parse_cube_gcode(cube_gcode) -> None:
    result = parse_gcode(cube_gcode)
    assert result.generator == "Snapmaker Orca 2.4.0"
    assert result.estimated_time_s == 901
    assert result.first_layer_time_s == 19
    assert result.layer_count == 100
    assert result.max_z_height_mm == pytest.approx(20.05)
    assert result.filament_used_g == [3.65, 0.0, 0.0, 0.0]
    assert result.total_filament_g == pytest.approx(3.65)
    assert result.filament_used_mm[0] == pytest.approx(1225.34)
    assert result.filament_used_cm3[0] == pytest.approx(2.95)
    assert result.total_filament_cost == pytest.approx(0.07)
    assert result.tool_count == 4
    assert result.tools_used == [0]
    assert result.printer_settings_id == "Snapmaker U1 (0.4 nozzle)"
    assert result.print_settings_id == "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"
    assert result.settings["layer_height"] == "0.2"
    assert result.settings["wall_loops"] == "2"
    assert result.settings["sparse_infill_density"] == "15%"
    assert (
        result.settings["filament_settings_id"]
        == '"Generic PLA";"Generic PLA";"Generic PLA";"Generic PLA"'
    )


def test_config_block_values_do_not_leak_into_summary() -> None:
    text = """; total layer number: 5
; filament used [g] = 1.00, 0.50
; total filament used [g] = 1.50
; estimated printing time (normal mode) = 1m 0s
; CONFIG_BLOCK_START
; total filament used [g] = 99
; print_settings_id = "Quoted name"
; CONFIG_BLOCK_END
"""
    result = parse_gcode_text(text)
    assert result.total_filament_g == pytest.approx(1.5)
    assert result.layer_count == 5
    assert result.tools_used == [0, 1]
    assert result.print_settings_id == "Quoted name"


def test_missing_time_fails() -> None:
    text = "; filament used [g] = 1.0\n; total filament used [g] = 1.0\n; total layers count = 3\n"
    with pytest.raises(GcodeParseError, match="estimated printing time"):
        parse_gcode_text(text)


def test_inconsistent_grams_fail() -> None:
    text = """; filament used [g] = 1.00, 2.00
; total filament used [g] = 9.00
; total layers count = 3
; estimated printing time (normal mode) = 10s
"""
    with pytest.raises(GcodeParseError, match="do not add up"):
        parse_gcode_text(text)


def test_unreadable_file_fails(tmp_path) -> None:
    with pytest.raises(GcodeParseError, match="Cannot read"):
        parse_gcode(tmp_path / "missing.gcode")
