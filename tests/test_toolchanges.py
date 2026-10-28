import pytest

from print_copilot.errors import GcodeParseError
from print_copilot.gcode.header import parse_gcode_text
from print_copilot.gcode.toolchanges import grams_per_mm, to_grams, tool_change_lines, tool_changes

# Shaped like the U1 output on 01.10.01.50 (two objects on T0 and T2).
GCODE = """\
; start
T0
M83
T0
;LAYER_CHANGE
;TYPE:Outer wall
G1 X10 Y10 E2
G1 E-0.8
G1 E0.8
T2
SM_PRINT_PREEXTRUDE_FILAMENT INDEX=2
; CP TOOLCHANGE START
;TYPE:Prime tower
; CP TOOLCHANGE WIPE
G1 X30 Y30 E1.5
; CP TOOLCHANGE END
G1 X31 Y30 E0.5
;TYPE:Outer wall
G1 X50 Y50 E3
;LAYER_CHANGE
T2 ; same tool again: not a change
T0
SM_PRINT_PREEXTRUDE_FILAMENT INDEX=0
;TYPE:Prime tower
; CP TOOLCHANGE WIPE
G1 X30 Y32 E1
; CP TOOLCHANGE END
;TYPE:Inner wall
G1 X12 Y10 E1
"""


def test_counts_changes_tower_and_purge() -> None:
    stats = tool_change_lines(GCODE.splitlines())
    assert stats.tool_changes == 2
    assert stats.transitions == {"T0>T2": 1, "T2>T0": 1}
    assert stats.first_tool == 0
    assert stats.tools_used == [0, 2]
    assert stats.layers_with_changes == 2
    assert stats.extrusion_mm == {0: 4.0, 2: 5.0}
    assert stats.prime_tower_mm == {0: 1.0, 2: 2.0}
    assert stats.prime_tower_total_mm == 3.0
    assert stats.purge_mm == {0: 1.0, 2: 1.5}
    assert stats.purge_total_mm == 2.5
    assert stats.preextrude_macro_calls == 2


def test_absolute_extrusion() -> None:
    lines = ["T1", "M82", "G92 E0", "G1 X1 Y1 E2", "G1 E1.5", "G1 E2", "G1 X2 Y1 E3"]
    assert tool_change_lines(lines).extrusion_mm == {1: 3.0}


def test_grams_use_each_heads_ratio(cube_gcode) -> None:
    header = """\
; filament used [mm] = 100.00, 0.00, 200.00, 0.00
; filament used [g] = 0.30, 0.00, 0.50, 0.00
; total filament used [g] = 0.80
; estimated printing time (normal mode) = 1m 0s
; total layer number: 2
"""
    ratios = grams_per_mm(parse_gcode_text(header))
    assert ratios == pytest.approx([0.003, 0.0, 0.0025, 0.0])
    assert to_grams({0: 100.0, 2: 40.0}, ratios) == 0.4


def test_file_without_tool_selection_fails(tmp_path) -> None:
    path = tmp_path / "none.gcode"
    path.write_text("G1 X1 Y1 E1\n")
    with pytest.raises(GcodeParseError, match="no T<n>"):
        tool_changes(path)


def test_real_cube_fixture_selects_one_tool(cube_gcode) -> None:
    stats = tool_changes(cube_gcode)
    assert stats.tool_changes == 0
    assert stats.first_tool == 0
