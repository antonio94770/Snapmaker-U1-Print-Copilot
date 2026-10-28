"""Assign a project's filaments to the U1's tool heads.

On the U1 every head has its own nozzle, so a tool change needs no flushing:
slicing the same two-object print with `flush_volumes_matrix` all 280 mm³
or 0/500/50 mm³ gave identical G-code numbers (7.31 g + 6.86 g, 1h 58m 55s,
249 changes; verified on 01.10.01.50). Which head prints which filament
therefore does not change time or material; it only has to match what is
loaded. The assignment picks, for each filament the plate uses, the loaded
head whose colour is closest, preferring the same filament type.

Colour distance is CIE76 delta E in CIELAB (D65): a heuristic, labelled as
such. Below about 2 the colours look the same; above about 20 they are
clearly different.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from print_copilot.errors import SliceError

COLOUR_RE = re.compile(r"^#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$")
# A different filament type weighs like this much colour difference.
TYPE_MISMATCH_PENALTY = 50.0
CLEARLY_DIFFERENT_DELTA_E = 20.0


def hex_to_lab(colour: str) -> tuple[float, float, float]:
    match = COLOUR_RE.match(colour.strip())
    if not match:
        raise SliceError(f"Invalid colour {colour!r}: expected #RRGGBB.")
    value = match.group(1)
    rgb = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    r, g, b = linear
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str) -> float:
    la, lb = hex_to_lab(a), hex_to_lab(b)
    return round(sum((p - q) ** 2 for p, q in zip(la, lb, strict=True)) ** 0.5, 1)


@dataclass
class Head:
    """What is loaded on a tool head (`index` 0 = T0)."""

    index: int
    filament: str
    colour: str | None
    type: str | None


@dataclass
class Wanted:
    """A filament a project plate uses (`number` 1-based, as in the project)."""

    number: int
    colour: str | None
    type: str | None


@dataclass
class HeadChoice:
    filament: int  # project filament number
    head: int  # tool head index, 0 = T0
    project_colour: str | None
    head_colour: str | None
    project_type: str | None
    head_type: str | None
    head_filament: str
    delta_e: float | None  # heuristic colour distance
    type_match: bool | None
    explicit: bool  # given by the user, not chosen


@dataclass
class HeadAssignment:
    choices: list[HeadChoice]
    rule: str
    notes: list[str] = field(default_factory=list)

    def mapping(self) -> dict[int, int]:
        """Project filament number -> head number, both 1-based (the 3MF form)."""
        return {c.filament: c.head + 1 for c in self.choices}


RULE = (
    "filaments of identical colour and type share a head; every other used filament gets a "
    "distinct loaded head; the total colour distance "
    f"(CIE76 delta E, heuristic) plus {TYPE_MISMATCH_PENALTY:g} per filament-type mismatch is "
    "minimised; heads given explicitly are kept"
)


def _cost(want: Wanted, head: Head) -> tuple[float, float | None, bool | None]:
    distance = None
    if want.colour and head.colour:
        distance = delta_e(want.colour, head.colour)
    type_match = None
    if want.type and head.type:
        type_match = want.type.strip().upper() == head.type.strip().upper()
    cost = (distance or 0.0) + (0.0 if type_match is not False else TYPE_MISMATCH_PENALTY)
    return cost, distance, type_match


def _nearest_groups(wanted: Sequence[Wanted], heads: int) -> str:
    """A readable suggestion of which colours could share a head."""
    with_colour = [w for w in wanted if w.colour]
    if len(with_colour) < 2:
        return ""
    pairs = sorted(
        (delta_e(a.colour, b.colour), a.number, b.number)  # type: ignore[arg-type]
        for a, b in itertools.combinations(with_colour, 2)
    )
    shown = ", ".join(f"{a}+{b} (delta E {d:g})" for d, a, b in pairs[: len(wanted) - heads + 2])
    return f" Closest colour pairs that could share a head: {shown}."


def assign_heads(
    wanted: Sequence[Wanted],
    heads: Sequence[Head],
    explicit: Mapping[int, int] | None = None,
    *,
    merge_identical: bool = True,
    reduce_to_heads: bool = False,
) -> HeadAssignment:
    """Choose a head for every wanted filament (see `RULE`).

    `explicit` maps project filament numbers to head indexes (0 = T0); several
    filaments may share a head this way. With `merge_identical`, filaments of
    the same colour and type share one head, which saves tool changes and
    looks the same. With `reduce_to_heads`, more filaments than free heads are
    grouped by nearest colour (`_group_colours`, a heuristic) until they fit;
    without it, more filaments than heads is an error.
    """
    explicit = dict(explicit or {})
    by_number = {w.number: w for w in wanted}
    for number, head in explicit.items():
        if number not in by_number:
            raise SliceError(
                f"Filament {number} is not used on this plate; used: {sorted(by_number)}."
            )
        if not 0 <= head < len(heads):
            raise SliceError(f"Tool head T{head} does not exist (T0..T{len(heads) - 1}).")
    free_wanted = [w for w in wanted if w.number not in explicit]
    followers: dict[int, int] = {}  # filament number -> filament whose head it shares
    if merge_identical:
        leaders: dict[tuple[str, str], int] = {}
        for want in free_wanted:
            if not want.colour:
                continue
            key = (want.colour.upper(), (want.type or "").upper())
            if key in leaders:
                followers[want.number] = leaders[key]
            else:
                leaders[key] = want.number
        free_wanted = [w for w in free_wanted if w.number not in followers]
    free_heads = [h for h in heads if h.index not in set(explicit.values())]
    merged: dict[int, int] = {}
    if reduce_to_heads and len(free_wanted) > len(free_heads) and free_heads:
        merged = _group_colours(free_wanted, len(free_heads))
        free_wanted = [w for w in free_wanted if w.number not in merged]
        followers.update(merged)
    if len(free_wanted) > len(free_heads):
        raise SliceError(
            f"The plate uses {len(wanted)} filaments {sorted(by_number)} but the printer has "
            f"{len(heads)} tool heads. Map filaments to heads explicitly (several filaments may "
            "share a head, which merges their colours)." + _nearest_groups(wanted, len(heads))
        )

    best: tuple[float, tuple[Head, ...]] | None = None
    for combo in itertools.permutations(free_heads, len(free_wanted)):
        total = sum(_cost(w, h)[0] for w, h in zip(free_wanted, combo, strict=True))
        if best is None or total < best[0] - 1e-9:
            best = (total, combo)
    chosen = dict(zip((w.number for w in free_wanted), best[1] if best else (), strict=True))
    for number, leader in followers.items():
        chosen[number] = chosen[leader]

    choices = []
    notes = []
    for want in sorted(wanted, key=lambda w: w.number):
        is_explicit = want.number in explicit
        head = heads[explicit[want.number]] if is_explicit else chosen[want.number]
        _, distance, type_match = _cost(want, head)
        choices.append(
            HeadChoice(
                filament=want.number,
                head=head.index,
                project_colour=want.colour,
                head_colour=head.colour,
                project_type=want.type,
                head_type=head.type,
                head_filament=head.filament,
                delta_e=distance,
                type_match=type_match,
                explicit=is_explicit,
            )
        )
        if want.number in merged:
            pass
        elif distance is not None and distance > CLEARLY_DIFFERENT_DELTA_E:
            notes.append(
                f"Filament {want.number} ({want.colour}) goes to T{head.index}, which has "
                f"{head.colour} loaded (delta E {distance:g}): load the right colour on "
                f"T{head.index} or change the mapping."
            )
        if type_match is False:
            notes.append(
                f"Filament {want.number} is {want.type} but T{head.index} has {head.type} "
                f"({head.filament})."
            )
    for number, leader in sorted(followers.items()):
        if number in merged:
            notes.append(
                f"Filament {number} ({by_number[number].colour}) is merged with filament "
                f"{leader} ({by_number[leader].colour}) on T{chosen[leader].index} to fit "
                f"{len(heads)} heads (nearest colours, heuristic)."
            )
            continue
        notes.append(
            f"Filament {number} has the same colour and type as filament {leader}, so both "
            f"print with T{chosen[leader].index} (fewer tool changes)."
        )
    shared = [h for h, n in _count_heads(choices).items() if n > 1]
    if shared:
        notes.append(f"Several filaments share head(s) {['T' + str(h) for h in shared]}.")
    return HeadAssignment(choices=choices, rule=RULE, notes=notes)


def _group_colours(wanted: Sequence[Wanted], groups: int) -> dict[int, int]:
    """Merge the two groups whose mean CIELAB colours are closest until
    `groups` remain (centroid linkage; single linkage chained most colours of
    a 14-colour test project into one group). Returns member -> group leader
    (the lowest filament number); leaders are not in the result. Filaments
    without a colour cannot be grouped and stay alone."""
    clusters: list[list[Wanted]] = [[w] for w in sorted(wanted, key=lambda w: w.number)]

    def centre(cluster: list[Wanted]) -> tuple[float, ...] | None:
        labs = [hex_to_lab(w.colour) for w in cluster if w.colour]
        if not labs:
            return None
        return tuple(sum(v) / len(labs) for v in zip(*labs, strict=True))

    while len(clusters) > groups:
        best: tuple[float, int, int] | None = None
        for i, j in itertools.combinations(range(len(clusters)), 2):
            a, b = centre(clusters[i]), centre(clusters[j])
            if a is None or b is None:
                continue
            gap = sum((p - q) ** 2 for p, q in zip(a, b, strict=True)) ** 0.5
            if best is None or gap < best[0]:
                best = (gap, i, j)
        if best is None:
            break
        _, i, j = best
        clusters[i] = clusters[i] + clusters[j]
        del clusters[j]
    result: dict[int, int] = {}
    for cluster in clusters:
        leader = min(w.number for w in cluster)
        result.update({w.number: leader for w in cluster if w.number != leader})
    return result


def _count_heads(choices: Sequence[HeadChoice]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for choice in choices:
        counts[choice.head] = counts.get(choice.head, 0) + 1
    return counts
