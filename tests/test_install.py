import sys

import pytest

from print_copilot.errors import OrcaNotFoundError
from print_copilot.orca import install


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")
def test_msys_paths_become_windows_paths() -> None:
    assert install.cli_path("/c/Users/someone/cube.stl") == "C:/Users/someone/cube.stl"
    assert install.cli_path(r"D:\models\cube.stl") == "D:/models/cube.stl"


def test_explicit_exe_must_exist(tmp_path) -> None:
    with pytest.raises(OrcaNotFoundError, match="--orca-exe"):
        install.locate_exe(tmp_path / "missing.exe")


def test_exe_from_env(monkeypatch, tmp_path) -> None:
    exe = tmp_path / "snapmaker-orca.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv(install.ENV_EXE, str(exe))
    assert install.locate_exe() == exe.resolve()


def test_data_dir_needs_system_folder(tmp_path) -> None:
    with pytest.raises(OrcaNotFoundError, match="missing 'system' folder"):
        install.locate_data_dir(tmp_path)


def test_data_dir_from_appdata(monkeypatch, tmp_path) -> None:
    (tmp_path / "Snapmaker_Orca" / "system").mkdir(parents=True)
    monkeypatch.delenv(install.ENV_DATA_DIR, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert install.locate_data_dir() == (tmp_path / "Snapmaker_Orca").resolve()
