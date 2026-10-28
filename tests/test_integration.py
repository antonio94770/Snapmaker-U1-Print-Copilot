"""Slice a real model with the installed Snapmaker Orca. Skipped when it is absent."""

from pathlib import Path

import pytest

from print_copilot import actions
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
def test_slice_cube_u1_standard_pla(real_install, real_process_name, tmp_path) -> None:
    library = PresetLibrary(real_install.data_dir, system_dir=real_install.system_dir)
    machine = library.resolve("machine", "Snapmaker U1 (0.4 nozzle)")
    process = library.resolve("process", real_process_name)
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


@pytest.mark.integration
def test_compare_infill_15_vs_30(real_install, real_process_name, tmp_path) -> None:
    library = PresetLibrary(real_install.data_dir, system_dir=real_install.system_dir)
    selection = actions.select_presets(
        library,
        printer="Snapmaker U1 (0.4 nozzle)",
        process=real_process_name,
        filaments=["Generic PLA"],
    )
    comparison = actions.compare_variants(
        real_install,
        CUBE,
        selection,
        [
            actions.Variant(overrides={"sparse_infill_density": "15%"}, label="15%"),
            actions.Variant(overrides={"sparse_infill_density": "30%"}, label="30%"),
        ],
        work_dir=tmp_path,
    )
    low, high = comparison.variants
    # Measured on 01.10.01.50: 3.65 g vs 4.71 g.
    assert high.total_filament_g > low.total_filament_g
    assert high.filament_delta_g > 0
    assert low.layer_count == high.layer_count == 100
    gcode = (high.run_dir / "out" / "plate_1.gcode").read_text(encoding="utf-8")
    assert "; sparse_infill_density = 30%" in gcode


@pytest.mark.integration
def test_orient_tee_prefers_no_support(real_install, real_process_name, tmp_path) -> None:
    library = PresetLibrary(real_install.data_dir, system_dir=real_install.system_dir)
    selection = actions.select_presets(
        library,
        printer="Snapmaker U1 (0.4 nozzle)",
        process=real_process_name,
        filaments=["Generic PLA"],
    )
    tee = Path(__file__).parent / "fixtures" / "tee.stl"
    report = actions.optimize_orientation(
        real_install, tee, selection, top=2, support_modes=["tree"], work_dir=tmp_path
    )
    by_label = {s.candidate: s for s in report.slices}
    # Measured on 01.10.01.50: as loaded the cap needs tree supports (0.97 g of
    # 3.83 g, 20m 41s); lying on its side needs none (2.83 g, 11m 41s).
    assert by_label["as loaded"].support_filament_g > 0.5
    assert report.recommended.support_filament_mm == 0
    assert report.recommended.estimated_time_s < by_label["as loaded"].estimated_time_s
    assert report.notes == []


@pytest.mark.integration
def test_orient_bust_quality_keeps_face_clean(real_install, real_process_name, tmp_path) -> None:
    from print_copilot.model.mesh import write_stl
    from print_copilot.model.orientation import Region
    from synthetic_bust import FACE_BOX_MAX, FACE_BOX_MIN, bust_mesh

    model = write_stl(bust_mesh(), tmp_path / "bust.stl")
    library = PresetLibrary(real_install.data_dir, system_dir=real_install.system_dir)
    selection = actions.select_presets(
        library,
        printer="Snapmaker U1 (0.4 nozzle)",
        process=real_process_name,
        filaments=["Generic PLA"],
    )
    face = Region("face", [0, -1, 0], 70, FACE_BOX_MIN, FACE_BOX_MAX)
    report = actions.optimize_orientation(
        real_install,
        model,
        selection,
        top=2,
        support_modes=["tree"],
        goal="quality",
        regions=[face],
        work_dir=tmp_path / "runs",
    )
    # Measured on 01.10.01.50: upright needs 0.13 g of tree support and no
    # support interface; upside down needs 1.62 g.
    assert report.recommended.candidate == "as loaded"
    assert report.recommended.support_interface_mm == 0
    assert report.recommended.support_filament_g < 0.5
