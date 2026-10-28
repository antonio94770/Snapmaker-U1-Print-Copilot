"""Locate the Snapmaker Orca executable and its user data directory."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from print_copilot.errors import OrcaNotFoundError

# Verified on Windows 11 with Snapmaker Orca 01.10.01.50 (HANDOFF.md section 3).
DEFAULT_EXE = Path(r"C:\Program Files\Snapmaker_Orca\snapmaker-orca.exe")
DATA_DIR_NAME = "Snapmaker_Orca"

ENV_EXE = "PRINT_COPILOT_ORCA_EXE"
ENV_DATA_DIR = "PRINT_COPILOT_ORCA_DATA"

_MSYS_PATH = re.compile(r"^/([a-zA-Z])(/|$)")


def to_windows_path(path: str | os.PathLike[str]) -> Path:
    """Return an absolute Windows-style path.

    The Snapmaker Orca CLI rejects MSYS paths such as `/c/Users/...` with
    "can not find setting file" (HANDOFF.md section 4, gotcha 2), so they are
    rewritten to `C:/Users/...` before use.
    """
    text = os.fspath(path)
    match = _MSYS_PATH.match(text)
    if match:
        text = f"{match.group(1).upper()}:/{text[match.end() :]}"
    return Path(text).resolve()


def cli_path(path: str | os.PathLike[str]) -> str:
    """Format a path for the Snapmaker Orca command line (`C:/...`)."""
    return to_windows_path(path).as_posix()


@dataclass(frozen=True)
class OrcaInstall:
    """A Snapmaker Orca installation: the executable and the user data dir."""

    exe: Path
    data_dir: Path

    @property
    def system_dir(self) -> Path:
        legacy = self.data_dir / "system"
        if legacy.is_dir():
            return legacy
        # Verified with the installed Windows 2.4.1 release: system presets
        # ship in resources/profiles; user data contains only config + user.
        bundled = self.exe.parent / "resources" / "profiles"
        if bundled.is_dir():
            return bundled
        raise OrcaNotFoundError(
            f"No system presets at {legacy} or {bundled}. Check the selected Orca installation."
        )

    @property
    def user_dir(self) -> Path:
        return self.data_dir / "user"


def default_data_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / DATA_DIR_NAME if appdata else None


def locate_exe(exe: str | os.PathLike[str] | None = None) -> Path:
    """Find `snapmaker-orca.exe`: explicit path, then env var, then default."""
    candidate = exe or os.environ.get(ENV_EXE) or DEFAULT_EXE
    path = to_windows_path(candidate)
    if not path.is_file():
        raise OrcaNotFoundError(
            f"Snapmaker Orca executable not found at {path}. Pass --orca-exe or set {ENV_EXE}."
        )
    return path


def locate_data_dir(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """Find the Snapmaker Orca data dir: explicit path, env var, %APPDATA%."""
    candidate = data_dir or os.environ.get(ENV_DATA_DIR) or default_data_dir()
    if candidate is None:
        raise OrcaNotFoundError(
            f"Cannot locate the Snapmaker Orca data directory: %APPDATA% is not set. "
            f"Pass --orca-data or set {ENV_DATA_DIR}."
        )
    path = to_windows_path(candidate)
    has_user_data = (path / "user").is_dir() and (path / "Snapmaker_Orca.conf").is_file()
    if not (path / "system").is_dir() and not has_user_data:
        raise OrcaNotFoundError(
            f"No Snapmaker Orca data directory at {path} (missing 'system' folder "
            "or user data with Snapmaker_Orca.conf). "
            f"Start Snapmaker Orca once, or pass --orca-data / set {ENV_DATA_DIR}."
        )
    return path


def locate(
    exe: str | os.PathLike[str] | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> OrcaInstall:
    return OrcaInstall(exe=locate_exe(exe), data_dir=locate_data_dir(data_dir))
