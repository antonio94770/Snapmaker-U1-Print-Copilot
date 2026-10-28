import pytest

from print_copilot.errors import SliceError
from print_copilot.orca.heads import Head, Wanted, assign_heads, delta_e, hex_to_lab

U1_HEADS = [
    Head(0, "Generic PLA", "#FFFFFF", "PLA"),
    Head(1, "Generic PLA", "#000000", "PLA"),
    Head(2, "Generic PLA", "#F4E2C1", "PLA"),
    Head(3, "Generic PLA", "#ED1C24", "PLA"),
]


def test_lab_and_delta_e() -> None:
    assert hex_to_lab("#FFFFFF")[0] == pytest.approx(100, abs=0.1)
    assert hex_to_lab("#000000")[0] == pytest.approx(0, abs=0.1)
    assert delta_e("#FF0000", "#FF0000") == 0
    assert delta_e("#000000", "#FFFFFF") == pytest.approx(100, abs=0.1)
    assert delta_e("#0184D0FF", "#0184D0") == 0  # alpha ignored
    with pytest.raises(SliceError):
        hex_to_lab("red")


def test_nearest_colours_get_distinct_heads() -> None:
    wanted = [Wanted(1, "#5E43B7", "PLA"), Wanted(3, "#000000", "PLA"), Wanted(6, "#FFFFFF", "PLA")]
    result = assign_heads(wanted, U1_HEADS)
    assert result.mapping() == {1: 3, 3: 2, 6: 1}  # 1-based heads: purple -> T2
    purple = result.choices[0]
    assert purple.head == 2 and purple.delta_e > 20
    assert any("load the right colour on T2" in n for n in result.notes)


def test_identical_colours_share_a_head() -> None:
    wanted = [Wanted(n, c, "PLA") for n, c in ((1, "#FFFFFF"), (2, "#000000"), (3, "#FFFFFF"))]
    result = assign_heads(wanted, U1_HEADS)
    assert result.mapping() == {1: 1, 2: 2, 3: 1}
    assert any("same colour and type" in n for n in result.notes)
    separate = assign_heads(wanted, U1_HEADS, merge_identical=False)
    assert len(set(separate.mapping().values())) == 3


def test_type_mismatch_is_penalised() -> None:
    heads = [Head(0, "Generic PETG", "#FFFFFF", "PETG"), Head(1, "Generic PLA", "#EEEEEE", "PLA")]
    result = assign_heads([Wanted(1, "#FFFFFF", "PLA")], heads)
    assert result.choices[0].head == 1


def test_explicit_mapping_and_its_errors() -> None:
    wanted = [Wanted(1, "#FF0000", "PLA"), Wanted(2, "#00FF00", "PLA")]
    result = assign_heads(wanted, U1_HEADS, {2: 0})
    assert result.choices[1].head == 0 and result.choices[1].explicit
    with pytest.raises(SliceError, match="not used"):
        assign_heads(wanted, U1_HEADS, {7: 0})
    with pytest.raises(SliceError, match="T9 does not exist"):
        assign_heads(wanted, U1_HEADS, {1: 9})


def test_more_filaments_than_heads() -> None:
    colours = ["#FF0000", "#EE0000", "#00FF00", "#0000FF", "#FFFF00"]
    wanted = [Wanted(i + 1, c, "PLA") for i, c in enumerate(colours)]
    with pytest.raises(SliceError, match="Closest colour pairs that could share a head: 1\\+2"):
        assign_heads(wanted, U1_HEADS)
    reduced = assign_heads(wanted, U1_HEADS, reduce_to_heads=True)
    mapping = reduced.mapping()
    assert mapping[1] == mapping[2]  # the two reds share a head
    assert len(set(mapping.values())) == 4
    assert any("merged with filament 1" in n for n in reduced.notes)
