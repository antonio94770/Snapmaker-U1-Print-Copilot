import json

import pytest

from print_copilot.errors import SliceError
from print_copilot.orca.presets import PresetLibrary
from print_copilot.orca.slicer import (
    SettingsOverrides,
    apply_overrides,
    prepare_cli_settings,
    slice_model,
)

U1 = "Snapmaker U1 (0.4 nozzle)"
U1_PROCESS = "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"


@pytest.fixture
def presets(library: PresetLibrary):
    machine = library.resolve("machine", U1)
    process = library.resolve("process", U1_PROCESS)
    filaments = [library.resolve("filament", "Generic PLA")] * 4
    return machine, process, filaments


def test_from_flat_splits_sections() -> None:
    overrides = SettingsOverrides.from_flat(
        {"wall_loops": 3, "machine.nozzle_diameter": "0.4", "filament.nozzle_temperature": 215}
    )
    assert overrides.process == {"wall_loops": 3}
    assert overrides.machine == {"nozzle_diameter": "0.4"}
    assert overrides.filament == {"nozzle_temperature": 215}
    assert overrides.to_flat() == {
        "wall_loops": 3,
        "machine.nozzle_diameter": "0.4",
        "filament.nozzle_temperature": 215,
    }
    assert SettingsOverrides.from_flat(None).is_empty()


@pytest.mark.parametrize("key", ["printer.wall_loops", "machine.", "filament.a.b"])
def test_from_flat_rejects_bad_keys(key) -> None:
    with pytest.raises(SliceError, match="Invalid override key"):
        SettingsOverrides.from_flat({key: "1"})


def test_brim_type_can_be_set_although_the_preset_leaves_it_out() -> None:
    settings = {"wall_loops": "2"}
    apply_overrides(settings, {"brim_type": "outer_only"}, section="process", preset_name="P")
    assert settings["brim_type"] == "outer_only"
    with pytest.raises(SliceError, match="Unknown process setting 'brim_typo'"):
        apply_overrides(settings, {"brim_typo": "x"}, section="process", preset_name="P")
    with pytest.raises(SliceError, match="Unknown machine setting 'brim_type'"):
        apply_overrides({}, {"brim_type": "outer_only"}, section="machine", preset_name="M")


def test_values_are_stored_as_preset_strings() -> None:
    settings = {"wall_loops": "2", "enable_support": "0", "density": "15%", "temp": ["220"]}
    apply_overrides(
        settings,
        {"wall_loops": 4, "enable_support": True, "density": "30%", "temp": 215},
        section="process",
        preset_name="P",
    )
    assert settings == {
        "wall_loops": "4",
        "enable_support": "1",
        "density": "30%",
        "temp": ["215"],
    }


def test_scalar_fills_every_list_entry_and_lists_replace() -> None:
    settings = {"nozzle_diameter": ["0.4"] * 4, "retraction_length": ["0.8"] * 4}
    apply_overrides(
        settings,
        {"nozzle_diameter": 0.6, "retraction_length": ["1", "1", "2", "2"]},
        section="machine",
        preset_name="M",
    )
    assert settings["nozzle_diameter"] == ["0.6"] * 4
    assert settings["retraction_length"] == ["1", "1", "2", "2"]


def test_list_for_single_value_is_rejected() -> None:
    with pytest.raises(SliceError, match="single value"):
        apply_overrides(
            {"wall_loops": "2"}, {"wall_loops": ["2"]}, section="process", preset_name="P"
        )


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(SliceError, match=r"Unknown process setting 'wal_loops'.*'P'"):
        apply_overrides({"wall_loops": "2"}, {"wal_loops": 3}, section="process", preset_name="P")


@pytest.mark.parametrize("key", ["name", "inherits", "from"])
def test_identity_keys_are_protected(key) -> None:
    with pytest.raises(SliceError, match="cannot be overridden"):
        apply_overrides({key: "x"}, {key: "y"}, section="process", preset_name="P")


def test_crashing_key_is_rejected() -> None:
    with pytest.raises(SliceError, match="crashes the Snapmaker Orca CLI"):
        apply_overrides(
            {"wipe_tower_filament": "0"},
            {"wipe_tower_filament": "1"},
            section="process",
            preset_name="P",
        )


def test_unsupported_value_type_is_rejected() -> None:
    with pytest.raises(SliceError, match="must be a string"):
        apply_overrides({"a": "1"}, {"a": {"b": 1}}, section="process", preset_name="P")


def test_prepare_applies_overrides_before_workarounds(presets) -> None:
    machine, process, filaments = presets
    overrides = SettingsOverrides.from_flat(
        {"sparse_infill_density": "30%", "filament.nozzle_temperature": 215}
    )
    machine_s, process_s, filament_s = prepare_cli_settings(machine, process, filaments, overrides)
    assert process_s["sparse_infill_density"] == "30%"
    assert "wipe_tower_filament" not in process_s
    assert machine_s["from"] == "system"
    assert all(f["nozzle_temperature"] == ["215"] for f in filament_s)
    # The resolved presets are untouched.
    assert process.settings["sparse_infill_density"] == "15%"
    assert filaments[0].settings["nozzle_temperature"] != ["215"]


def test_slice_records_overrides(fake_orca, fake_install, model, tmp_path, presets) -> None:
    overrides = SettingsOverrides.from_flat({"sparse_infill_density": "30%"})
    run = slice_model(
        fake_install, model, *presets, work_dir=tmp_path / "runs", overrides=overrides
    )
    assert run.overrides == {"sparse_infill_density": "30%"}
    saved = json.loads((run.run_dir / "overrides.json").read_text(encoding="utf-8"))
    assert saved["process"] == {"sparse_infill_density": "30%"}
    sent = json.loads((run.run_dir / "process.json").read_text(encoding="utf-8"))
    assert sent["sparse_infill_density"] == "30%"
    result = json.loads((run.run_dir / "result.json").read_text(encoding="utf-8"))
    assert result["overrides"] == {"sparse_infill_density": "30%"}
    assert run.result.total_filament_g == pytest.approx(4.71)


def test_slice_without_overrides_writes_no_overrides_file(
    fake_orca, fake_install, model, tmp_path, presets
) -> None:
    run = slice_model(fake_install, model, *presets, work_dir=tmp_path / "runs")
    assert run.overrides == {}
    assert not (run.run_dir / "overrides.json").exists()


def test_bad_override_fails_before_running(
    fake_orca, fake_install, model, tmp_path, presets
) -> None:
    with pytest.raises(SliceError, match="Unknown process setting"):
        slice_model(
            fake_install,
            model,
            *presets,
            work_dir=tmp_path / "runs",
            overrides=SettingsOverrides.from_flat({"nope": 1}),
        )
    assert fake_orca.commands == []
    assert not (tmp_path / "runs").exists()
