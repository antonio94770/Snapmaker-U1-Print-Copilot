import pytest

from print_copilot import actions
from print_copilot.errors import ConfError, SliceError
from print_copilot.orca.presets import PresetLibrary

U1 = "Snapmaker U1 (0.4 nozzle)"
U1_PROCESS = "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"


def test_selection_defaults_come_from_conf(library: PresetLibrary) -> None:
    selection = actions.select_presets(library)
    assert selection.machine.name == U1
    assert selection.process.name == U1_PROCESS
    assert [f.name for f in selection.filaments] == ["Generic PLA"] * 4
    assert any("printer" in n and "Snapmaker_Orca.conf" in n for n in selection.notes)


def test_explicit_selection_has_no_notes(library: PresetLibrary) -> None:
    selection = actions.select_presets(
        library, printer=U1, process=U1_PROCESS, filaments=["Generic PLA"] * 4
    )
    assert selection.notes == []


def test_single_filament_fills_all_heads(library: PresetLibrary) -> None:
    selection = actions.select_presets(
        library, printer=U1, process=U1_PROCESS, filaments=["My PLA"]
    )
    assert [f.name for f in selection.filaments] == ["My PLA"] * 4
    assert selection.notes == ["using 'My PLA' on all 4 tool heads"]


def test_printer_without_stored_selection_fails(library: PresetLibrary) -> None:
    with pytest.raises(ConfError, match="no stored preset selection"):
        actions.select_presets(library, printer="Some Printer")


def test_list_presets_catalog(library: PresetLibrary) -> None:
    catalog = actions.list_presets(library, "filament", printer=U1)
    names = [e.name for e in catalog.presets["filament"]]
    assert "My PLA" in names
    assert list(catalog.presets) == ["filament"]


def test_compare_variants_tabulates_measured_numbers(
    fake_orca, fake_install, model, tmp_path, library
) -> None:
    selection = actions.select_presets(library)
    comparison = actions.compare_variants(
        fake_install,
        model,
        selection,
        [
            actions.Variant(overrides={}),
            actions.Variant(overrides={"sparse_infill_density": "30%"}, label="dense"),
        ],
        work_dir=tmp_path / "runs",
    )
    first, second = comparison.variants
    assert first.label == "presets as is"
    assert (first.estimated_time_s, first.total_filament_g) == (901, 3.65)
    assert second.label == "dense"
    assert (second.estimated_time_s, second.total_filament_g) == (908, 4.71)
    assert second.time_delta_s == 7
    assert second.filament_delta_g == pytest.approx(1.06)
    assert second.filament_used_g == [4.71, 0.0, 0.0, 0.0]
    assert second.overrides == {"sparse_infill_density": "30%"}
    assert first.run_dir != second.run_dir
    assert comparison.machine == U1


def test_compare_checks_every_variant_before_slicing(
    fake_orca, fake_install, model, tmp_path, library
) -> None:
    selection = actions.select_presets(library)
    variants = [
        actions.Variant(overrides={}),
        actions.Variant(overrides={"sparse_infil_density": "30%"}),
    ]
    with pytest.raises(SliceError, match=r"Variant 'sparse_infil_density=30%'.*Unknown process"):
        actions.compare_variants(fake_install, model, selection, variants, work_dir=tmp_path)
    assert fake_orca.commands == []


def test_compare_needs_variants(fake_install, model, library) -> None:
    with pytest.raises(SliceError, match="at least one variant"):
        actions.compare_variants(fake_install, model, actions.select_presets(library), [])
