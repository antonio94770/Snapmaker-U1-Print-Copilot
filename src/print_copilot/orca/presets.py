"""Read Snapmaker Orca presets and flatten their `inherits` chains.

Layout (verified on Snapmaker Orca 01.10.01.50, HANDOFF.md section 3):

    <data_dir>/system/<Vendor>/<kind>/**/*.json  system presets, one vendor per folder
    <data_dir>/user/<profile>/<kind>/*.json      user presets (profile is usually "default")

The shared `OrcaFilamentLibrary` vendor keeps its filaments in subfolders
(`filament/base/`, `filament/Bambu/`, ...), so folders are searched recursively.

Presets are identified by their `name` field, not by file name (for example
`Generic PLA` lives in a file with a different name). A child preset names its
parent in `inherits`; flattening merges parent first, then child.

On the installed Windows 2.4.1 release the system tree lives instead at
<exe_dir>/resources/profiles. Pass that root as system_dir while keeping
user presets under data_dir; OrcaInstall.system_dir selects the verified root.

This module only reads. It never writes into the Snapmaker Orca data directory.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from print_copilot.errors import PresetError

PresetKind = Literal["machine", "process", "filament"]
KINDS: tuple[PresetKind, ...] = ("machine", "process", "filament")

DEFAULT_VENDOR = "Snapmaker"
# Shared filament bases that vendor presets may inherit from.
LIBRARY_VENDOR = "OrcaFilamentLibrary"


@dataclass(frozen=True)
class PresetRef:
    """Where a preset lives, without its resolved settings."""

    name: str
    kind: PresetKind
    source: Literal["system", "user"]
    vendor: str | None  # system vendor folder, or None for user presets
    path: Path
    inherits: str | None
    instantiable: bool  # False for abstract bases such as `fdm_U1`


@dataclass(frozen=True)
class Preset:
    """A preset with its `inherits` chain merged into one flat settings dict."""

    ref: PresetRef
    chain: tuple[str, ...]  # names from the preset itself up to the root
    settings: dict[str, Any]

    @property
    def name(self) -> str:
        return self.ref.name

    @property
    def compatible_printers(self) -> list[str]:
        value = self.settings.get("compatible_printers") or []
        return list(value) if isinstance(value, list) else [value]


@dataclass
class PresetListing:
    """Result of `PresetLibrary.list_presets`."""

    presets: list[Preset] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)  # one message per unresolvable preset


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_instantiable(data: dict[str, Any]) -> bool:
    value = data.get("instantiation")
    if value is None:
        # User presets carry no `instantiation` key; they are always usable.
        return True
    return str(value).lower() == "true"


class PresetLibrary:
    """Index of the system and user presets in a Snapmaker Orca data dir."""

    def __init__(
        self, data_dir: Path, vendor: str = DEFAULT_VENDOR, *, system_dir: Path | None = None
    ) -> None:
        self.data_dir = data_dir
        self.vendor = vendor
        self.system_dir = system_dir if system_dir is not None else data_dir / "system"
        # (kind, vendor) -> name -> (ref, raw json)
        self._system: dict[tuple[str, str], dict[str, tuple[PresetRef, dict[str, Any]]]] = {}
        # kind -> name -> (ref, raw json)
        self._user: dict[str, dict[str, tuple[PresetRef, dict[str, Any]]]] = {}
        self._load()

    def _load(self) -> None:
        system_root = self.system_dir
        if not system_root.is_dir():
            raise PresetError(f"No system presets folder at {system_root}")
        for vendor_dir in sorted(p for p in system_root.iterdir() if p.is_dir()):
            for kind in KINDS:
                index = self._system.setdefault((kind, vendor_dir.name), {})
                for path in sorted((vendor_dir / kind).rglob("*.json")):
                    self._add(index, path, kind, "system", vendor_dir.name)

        user_root = self.data_dir / "user"
        if user_root.is_dir():
            for profile_dir in sorted(p for p in user_root.iterdir() if p.is_dir()):
                for kind in KINDS:
                    index = self._user.setdefault(kind, {})
                    for path in sorted((profile_dir / kind).rglob("*.json")):
                        self._add(index, path, kind, "user", None)

    @staticmethod
    def _add(
        index: dict[str, tuple[PresetRef, dict[str, Any]]],
        path: Path,
        kind: PresetKind,
        source: Literal["system", "user"],
        vendor: str | None,
    ) -> None:
        data = _read_json(path)
        if data is None or not isinstance(data.get("name"), str):
            return
        ref = PresetRef(
            name=data["name"],
            kind=kind,
            source=source,
            vendor=vendor,
            path=path,
            inherits=data.get("inherits") or None,
            instantiable=_is_instantiable(data),
        )
        index.setdefault(ref.name, (ref, data))

    def _vendor_order(self, first: str | None) -> list[str]:
        vendors = sorted({v for (_, v) in self._system})
        order = [v for v in (first, self.vendor, LIBRARY_VENDOR) if v in vendors]
        return list(dict.fromkeys(order + vendors))

    def _find(
        self, kind: PresetKind, name: str, near_vendor: str | None = None, *, user: bool = True
    ) -> tuple[PresetRef, dict[str, Any]]:
        if user and name in self._user.get(kind, {}):
            return self._user[kind][name]
        for vendor in self._vendor_order(near_vendor):
            entry = self._system.get((kind, vendor), {}).get(name)
            if entry is not None:
                return entry
        raise PresetError(f"No {kind} preset named {name!r} in {self.data_dir}")

    def refs(self, kind: PresetKind) -> Iterable[PresetRef]:
        """All presets of a kind: user presets first, then the configured vendor."""
        seen: set[str] = set()
        for ref, _ in self._user.get(kind, {}).values():
            seen.add(ref.name)
            yield ref
        for vendor in self._vendor_order(None):
            for ref, _ in self._system.get((kind, vendor), {}).values():
                if ref.name not in seen:
                    seen.add(ref.name)
                    yield ref

    def resolve(self, kind: PresetKind, name: str) -> Preset:
        """Flatten a preset's `inherits` chain into one settings dict.

        A parent is looked up in the child's own vendor folder first, then in
        the configured vendor and the shared filament library. A missing parent
        or a cycle raises `PresetError`.
        """
        ref, data = self._find(kind, name)
        layers = [data]
        chain = [ref.name]
        vendor = ref.vendor
        parent = ref.inherits
        while parent:
            if parent in chain:
                raise PresetError(
                    f"Cycle in the inherits chain of {kind} preset {name!r}: "
                    + " -> ".join([*chain, parent])
                )
            try:
                parent_ref, parent_data = self._find(kind, parent, vendor, user=False)
            except PresetError as exc:
                raise PresetError(
                    f"{kind} preset {name!r} inherits {parent!r}, which does not exist "
                    f"(chain: {' -> '.join(chain)})"
                ) from exc
            layers.append(parent_data)
            chain.append(parent_ref.name)
            vendor = parent_ref.vendor
            parent = parent_ref.inherits

        settings: dict[str, Any] = {}
        for layer in reversed(layers):
            settings.update(copy.deepcopy(layer))
        settings.pop("inherits", None)
        settings["name"] = ref.name
        return Preset(ref=ref, chain=tuple(chain), settings=settings)

    def list_presets(
        self,
        kind: PresetKind,
        *,
        printer: str | None = None,
        include_abstract: bool = False,
    ) -> PresetListing:
        """Resolved presets of a kind, optionally only those compatible with `printer`.

        Compatibility uses the flattened `compatible_printers` list. An empty
        list means "any printer". `compatible_printers_condition` expressions are
        not evaluated. Presets whose `inherits` chain cannot be resolved are
        reported in `PresetListing.broken` instead of aborting the listing.
        """
        listing = PresetListing()
        for ref in self.refs(kind):
            if not (include_abstract or ref.instantiable):
                continue
            try:
                preset = self.resolve(kind, ref.name)
            except PresetError as exc:
                listing.broken.append(str(exc))
                continue
            compatible = preset.compatible_printers
            if printer and kind != "machine" and compatible and printer not in compatible:
                continue
            listing.presets.append(preset)
        return listing
