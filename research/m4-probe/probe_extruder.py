"""Probe: does per-object `extruder` metadata in a 3MF reach the Snapmaker Orca CLI?

Usage: python probe_extruder.py <input.3mf> <work_dir> [--plate] [object_id=extruder ...]

Writes a copy of the 3MF with the extruder metadata (and, with --plate, a
plate section listing every object), slices it with the selection from
Snapmaker_Orca.conf and prints the tool changes in the G-code.
"""

import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

from print_copilot.actions import run_slice, select_presets
from print_copilot.orca.install import locate
from print_copilot.orca.presets import PresetLibrary


def plate_xml(object_ids: list[str]) -> str:
    instances = "".join(
        "    <model_instance>\n"
        f'      <metadata key="object_id" value="{oid}"/>\n'
        '      <metadata key="instance_id" value="0"/>\n'
        "    </model_instance>\n"
        for oid in object_ids
    )
    return f'  <plate>\n    <metadata key="plater_id" value="1"/>\n{instances}  </plate>\n'


def rewrite(src: Path, dst: Path, assign: dict[str, str], plate: bool) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "Metadata/model_settings.config":
                text = data.decode()
                for oid, ext in assign.items():
                    text = text.replace(
                        f'<object id="{oid}">',
                        f'<object id="{oid}">\n    <metadata key="extruder" value="{ext}"/>',
                    )
                if plate and "<plate>" not in text:
                    ids = re.findall(r'<object id="(\d+)">', text)
                    text = text.replace("</config>", plate_xml(ids) + "</config>")
                data = text.encode()
            zout.writestr(info, data)


def tool_lines(gcode: Path) -> tuple[Counter, list[str]]:
    tools: Counter = Counter()
    order: list[str] = []
    for line in gcode.read_text(errors="replace").splitlines():
        m = re.match(r"^T(\d+)\b", line)
        if m:
            tools[m.group(1)] += 1
            order.append(m.group(1))
    return tools, order


def main() -> None:
    src, work = Path(sys.argv[1]), Path(sys.argv[2])
    args = sys.argv[3:]
    assign = dict(a.split("=") for a in args if "=" in a)
    work.mkdir(parents=True, exist_ok=True)
    dst = work / f"probe-{src.stem}.3mf"
    rewrite(src, dst, assign, "--plate" in args)
    install = locate()
    selection = select_presets(PresetLibrary(install.data_dir))
    run = run_slice(install, dst, selection, work_dir=work)
    tools, order = tool_lines(run.gcode_path)
    print("run", run.run_dir)
    print("grams per tool", run.result.filament_used_g, "time", run.result.estimated_time_s)
    print("T lines", dict(tools), "sequence head", order[:20])


if __name__ == "__main__":
    main()
