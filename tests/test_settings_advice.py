import dataclasses
from pathlib import Path

import numpy as np
import pytest

from print_copilot import actions, settings_advice
from print_copilot.errors import ModelError
from print_copilot.model.mesh import Mesh, load_stl, write_stl
from print_copilot.model.sections import cut, local_thickness, thickness_profile
from synthetic_project import build_project, with_left_cube_settings

TEE = Path(__file__).parent / "fixtures" / "tee.stl"

# Outward, counter-clockwise faces of a unit cube as corner indices.
CUBE_FACES = [
    (0, 2, 1), (0, 3, 2),  # bottom
    (4, 5, 6), (4, 6, 7),  # top
    (0, 1, 5), (0, 5, 4),  # front (y min)
    (2, 3, 7), (2, 7, 6),  # back (y max)
    (1, 2, 6), (1, 6, 5),  # right (x max)
    (3, 0, 4), (3, 4, 7),  # left (x min)
]  # fmt: skip


def box(low, high) -> np.ndarray:
    (x0, y0, z0), (x1, y1, z1) = low, high
    corners = np.array(
        [
            (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
            (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
        ]
    )  # fmt: skip
    return corners[np.array(CUBE_FACES)]


def fin_mesh(fin_mm: float = 0.6) -> Mesh:
    """A 20 x 20 x 10 mm block with a 20 mm tall, `fin_mm` thick fin on top."""
    return Mesh(
        np.concatenate([box((0, 0, 0), (20, 20, 10)), box((0, 10, 10), (20, 10 + fin_mm, 30))])
    )


def selection(library, **process) -> actions.Selection:
    chosen = actions.select_presets(library)
    settings = {
        **chosen.process.settings,
        "wall_generator": "classic",
        "outer_wall_line_width": "0.42",
        **process,
    }
    machine = {
        **chosen.machine.settings,
        "min_layer_height": ["0.08"],
        "max_layer_height": ["0.32"],
    }
    return dataclasses.replace(
        chosen,
        process=dataclasses.replace(chosen.process, settings=settings),
        machine=dataclasses.replace(chosen.machine, settings=machine),
    )


def test_box_section_and_thickness() -> None:
    mesh = Mesh(box((0, 0, 0), (10, 4, 5)))
    section = cut(mesh, 2.5)
    assert section.lengths_mm.sum() == pytest.approx(28.0)
    thickness, weight = local_thickness(section)
    # Rays from the long sides cross 4 mm, from the short sides 10 mm.
    assert sorted(set(np.round(thickness, 3))) == [4.0, 10.0]
    assert weight.sum() == pytest.approx(28.0)


def test_thickness_profile_finds_the_fin() -> None:
    profile = thickness_profile(fin_mesh(0.6), [0.84, 0.42])
    assert profile.min_thickness_mm == pytest.approx(0.6, abs=1e-3)
    assert profile.thin_shares[0] > 0.3
    assert profile.thin_shares[1] == 0.0
    assert min(profile.thin_heights_mm) > 10
    assert max(profile.thin_heights_mm) < 30


def test_tee_without_purpose_proposes_nothing(library) -> None:
    result = settings_advice.suggest_settings(TEE, selection(library))
    assert result.proposals == []
    assert any("No purpose given" in n for n in result.notes)


def test_fin_model_gets_wall_generator(tmp_path, library) -> None:
    stl = write_stl(fin_mesh(), tmp_path / "fin.stl")
    result = settings_advice.suggest_settings(stl, selection(library))
    [proposal] = result.proposals
    assert (proposal.key, proposal.current, proposal.proposed) == (
        "wall_generator",
        "classic",
        "arachne",
    )
    assert proposal.basis == "geometry heuristic"
    assert [v.overrides for v in result.variants] == [{"wall_generator": "arachne"}]
    # Already variable-width walls: nothing to propose.
    again = settings_advice.suggest_settings(stl, selection(library, wall_generator="arachne"))
    assert again.proposals == []


def test_visual_short_print_gets_finer_layers(library) -> None:
    result = settings_advice.suggest_settings(TEE, selection(library), purpose="visual")
    [proposal] = result.proposals
    assert (proposal.key, proposal.proposed) == ("layer_height", 0.12)
    assert "25.0 mm tall" in proposal.reason
    # Already at or below the visual layer height.
    finer = selection(library, layer_height="0.12")
    assert settings_advice.suggest_settings(TEE, finer, purpose="visual").proposals == []


def test_functional_print_gets_walls_and_infill(library) -> None:
    result = settings_advice.suggest_settings(TEE, selection(library), purpose="functional")
    assert {p.key: p.proposed for p in result.proposals} == {
        "wall_loops": 4,
        "sparse_infill_density": "25%",
    }
    assert {p.basis for p in result.proposals} == {"rule of thumb"}
    labels = [v.display_label() for v in result.variants]
    assert labels == [
        "suggested settings (heuristic)",
        "wall_loops=4",
        "sparse_infill_density=25%",
    ]
    assert result.variants[0].overrides == {"wall_loops": 4, "sparse_infill_density": "25%"}
    strong = selection(library, wall_loops="5", sparse_infill_density="40%")
    assert settings_advice.suggest_settings(TEE, strong, purpose="functional").proposals == []


def test_tall_model_on_a_small_base(tmp_path, library) -> None:
    stl = write_stl(Mesh(box((0, 0, 0), (5, 5, 60))), tmp_path / "pillar.stl")
    result = settings_advice.suggest_settings(stl, selection(library))
    assert result.geometry.height_to_contact == pytest.approx(12.0)
    [proposal] = result.proposals
    assert (proposal.key, proposal.current, proposal.proposed) == (
        "brim_type",
        "auto_brim",
        "outer_only",
    )
    assert "Tall on a small base" in proposal.reason
    # A brim the process already asks for is left alone.
    outer = selection(library, brim_type="outer_only")
    assert settings_advice.suggest_settings(stl, outer).proposals == []


def test_object_settings_in_a_project_are_named(tmp_path, library) -> None:
    project = with_left_cube_settings(
        build_project(tmp_path / "p.3mf"),
        tmp_path / "object.3mf",
        '<metadata key="sparse_infill_density" value="5%"/>',
    )
    result = settings_advice.suggest_settings(project, selection(library), purpose="functional")
    assert "sparse_infill_density" in {p.key for p in result.proposals}
    [note] = [n for n in result.notes if "sets sparse_infill_density=5% itself" in n]
    assert note.startswith("Object 'left cube' (object)")


def test_percentage_line_width(library) -> None:
    sel = selection(library, outer_wall_line_width="105%")
    width, note = settings_advice.outer_line_width(sel)
    assert width == pytest.approx(0.42)
    assert note is None


def test_unknown_purpose(library) -> None:
    with pytest.raises(ModelError, match="Unknown purpose"):
        settings_advice.suggest_settings(TEE, selection(library), purpose="art")


def test_tee_geometry(library) -> None:
    facts = settings_advice.suggest_settings(TEE, selection(library)).geometry
    tee = load_stl(TEE)
    assert facts.face_count == tee.face_count
    assert facts.bed_contact_area_mm2 == pytest.approx(150.0)
    assert facts.thickness.thin_shares == [0.0, 0.0]
