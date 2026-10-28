"""Quality workflow against the official installed slicer; never a printer."""

from pathlib import Path

import pytest

from print_copilot import actions, quality
from print_copilot.errors import OrcaNotFoundError
from print_copilot.gcode.header import read_config_block
from print_copilot.orca.install import locate
from print_copilot.orca.presets import PresetLibrary
from synthetic_project import build_project


@pytest.fixture(scope="module")
def real_install():
    try:
        return locate()
    except OrcaNotFoundError as exc:
        pytest.skip(f"Snapmaker Orca not installed: {exc}")


@pytest.fixture(scope="module")
def selection(real_install, real_process_name):
    library = PresetLibrary(real_install.data_dir, system_dir=real_install.system_dir)
    chosen = actions.select_presets(
        library,
        printer="Snapmaker U1 (0.4 nozzle)",
        process=real_process_name,
        filaments=["Generic PLA"],
    )
    chosen.colours = ["#FFFFFF", "#000000", "#F4E2C1", "#ED1C24"]
    return chosen


@pytest.mark.integration
def test_real_quality_comparison_and_specific_layer_preview(real_install, selection, tmp_path):
    model = Path(__file__).parent / "fixtures" / "tee.stl"
    report = quality.review_print(
        model, install=real_install, selection=selection, work_dir=tmp_path
    )
    assert report.status == "visual_review_pending"
    assert len(report.variants) == 2
    base, fine = report.variants
    assert base.error is None and fine.error is None
    assert read_config_block(base.gcode_path)["layer_height"] == "0.2"
    assert read_config_block(fine.gcode_path)["layer_height"] == "0.1"
    assert fine.time_s > base.time_s > 0
    assert any(f.code == "overhangs_without_support" for f in base.audit.findings)
    assert base.model_preview.is_file() and fine.preview.html_path.is_file()
    preview = quality.preview_gcode(base.gcode_path, layers=[1, 100], work_dir=tmp_path)
    assert preview.layers_shown == [1, 100]


@pytest.mark.integration
def test_real_quality_review_keeps_project_painting_and_all_heads(
    real_install, selection, tmp_path
):
    model = build_project(tmp_path / "synthetic.3mf")
    report = quality.review_print(
        model, install=real_install, selection=selection, plate=1, work_dir=tmp_path, compare=False
    )
    assert report.status == "visual_review_pending"
    variant = report.variants[0]
    assert variant.error is None
    assert variant.audit.tools_used == [0, 1, 2, 3]
    assert not any(f.severity == "error" for f in variant.audit.findings)
    assert variant.preview.all_layers > 1 and variant.preview.segment_count > 100
    assert variant.audit.feature_extrusion_mm["Prime tower"] > 0
