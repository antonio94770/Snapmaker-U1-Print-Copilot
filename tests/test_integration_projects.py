"""Multi-colour projects sliced with the installed Snapmaker Orca.

Skipped when Snapmaker Orca is absent. The tests on downloaded projects also
need PRINT_COPILOT_TEST_PROJECTS pointing to a folder filled by
`research/m4-probe/download_projects.py`.

Reference numbers were measured on 01.10.01.50 with 0.20mm Standard and
Generic PLA on all heads; the ranges leave room for slicer updates.
"""

import os
import zipfile
from pathlib import Path

import pytest

from print_copilot import multicolor
from print_copilot.actions import select_presets
from print_copilot.errors import OrcaNotFoundError, SliceError
from print_copilot.orca.install import locate
from print_copilot.orca.presets import PresetLibrary
from synthetic_project import build_project

U1 = "Snapmaker U1 (0.4 nozzle)"
PROCESS = "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"
# The loaded colours stored in the maintainer's Snapmaker_Orca.conf may
# change; the tests fix their own.
HEAD_COLOURS = ["#FFFFFF", "#000000", "#F4E2C1", "#ED1C24"]


@pytest.fixture(scope="module")
def real_install():
    try:
        return locate()
    except OrcaNotFoundError as exc:
        pytest.skip(f"Snapmaker Orca not installed: {exc}")


@pytest.fixture(scope="module")
def selection(real_install):
    chosen = select_presets(
        PresetLibrary(real_install.data_dir), printer=U1, process=PROCESS, filaments=["Generic PLA"]
    )
    chosen.colours = list(HEAD_COLOURS)
    return chosen


@pytest.fixture
def work(tmp_path: Path) -> Path:
    # Short paths: the CLI cannot write into output folders over 229 characters.
    return tmp_path


def downloaded(name: str) -> Path:
    folder = os.environ.get("PRINT_COPILOT_TEST_PROJECTS")
    if not folder or not (Path(folder) / name).is_file():
        pytest.skip(f"{name} not downloaded (set PRINT_COPILOT_TEST_PROJECTS)")
    return Path(folder) / name


@pytest.mark.integration
def test_synthetic_project_heads_and_painting(real_install, selection, work) -> None:
    project = build_project(work / "synthetic.3mf")
    result = multicolor.slice_project(real_install, project, selection, plate=1, work_dir=work)
    # red -> T3, green -> T2 (cream is nearest), blue -> T1, white paint -> T0.
    assert result.assignment.mapping() == {1: 4, 2: 3, 3: 2, 4: 1}
    grams = {h.head: h.total_g for h in result.heads}
    assert set(grams) == {0, 1, 2, 3} and all(g > 0 for g in grams.values())
    assert result.tool_changes > 0
    assert result.prime_tower_g > 0
    assert result.toolpath.tools_used  # every head printed something

    painted_bottom = multicolor.slice_project(
        real_install, project, selection, plate=2, work_dir=work
    )
    # The far cube is white with its bottom face painted black (filament 5).
    assert painted_bottom.assignment.mapping() == {4: 1, 5: 2}
    assert {h.head for h in painted_bottom.heads if h.total_g > 0} == {0, 1}


@pytest.mark.integration
def test_synthetic_project_strategies_and_export(real_install, selection, work) -> None:
    project = build_project(work / "synthetic.3mf")
    comparison = multicolor.compare_strategies(
        real_install, project, selection, plate=1, work_dir=work
    )
    results = {v.label: v for v in comparison.variants}
    base = results["presets as is"].slice
    no_tower = results["no prime tower"].slice
    assert base is not None and no_tower is not None
    assert no_tower.prime_tower_g == 0 < base.prime_tower_g
    assert no_tower.total_filament_g < base.total_filament_g
    assert no_tower.tool_changes == base.tool_changes
    by_object = results["one object at a time"]
    if by_object.slice is not None:
        assert by_object.slice.tool_changes < base.tool_changes

    out = work / "exported.3mf"
    exported = multicolor.export_project(
        real_install, project, selection, out, plate=1, work_dir=work
    )
    with zipfile.ZipFile(exported.path) as archive:
        assert "Metadata/project_settings.config" in archive.namelist()


@pytest.mark.integration
def test_spiderman_identical_colours_share_heads(real_install, selection, work) -> None:
    path = downloaded("spiderman-4color.3mf")
    result = multicolor.slice_project(real_install, path, selection, work_dir=work)
    # Filaments 1/3 are white and 2/4 black: two heads, painting honoured.
    assert result.assignment.mapping() == {1: 1, 2: 2, 3: 1, 4: 2}
    used = {h.head for h in result.heads if h.total_g > 0}
    assert used == {0, 1}
    # 01.10.01.50, 0.20mm Standard: 353 changes, 79.9 g, 4 h 42 min.
    assert 200 < result.tool_changes < 600
    assert 60 < result.total_filament_g < 110


@pytest.mark.integration
def test_nx01_plate4_moves_the_prime_tower(real_install, selection, work) -> None:
    path = downloaded("nx01.3mf")
    result = multicolor.slice_project(real_install, path, selection, plate=4, work_dir=work)
    assert any("prime tower collided" in n for n in result.notes)
    assert result.total_filament_g > 50


@pytest.mark.integration
def test_marge_needs_reduction(real_install, selection, work) -> None:
    path = downloaded("reduced_color.3mf")
    with pytest.raises(SliceError, match="14 filaments"):
        multicolor.slice_project(real_install, path, selection, work_dir=work)
    result = multicolor.slice_project(
        real_install, path, selection, reduce_colours=True, work_dir=work
    )
    assert len(set(result.assignment.mapping().values())) == 4
    assert all(h.total_g > 0 for h in result.heads)


@pytest.mark.integration
def test_majoras_mask_u1_plate(real_install, selection, work) -> None:
    path = downloaded("majoras-mask-u1.3mf")
    summary = multicolor.inspect_project(path, selection)
    assert len(summary.plates) == 9
    # Plate names list the colours the plate needs.
    assert summary.plates[1].name == "black+red+yellow"
    assert [w.colour for w in summary.plates[1].filaments] == ["#000000", "#F7D959", "#DE4343"]
    comparison = multicolor.compare_strategies(
        real_install, path, selection, plate=7, work_dir=work
    )
    labels = {v.label: v for v in comparison.variants}
    assert labels["presets as is"].slice is not None
    # Six small parts side by side: the slicer refuses one at a time.
    refused = labels["one object at a time"]
    assert refused.slice is None and "too close to others" in refused.error
