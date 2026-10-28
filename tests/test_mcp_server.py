"""MCP tools called directly as functions, plus the server's tool listing."""

import asyncio
import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from print_copilot import mcp_server
from print_copilot.orca import install as install_mod
from print_copilot.orca import slicer as slicer_mod

U1 = "Snapmaker U1 (0.4 nozzle)"


@pytest.fixture(autouse=True)
def env(monkeypatch, fake_install, tmp_path):
    monkeypatch.setenv(install_mod.ENV_EXE, str(fake_install.exe))
    monkeypatch.setenv(install_mod.ENV_DATA_DIR, str(fake_install.data_dir))
    monkeypatch.setenv(slicer_mod.ENV_WORK_DIR, str(tmp_path / "runs"))


def test_server_lists_the_tools() -> None:
    tools = asyncio.run(mcp_server.server.list_tools())
    assert sorted(t.name for t in tools) == [
        "analyze_model",
        "compare_variants",
        "get_current_setup",
        "list_presets",
        "optimize_orientation",
        "render_model",
        "slice",
    ]
    for tool in tools:
        assert tool.description
        # render_model returns text plus an image, so it has no output schema.
        assert (tool.output_schema is None) == (tool.name == "render_model")


def test_list_presets() -> None:
    data = mcp_server.list_presets(kind="filament", printer=U1)
    names = [p["name"] for p in data["presets"]["filament"]]
    assert "My PLA" in names


def test_get_current_setup() -> None:
    data = mcp_server.get_current_setup()
    assert data["machine"] == U1
    assert data["filaments"] == ["Generic PLA"] * 4
    assert data["flush_volumes_matrix_mm3"][1][0] == 703.0


def test_slice_with_overrides(fake_orca, model) -> None:
    data = mcp_server.slice(str(model), overrides={"sparse_infill_density": "30%"})
    assert data["result"]["total_filament_g"] == pytest.approx(4.71)
    assert data["overrides"] == {"sparse_infill_density": "30%"}
    assert data["machine"] == U1
    assert any("Snapmaker_Orca.conf" in note for note in data["notes"])


def test_compare_variants(fake_orca, model) -> None:
    data = mcp_server.compare_variants(
        str(model),
        [
            mcp_server.VariantSpec(label="15%"),
            mcp_server.VariantSpec(label="30%", overrides={"sparse_infill_density": "30%"}),
        ],
    )
    table = {v["label"]: v for v in data["variants"]}
    assert table["30%"]["total_filament_g"] > table["15%"]["total_filament_g"]
    assert table["30%"]["time_delta_s"] == 7


def test_errors_become_tool_errors(fake_orca, model) -> None:
    with pytest.raises(ToolError, match="Unknown process setting 'nope'"):
        mcp_server.slice(str(model), overrides={"nope": 1})
    with pytest.raises(ToolError, match="no stored preset selection"):
        mcp_server.get_current_setup(printer="Other")


def test_tool_error_reaches_the_client(fake_orca, model) -> None:
    async def call():
        return await mcp_server.server.call_tool(
            "slice", {"model": str(model), "overrides": {"nope": 1}}
        )

    with pytest.raises(ToolError, match="Error executing tool slice: Unknown process setting"):
        asyncio.run(call())


TEE = str(Path(__file__).parent / "fixtures" / "tee.stl")


def test_analyze_model() -> None:
    data = mcp_server.analyze_model(TEE)
    assert data["candidates"][0]["label"] == "+Z down"
    assert data["threshold_deg"] == 30.0
    assert any("Snapmaker_Orca.conf" in note for note in data["notes"])


def test_optimize_orientation(fake_orca) -> None:
    data = mcp_server.optimize_orientation(TEE, top=1, support_modes=["tree"])
    assert [s["candidate"] for s in data["slices"]] == ["+Z down", "as loaded"]
    assert data["recommended"]["support_mode"] == "tree"
    assert data["recommended"]["support_filament_mm"] == 100.0
    assert data["recommendation_rule"]


def test_optimize_orientation_quality_with_protected_face(fake_orca) -> None:
    data = mcp_server.optimize_orientation(
        TEE,
        goal="quality",
        protect=[mcp_server.RegionSpec(label="cap top", direction=[0, 0, 1], angle_deg=10)],
        top=1,
        support_modes=["tree"],
    )
    assert data["goal"] == "quality"
    assert data["analysis"]["regions"][0]["label"] == "cap top"
    # Upside down puts the protected cap top on the bed, so it is not chosen.
    assert data["recommended"]["candidate"] != "+Z down"
    assert "quality" in data["recommendation_rule"] or "rank" in data["recommendation_rule"]


def test_render_model_returns_text_and_image() -> None:
    from mcp.server.mcpserver import Image

    text, image = mcp_server.render_model(TEE, down_vector=[0, 0, 1], views=["front", "iso"])
    info = json.loads(text)
    assert isinstance(image, Image)
    assert Path(info["png_path"]).is_file()
    assert [v["name"] for v in info["views"]] == ["front", "iso"]
    assert info["face_areas_mm2"]["on_bed"] == pytest.approx(450.0)
    assert info["face_areas_mm2"]["needs_support"] == 0


def test_render_model_through_the_server() -> None:
    async def call():
        return await mcp_server.server.call_tool("render_model", {"model": TEE})

    result = asyncio.run(call())
    kinds = [block.type for block in result.content]
    assert kinds == ["text", "image"]
