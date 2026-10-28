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


def test_data_dir_accepts_241_user_data(monkeypatch, tmp_path) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "Snapmaker_Orca.conf").write_text("{}")
    assert install.locate_data_dir(tmp_path) == tmp_path.resolve()


def test_system_dir_uses_profiles_bundled_with_selected_exe(tmp_path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    exe = tmp_path / "app" / "snapmaker-orca.exe"
    profiles = exe.parent / "resources" / "profiles"
    profiles.mkdir(parents=True)
    chosen = install.OrcaInstall(exe=exe, data_dir=data)
    assert chosen.system_dir == profiles
    (data / "system").mkdir()
    assert chosen.system_dir == data / "system"


def test_cli_and_mcp_list_bundled_presets(tmp_path, monkeypatch, capsys) -> None:
    import shutil
    from pathlib import Path

    from print_copilot import cli, mcp_server

    fixture = Path(__file__).parent / "fixtures" / "orca_data"
    data = tmp_path / "data"
    shutil.copytree(fixture / "user", data / "user")
    (data / "Snapmaker_Orca.conf").write_text("{}")
    exe = tmp_path / "app" / "snapmaker-orca.exe"
    profiles = exe.parent / "resources" / "profiles"
    shutil.copytree(fixture / "system", profiles)
    exe.write_bytes(b"")
    assert (
        cli.main(["presets", "--orca-data", str(data), "--orca-exe", str(exe), "--kind", "machine"])
        == 0
    )
    assert "Snapmaker U1" in capsys.readouterr().out
    monkeypatch.setenv(install.ENV_DATA_DIR, str(data))
    monkeypatch.setenv(install.ENV_EXE, str(exe))
    result = mcp_server.list_presets(kind="machine")
    assert any(p["name"] == "Snapmaker U1 (0.4 nozzle)" for p in result["presets"]["machine"])
