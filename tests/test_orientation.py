from pathlib import Path

import numpy as np
import pytest

from print_copilot.model.mesh import load_stl
from print_copilot.model.orientation import (
    Region,
    analyse_orientations,
    measure,
    oriented_mesh,
    rotation_to_bed,
    stepped_overhang_note,
)

FIXTURES = Path(__file__).parent / "fixtures"
CUBE = Path(__file__).parents[1] / "research" / "cli-probe" / "cube.stl"


@pytest.fixture
def tee():
    # 30 x 15 x 25 mm "T": a 10 mm stem (Z 0..20) under a 30 mm wide, 5 mm thick
    # cap. As loaded, the two 10 x 15 mm undersides of the cap overhang.
    return load_stl(FIXTURES / "tee.stl")


@pytest.mark.parametrize(
    "down", [(0, 0, -1), (0, 0, 1), (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0.6, 0, 0.8)]
)
def test_rotation_to_bed(down) -> None:
    rotation = rotation_to_bed(np.array(down, dtype=float))
    assert np.allclose(rotation @ np.array(down), [0, 0, -1])
    assert np.allclose(rotation @ rotation.T, np.eye(3))
    assert np.linalg.det(rotation) == pytest.approx(1.0)


def test_tee_as_loaded_overhangs(tee) -> None:
    m = measure(tee, np.eye(3))
    assert m.bed_contact_area_mm2 == pytest.approx(150.0)  # stem foot 10 x 15
    assert m.overhang_area_mm2 == pytest.approx(300.0)  # two 10 x 15 cap undersides
    assert m.support_volume_mm3 == pytest.approx(300.0 * 20)  # 20 mm above the bed
    assert m.size_mm == pytest.approx([30, 15, 25])
    assert m.stair_area_mm2 == 0
    assert m.protected_area_mm2 is None


def test_tee_ranking(tee) -> None:
    analysis = analyse_orientations(tee, bed_size_mm=(270, 270, 270))
    labels = [c.label for c in analysis.candidates]
    # Upside down (cap on the bed) and lying on a side need no support.
    best = analysis.candidates[0]
    assert best.label == "+Z down"
    assert best.overhang_area_mm2 == 0
    assert best.bed_contact_area_mm2 == pytest.approx(450.0)
    assert set(labels[1:3]) == {"-Y down", "+Y down"}
    assert labels[-1] == "as loaded"
    assert [c.rank for c in analysis.candidates] == list(range(1, len(labels) + 1))
    assert all(c.fits_bed for c in analysis.candidates)
    assert analysis.open_edges == 0
    assert "heuristic" in analysis.heuristic


def test_threshold_changes_overhang() -> None:
    # A 45 degree surface needs support only when the threshold is above 45.
    mesh = load_stl(CUBE)
    tilt = rotation_to_bed(np.array([np.sin(np.pi / 4), 0, -np.cos(np.pi / 4)]))
    tilted = mesh.transformed(tilt.T)  # puts one face at 45 degrees
    low = measure(tilted, np.eye(3), threshold_deg=30).overhang_area_mm2
    high = measure(tilted, np.eye(3), threshold_deg=60).overhang_area_mm2
    assert low == 0
    assert high > 0


def _cube_tilted(degrees: float):
    angle = np.radians(degrees)
    about_y = np.array(
        [[np.cos(angle), 0, np.sin(angle)], [0, 1, 0], [-np.sin(angle), 0, np.cos(angle)]]
    )
    return load_stl(CUBE).transformed(about_y)


@pytest.mark.parametrize(
    ("tilt", "stepped", "supported"),
    # A tilted cube has two slopes that add up to 90 degrees: 38 and 52 (only the first is
    # stepped), 20 and 70 (the first needs support), 80 and 10 (the second needs support).
    [(38, True, False), (20, False, True), (80, False, True)],
)
def test_stepped_overhang_sits_between_support_threshold_and_45(tilt, stepped, supported) -> None:
    face = measure(load_stl(CUBE), np.eye(3)).bed_contact_area_mm2
    m = measure(_cube_tilted(tilt), np.eye(3), threshold_deg=30)
    assert (m.stepped_overhang_area_mm2 == pytest.approx(face)) is stepped
    assert (m.overhang_area_mm2 == pytest.approx(face)) is supported
    if not stepped:
        assert m.stepped_overhang_area_mm2 == 0


def test_stepped_overhang_note_gives_the_step_per_layer() -> None:
    analysis = analyse_orientations(_cube_tilted(38), threshold_deg=30)
    as_loaded = next(c for c in analysis.candidates if c.origin == "as loaded")
    note = stepped_overhang_note(as_loaded, threshold_deg=30, layer_height_mm=0.2)
    # 0.2 / tan(30 degrees) = 0.35 mm of overhang per layer at the support threshold.
    assert note is not None and "0.35 mm at 0.2 mm layers" in note
    assert "Tell the user" in note
    assert "x the layer height" in stepped_overhang_note(
        as_loaded, threshold_deg=30, layer_height_mm=None
    )


def test_no_stepped_overhang_means_no_note(tee) -> None:
    candidate = analyse_orientations(tee).candidates[0]
    assert stepped_overhang_note(candidate, threshold_deg=30, layer_height_mm=0.2) is None


def test_candidates_that_do_not_fit_rank_last(tee) -> None:
    analysis = analyse_orientations(tee, bed_size_mm=(100, 100, 20))
    fits = [c.fits_bed for c in analysis.candidates]
    assert fits == sorted(fits, reverse=True)  # True before False
    tall = [c for c in analysis.candidates if c.height_mm > 20]
    assert tall and all(c.fits_bed is False for c in tall)


def test_nothing_fits_adds_a_note(tee) -> None:
    analysis = analyse_orientations(tee, bed_size_mm=(5, 5, 5))
    assert any("No candidate" in n for n in analysis.notes)


def test_flat_face_candidates_are_found() -> None:
    # Cube rotated 30 degrees about X: two of its faces are no longer axis-aligned.
    angle = np.radians(30)
    rot = np.array(
        [[1, 0, 0], [0, np.cos(angle), -np.sin(angle)], [0, np.sin(angle), np.cos(angle)]]
    )
    analysis = analyse_orientations(load_stl(CUBE).transformed(rot))
    flat = [c for c in analysis.candidates if c.origin == "flat face"]
    assert flat
    best = analysis.candidates[0]
    assert best.overhang_area_mm2 == 0
    assert best.bed_contact_area_mm2 == pytest.approx(400.0)
    assert best.height_mm == pytest.approx(20.0)


def test_oriented_mesh_sits_on_bed(tee) -> None:
    analysis = analyse_orientations(tee)
    best = analysis.candidates[0]
    placed = oriented_mesh(tee, best)
    low, high = placed.bounds_mm
    assert low[2] == pytest.approx(0)
    assert high[2] == pytest.approx(best.height_mm)
    assert placed.volume_mm3 == pytest.approx(tee.volume_mm3)


def test_bad_threshold_rejected(tee) -> None:
    with pytest.raises(ValueError):
        analyse_orientations(tee, threshold_deg=0)


@pytest.fixture
def bust():
    from synthetic_bust import bust_mesh

    return bust_mesh()


FACE = dict(label="face", direction=[0, -1, 0], angle_deg=70)


def test_region_mask_direction_and_box(bust) -> None:
    from synthetic_bust import FACE_BOX_MAX, FACE_BOX_MIN

    by_direction = Region(**FACE).mask(bust)
    boxed = Region(**FACE, box_min_mm=FACE_BOX_MIN, box_max_mm=FACE_BOX_MAX).mask(bust)
    assert boxed.sum() < by_direction.sum()
    assert (boxed <= by_direction).all()
    centroids = bust.triangles[boxed].mean(axis=1)
    assert (centroids[:, 1] < -3).all()
    assert (centroids[:, 2] > 19).all()


@pytest.mark.parametrize(
    "bad",
    [dict(), dict(direction=[0, 0, 0]), dict(direction=[0, 1]), dict(box_min_mm=[1, 2])],
)
def test_region_validation(bad) -> None:
    with pytest.raises(ValueError):
        Region(**bad).validate()


def test_region_adds_candidates(bust) -> None:
    from synthetic_bust import FACE_BOX_MAX, FACE_BOX_MIN

    region = Region(**FACE, box_min_mm=FACE_BOX_MIN, box_max_mm=FACE_BOX_MAX)
    analysis = analyse_orientations(bust, regions=[region], goal="quality")
    labels = [c.label for c in analysis.candidates]
    assert "face facing up" in labels
    assert any(label.startswith("face tilted 45 deg up") for label in labels)
    assert analysis.protected_area_mm2 > 0
    # Upright is the only orientation standing on its base, and its face is
    # nearly free of supports: it wins the quality ranking.
    best = analysis.candidates[0]
    assert best.label == "as loaded"
    assert best.stable
    assert best.protected_bed_contact_area_mm2 == 0
    upside_down = next(c for c in analysis.candidates if c.label == "+Z down")
    assert not upside_down.stable
    assert upside_down.rank > best.rank


def test_quality_cost_weights_protected_surfaces() -> None:
    from print_copilot.model.orientation import OrientationCandidate, quality_cost

    def cand(**areas):
        base = dict(
            label="x", down_vector=[0, 0, -1], rotation_matrix=[], origin="axis",
            height_mm=1, size_mm=[1, 1, 1], bed_contact_area_mm2=1, overhang_area_mm2=0,
            support_volume_mm3=0, stair_area_mm2=0,
        )  # fmt: skip
        base.update(areas)
        return OrientationCandidate(**base)

    scar_on_face = cand(overhang_area_mm2=10, protected_overhang_area_mm2=10)
    scar_elsewhere = cand(overhang_area_mm2=10, protected_overhang_area_mm2=0)
    assert quality_cost(scar_on_face) == pytest.approx(100)
    assert quality_cost(scar_elsewhere) == pytest.approx(10)
    assert quality_cost(cand(stair_area_mm2=10, protected_stair_area_mm2=10)) == 30


def test_quality_goal_without_regions(tee) -> None:
    analysis = analyse_orientations(tee, goal="quality")
    assert analysis.candidates[0].overhang_area_mm2 == 0
    assert analysis.candidates[0].quality_cost == 0


def test_empty_region_rejected(tee) -> None:
    region = Region(direction=[0, 0, 1], box_min_mm=[100, 100, 100])
    with pytest.raises(ValueError, match="select no surface"):
        analyse_orientations(tee, regions=[region])


def test_unknown_goal_rejected(tee) -> None:
    with pytest.raises(ValueError, match="Unknown goal"):
        analyse_orientations(tee, goal="pretty")
