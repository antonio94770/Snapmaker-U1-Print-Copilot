"""Read and remap multi-colour painting stored in 3MF triangles.

OrcaSlicer-based apps store painted colours per triangle in a `paint_color`
attribute. The value serialises a triangle subdivision tree (TriangleSelector
in the OrcaSlicer source, read from Snapmaker Orca v2.4.0):

- the hex string is read from its last character to its first, one nibble per
  character;
- a node's nibble holds the number of split sides in its two low bits; 0
  means a leaf, otherwise `split + 1` children follow, depth first;
- for a leaf, the two high bits hold the state; `0b11` means the state is
  `next nibble + 3`.

State 0 means "not painted" (the part's own filament); state n >= 1 is
filament n of the project (1-based). Every `paint_color` string of the five
downloaded test projects decodes with no nibble left over.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from print_copilot.errors import ModelError

# A leaf state above this needs more than one extra nibble, which the format
# does not allow (the extra nibble holds state - 3).
MAX_STATE = 15 + 3


def _nibbles(text: str) -> list[int]:
    try:
        return [int(ch, 16) for ch in reversed(text)]
    except ValueError as exc:
        raise ModelError(f"Invalid paint_color value {text[:40]!r}: not hexadecimal.") from exc


def _walk(text: str, visit: Callable[[int], int]) -> str:
    """Rebuild `text` with every leaf state replaced by `visit(state)`."""
    nibbles = _nibbles(text)
    out: list[int] = []
    pos = 0

    def node() -> None:
        nonlocal pos
        if pos >= len(nibbles):
            raise ModelError(f"Truncated paint_color value {text[:40]!r}.")
        code = nibbles[pos]
        pos += 1
        split = code & 0b11
        if split:
            out.append(code)
            for _ in range(split + 1):
                node()
            return
        if code & 0b1100 == 0b1100:
            if pos >= len(nibbles):
                raise ModelError(f"Truncated paint_color value {text[:40]!r}.")
            state = nibbles[pos] + 3
            pos += 1
        else:
            state = code >> 2
        state = visit(state)
        if not 0 <= state <= MAX_STATE:
            raise ModelError(f"Paint state {state} cannot be stored (0..{MAX_STATE}).")
        if state < 3:
            out.append(state << 2)
        else:
            out.extend((0b1100, state - 3))

    node()
    if pos != len(nibbles):
        raise ModelError(f"paint_color value {text[:40]!r} has trailing data.")
    return "".join(f"{n:X}" for n in reversed(out))


def paint_states(text: str) -> set[int]:
    """Every leaf state used by one triangle's `paint_color` value."""
    found: set[int] = set()

    def visit(state: int) -> int:
        found.add(state)
        return state

    _walk(text, visit)
    return found


def remap_paint(text: str, mapping: Mapping[int, int]) -> str:
    """Replace painted filament numbers (1-based) using `mapping`; state 0 is
    kept. A painted state missing from `mapping` is an error."""

    def visit(state: int) -> int:
        if state == 0:
            return 0
        if state not in mapping:
            raise ModelError(f"Painted filament {state} has no target in the mapping.")
        return mapping[state]

    return _walk(text, visit)
