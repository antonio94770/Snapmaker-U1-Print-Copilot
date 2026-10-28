import pytest

from print_copilot.errors import ModelError
from print_copilot.orca.paint import paint_states, remap_paint

# Real values from the downloaded test projects (Majora's Mask, Spider-Man).
REAL = [
    "0C",
    "00001C1C1C1C0001C131C051C1C1C31C01C11C1C33001C01C9301C01C1C1C331C31C1C1C01C1C31C31C1C"
    "00001C503031C1C1C01C1C31C33300303",
    "1C1C01C1C21C1C61C01C1C60A200C0C20C0C20C0C20C0C0C0C0C0C0C0AA630C0C1C01C00601C1CA61C0020C"
    "00600C0CAA60C301C1C21C1C60C01C1C061C21C1C21C01C1C61C00AA33",
]


def test_single_leaves() -> None:
    assert paint_states("4") == {1}  # code 0b0100: leaf, state 1
    assert paint_states("8") == {2}
    assert paint_states("0") == {0}
    assert paint_states("0C") == {3}  # 0b1100 then nibble 0: state 0 + 3
    assert paint_states("1C") == {4}
    assert paint_states("FC") == {18}


def test_split_node() -> None:
    # Read from the end: 0x1 = split one side -> two children: leaf 1, leaf 2.
    assert paint_states("841") == {1, 2}


@pytest.mark.parametrize("value", REAL)
def test_real_values_round_trip(value: str) -> None:
    states = paint_states(value)
    assert states
    assert remap_paint(value, {s: s for s in states if s}) == value


def test_remap_changes_states_and_encoding_length() -> None:
    assert remap_paint("0C", {3: 1}) == "4"
    assert remap_paint("4", {1: 5}) == "2C"
    assert remap_paint("841", {1: 4, 2: 1}) == "41C1"
    assert paint_states(remap_paint(REAL[1], {s: 1 for s in range(1, 19)})) <= {0, 1}


def test_unmapped_state_and_bad_values_fail() -> None:
    with pytest.raises(ModelError, match="no target"):
        remap_paint("8", {1: 1})
    with pytest.raises(ModelError, match="Truncated"):
        paint_states("1")
    with pytest.raises(ModelError, match="trailing"):
        paint_states("44")
    with pytest.raises(ModelError, match="not hexadecimal"):
        paint_states("zz")
    with pytest.raises(ModelError, match="cannot be stored"):
        remap_paint("4", {1: 19})
