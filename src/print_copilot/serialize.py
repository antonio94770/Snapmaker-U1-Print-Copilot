"""Convert result dataclasses to JSON-friendly values."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any


def to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        data = {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
        # Include derived values so JSON consumers do not have to recompute them.
        for name in ("tool_count", "tools_used"):
            if hasattr(type(value), name):
                data[name] = getattr(value, name)
        return data
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    return value
