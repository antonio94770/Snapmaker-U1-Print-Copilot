import math

import pytest

from print_copilot.errors import GcodeParseError
from print_copilot.gcode.paths import read_paths


def paths(tmp_path, body, **kwargs):
    path = tmp_path / "paths.gcode"
    path.write_text(body, encoding="utf-8")
    return read_paths(path, **kwargs)


def test_modal_moves_retractions_offsets_and_layer_numbers(tmp_path):
    result = paths(
        tmp_path,
        """G90
M83
G1 X0 Y0 Z0.2
G1 X2 E3
;LAYER_CHANGE
;TYPE:Outer wall
M104 T0 S215
G1 X10 E1 F600
G1 E-1
G1 X12
G1 E1
G1 X15 E0.5
G92 X0 E0
G1 X5 E0.3
G91
G1 X2 E0.2
G90
M82
G92 E0
;LAYER_CHANGE
T2
G1 X10 E1
G1 X11 E0.5
G1 X12 E1.5
""",
    )
    a, b, c, d = result.layers[0].segments
    assert a.start == (2, 0, 0.2) and a.end == (10, 0, 0.2)
    assert a.temperature_c == 215 and a.feed_mm_s == 10
    assert b.path_start and b.start[0] == 12
    assert c.end[0] == 20 and d.end[0] == 22
    assert sum(s.extrusion_mm for s in result.layers[0].segments) == pytest.approx(2)
    assert result.layers[1].segments[0].tool == 2
    assert result.layers[1].segments[-1].extrusion_mm == 1
    assert result.tagged_layer_count == 2


@pytest.mark.parametrize(
    "command,length",
    [
        ("G3 X10 Y10 I0 J10 E4", math.pi * 5),
        ("G2 X10 Y10 I0 J10 E4", math.pi * 15),
        ("G3 X10 Y10 R10 E4", math.pi * 5),
        ("G3 X10 Y10 R-10 E4", math.pi * 15),
        ("G3 X0 Y0 I0 J10 E4", math.pi * 20),
    ],
)
def test_arc_display_preserves_extrusion_and_analytic_length(tmp_path, command, length):
    result = paths(tmp_path, f"G90\nM83\nG1 X0 Y0 Z0.2 F600\n;LAYER_CHANGE\n{command}\n")
    segments = result.layers[0].segments
    assert len(segments) > 1
    assert sum(s.extrusion_mm for s in segments) == pytest.approx(4)
    assert sum(s.length_mm for s in segments) == pytest.approx(length)
    assert segments[0].path_start and not segments[1].path_start


@pytest.mark.parametrize(
    "body,reason",
    [
        ("M83\nG1 X0 Y0 Z0.2\nG1 X10 E1", "layer-tagged"),
        ("M83\n;LAYER_CHANGE\nG1 X10 E1", "unknown XYZ"),
        ("G20", "Unsupported coordinate"),
        ("G90.1", "Unsupported coordinate"),
        ("G54", "Unsupported coordinate"),
        ("G90\nM83\nG1 X0 Y0 Z0\n;LAYER_CHANGE\nG1 Xnan E1", "motion arguments"),
        ("G90\nM83\nG1 X0 Y0 Z0\n;LAYER_CHANGE\nG5 X10 E1", "extrusion command"),
        ("G90\nM83\nG1 X0 Y0 Z0\n;LAYER_CHANGE\nG18\nG2 X10 Z1 I5 E1", "arc plane"),
        ("G90\nM83\nG1 X0 Y0 Z0\n;LAYER_CHANGE\nG3 X10 Y0 R2 E1", "Invalid radius"),
    ],
)
def test_unsupported_or_incomplete_geometry_fails(tmp_path, body, reason):
    with pytest.raises(GcodeParseError, match=reason):
        paths(tmp_path, body)


def test_resource_cap_fails_instead_of_silently_dropping_paths(tmp_path):
    with pytest.raises(GcodeParseError, match="increase max_segments"):
        paths(
            tmp_path, "G90\nM83\nG1 X0 Y0 Z0.2\n;LAYER_CHANGE\nG1 X1 E1\nG1 X2 E1", max_segments=1
        )


def test_macros_remain_explicitly_uninterpreted(tmp_path):
    result = paths(tmp_path, "G90\nM83\nPRINT_START\nG1 X0 Y0 Z0.2\n;LAYER_CHANGE\nG1 X10 E1")
    assert result.macro_names == ["PRINT_START"]
    assert any("macros" in note for note in result.notes)
