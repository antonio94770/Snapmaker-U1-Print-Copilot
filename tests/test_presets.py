import pytest

from print_copilot.errors import PresetError
from print_copilot.orca.presets import PresetLibrary

U1 = "Snapmaker U1 (0.4 nozzle)"
U1_PROCESS = "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"


def test_machine_chain_is_merged_parent_first(library: PresetLibrary) -> None:
    machine = library.resolve("machine", U1)
    assert machine.chain == (U1, "fdm_U1", "fdm_toolchanger")
    # Child overrides parent: fdm_U1 replaces the single-nozzle list.
    assert machine.settings["nozzle_diameter"] == ["0.4"] * 4
    assert machine.settings["printer_model"] == "Snapmaker U1"
    # Inherited from the root.
    assert machine.settings["gcode_flavor"] == "klipper"
    assert "inherits" not in machine.settings
    assert machine.settings["name"] == U1
    assert machine.settings["instantiation"] == "true"


def test_process_keeps_most_specific_value(library: PresetLibrary) -> None:
    process = library.resolve("process", U1_PROCESS)
    assert process.settings["sparse_infill_density"] == "15%"
    assert process.settings["initial_layer_print_height"] == "0.25"
    assert process.settings["layer_height"] == "0.2"
    assert process.compatible_printers == [U1]


def test_filament_parent_found_in_shared_library(library: PresetLibrary) -> None:
    filament = library.resolve("filament", "Generic PLA")
    assert filament.chain == (
        "Generic PLA",
        "Generic PLA @U1 base",
        "fdm_filament_pla",
        "fdm_filament_common",
    )
    assert filament.settings["nozzle_temperature"] == ["220"]
    assert filament.settings["filament_type"] == ["PLA"]
    assert filament.settings["filament_diameter"] == ["1.75"]
    assert filament.ref.path.name == "pla_u1_04.json"  # name differs from file name


def test_user_preset_inherits_system_preset(library: PresetLibrary) -> None:
    filament = library.resolve("filament", "My PLA")
    assert filament.ref.source == "user"
    assert filament.ref.vendor is None
    assert filament.settings["nozzle_temperature"] == ["215"]
    assert filament.settings["filament_type"] == ["PLA"]


def test_resolved_settings_are_independent_copies(library: PresetLibrary) -> None:
    first = library.resolve("machine", U1)
    first.settings["nozzle_diameter"].append("0.8")
    assert library.resolve("machine", U1).settings["nozzle_diameter"] == ["0.4"] * 4


def test_unknown_preset_fails(library: PresetLibrary) -> None:
    with pytest.raises(PresetError, match="No process preset named 'Nope'"):
        library.resolve("process", "Nope")


def test_missing_parent_fails(library: PresetLibrary) -> None:
    with pytest.raises(PresetError, match="inherits 'does_not_exist'"):
        library.resolve("process", "Broken Process")


def test_inherits_cycle_fails(library: PresetLibrary) -> None:
    with pytest.raises(PresetError, match="Cycle"):
        library.resolve("process", "Cycle A")


def test_list_hides_abstract_and_reports_broken(library: PresetLibrary) -> None:
    listing = library.list_presets("process")
    names = [p.name for p in listing.presets]
    assert U1_PROCESS in names
    assert "fdm_process_U1" not in names
    assert len(listing.broken) == 2  # "Broken Process" and "Cycle A"

    with_abstract = library.list_presets("process", include_abstract=True)
    assert "fdm_process_U1" in [p.name for p in with_abstract.presets]


def test_list_filters_by_printer(library: PresetLibrary) -> None:
    process_names = [p.name for p in library.list_presets("process", printer=U1).presets]
    assert U1_PROCESS in process_names
    assert "0.12mm Standard @Snapmaker J1 (0.4 nozzle)" not in process_names

    filament_names = [p.name for p in library.list_presets("filament", printer=U1).presets]
    # Empty compatible_printers means "any printer"; user presets are listed first.
    assert filament_names == ["My PLA", "Generic PLA", "Acme PLA @System"]


def test_invalid_json_is_ignored(library: PresetLibrary) -> None:
    assert all(ref.path.name != "not_json.json" for ref in library.refs("filament"))


def test_preset_with_utf8_bom_is_read(orca_data, tmp_path) -> None:
    import shutil

    data = tmp_path / "data"
    shutil.copytree(orca_data, data)
    refs = PresetLibrary(data).refs("process")
    process = next(ref.path for ref in refs if ref.name == U1_PROCESS)
    process.write_bytes(b"\xef\xbb\xbf" + process.read_bytes())
    resolved = PresetLibrary(data).resolve("process", U1_PROCESS)
    assert resolved.settings["layer_height"] == "0.2"


def test_missing_system_folder_fails(tmp_path) -> None:
    with pytest.raises(PresetError, match="No system presets folder"):
        PresetLibrary(tmp_path)


def test_bundled_system_presets_keep_user_inheritance(orca_data, tmp_path) -> None:
    import shutil

    bundled = tmp_path / "profiles"
    data = tmp_path / "data"
    shutil.copytree(orca_data / "system", bundled)
    shutil.copytree(orca_data / "user", data / "user")
    library = PresetLibrary(data, system_dir=bundled)
    filament = library.resolve("filament", "My PLA")
    assert filament.settings["nozzle_temperature"] == ["215"]
    assert filament.ref.source == "user"
    assert library.resolve("machine", U1).ref.path.is_relative_to(bundled)
