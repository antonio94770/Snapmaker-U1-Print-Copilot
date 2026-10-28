import pytest

from print_copilot.errors import GcodeParseError
from print_copilot.gcode.features import feature_extrusion, feature_extrusion_lines

RELATIVE = """\
M83 ; use relative distances for extrusion
;TYPE:Custom
G1 X185 E15 F360
G1 E-1.5 F1800
;TYPE:Outer wall
G1 E1.5 F1800
G1 X10 Y10 E2.0
G1 X20 Y10 E-0.2
G2 X30 Y20 I5 J5 E1.0 ; arc
;TYPE:Support
G1 X40 Y20 E3.0
;TYPE:Support interface
G1 X41 Y20 E0.5
G1 Z1.2
"""


def test_relative_extrusion_by_feature() -> None:
    result = feature_extrusion_lines(RELATIVE.splitlines())
    assert result.by_feature_mm == {
        "Custom": 15.0,
        "Outer wall": 3.0,
        "Support": 3.0,
        "Support interface": 0.5,
    }
    assert result.support_mm == pytest.approx(3.5)
    assert result.total_mm == pytest.approx(21.5)


def test_absolute_extrusion_with_reset() -> None:
    text = """\
M82
G92 E0
;TYPE:Inner wall
G1 X1 Y1 E5
G1 E4
G1 E5
G1 X2 Y2 E7
G92 E0
;TYPE:Support
G1 X3 Y3 E1.5
"""
    result = feature_extrusion_lines(text.splitlines())
    assert result.by_feature_mm == {"Inner wall": 7.0, "Support": 1.5}


def test_fixture_matches_header_total(cube_gcode) -> None:
    result = feature_extrusion(cube_gcode)
    assert result.total_mm == pytest.approx(1225.34)
    assert result.support_mm == 0


def test_no_extrusion_fails(tmp_path) -> None:
    path = tmp_path / "empty.gcode"
    path.write_text("G28\n")
    with pytest.raises(GcodeParseError, match="no extruding moves"):
        feature_extrusion(path)
