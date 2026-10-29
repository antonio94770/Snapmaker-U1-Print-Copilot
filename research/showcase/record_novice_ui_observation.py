"""Record the separately observed GUI outcome without changing the locked protocol.

Run only for the documented 2026-10-29 observation, after the actual UI probe.
This is a transcription of that observation, not an automated UI assertion.
"""

import json
import sys
from pathlib import Path

import numpy as np

from print_copilot.model.mesh import load_mesh

folder = Path(sys.argv[1]).resolve()
observation = {
    "observation_type": "manual transcription of direct accessibility and screenshot inspection",
    "window_title": "functional-component - Snapmaker Orca",
    "project_opened": True,
    "orientation_visible": "broad closed face on the bed; open housing facing upward",
    "gui_global_wall_loops": 2,
    "gui_global_infill_percent": 15,
    "gui_reslice_total_filament_g_displayed": 96.20,
    "gui_reslice_total_time_displayed": "2h56m",
    "time_precision": "minutes as displayed, not an exact number of seconds",
    "saved_project_wall_loops": 4,
    "saved_project_infill_percent": 25,
    "paired_cli_gcode_wall_loops": 4,
    "paired_cli_gcode_infill_percent": 25,
    "paired_cli_gcode_total_filament_g": 113.11,
    "paired_cli_gcode_time_s": 12835,
    "object_process_overrides": {},
    "ui_actions": ["opened via product tool", "selected Strength tab", "sliced the plate"],
    "ui_setting_values_edited": False,
    "printer_commands_sent": False,
    "gui_settings_match_delivered_gcode": False,
    "cause": "not isolated; export/import inconsistency is observed",
}
(folder / "ui-observation.json").write_text(json.dumps(observation, indent=2), encoding="utf-8")
result_path = folder / "result.json"
result = json.loads(result_path.read_text(encoding="utf-8"))
result["software_flow"] = "CLI preparation completed; GUI delivery failed"
result["overall_novice_delivery"] = "failed"
result["orca_opening"] = "opened and correct geometry visible; process overrides not retained"
result["ui_observation"] = observation
# Verify arbitrary rigid poses rather than comparing axis-aligned bounding
# boxes, whose dimensions legitimately change under an oblique rotation.
orientation = json.loads((folder / "02-orientation.json").read_text(encoding="utf-8"))
chosen = orientation["recommended"]
pose = next(c for c in orientation["analysis"]["candidates"] if c["label"] == chosen["candidate"])
source = load_mesh(folder / "input.stl")
expected = source.transformed(np.array(pose["rotation_matrix"])).placed_on_bed()
actual = load_mesh(chosen["model_path"])
result["rigid_transform_geometry_verified"] = bool(
    np.allclose(expected.triangles, actual.triangles, rtol=1e-6, atol=0.0001)
)
result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(result["overall_novice_delivery"])
