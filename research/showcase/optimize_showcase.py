"""Compare measured candidates against restored saved process settings.

No source geometry, user presets or printer state is modified.
Run from the repository root with its Python environment.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from print_copilot.orca.install import locate
from print_copilot.orca.presets import PresetLibrary
from print_copilot.orca.slicer import CRASHING_PROCESS_KEYS, PROTECTED_KEYS

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "research/showcase/models"
OUT = ROOT / "runs" / f"optimize-showcase-{datetime.now():%Y%m%d-%H%M%S}"
OUT.mkdir(parents=True, exist_ok=False)
COMMANDS = []


def save(name, value):
    path = OUT / name
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return path


def cli(name, *args):
    command = [sys.executable, "-m", "print_copilot.cli", *map(str, args)]
    COMMANDS.append(command)
    save("commands.json", COMMANDS)
    print(f"Running {name}", flush=True)
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=1200
    )
    (OUT / f"{name}.txt").write_text(result.stdout, encoding="utf-8")
    (OUT / f"{name}.stderr.txt").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"{name}: {result.stderr[-3000:]}")
    data = json.loads(result.stdout)
    save(f"{name}.json", data)
    return data


def main():
    source = MODELS / "Haku_Dragon_Diorama_(Spirited_Away)_SM.3mf"
    with zipfile.ZipFile(source) as archive:
        saved = json.loads(archive.read("Metadata/project_settings.config"))
    install = locate()
    library = PresetLibrary(install.data_dir, system_dir=install.system_dir)
    process = library.resolve("process", "0.16mm Standard @Snapmaker U1 (0.4 nozzle)")
    excluded = {
        *PROTECTED_KEYS,
        *CRASHING_PROCESS_KEYS,
        "print_settings_id",
        "filename_format",
        "compatible_printers",
        "compatible_printers_condition",
    }
    restored = {}
    for key, current in process.settings.items():
        if key not in saved or key in excluded:
            continue
        value = saved[key]
        # Saved 3MF settings encode some scalar speeds as identical-value lists.
        if isinstance(value, list) and not isinstance(current, list):
            if not value or len(set(value)) != 1:
                raise ValueError(f"Cannot restore scalar {key}: {value}")
            value = value[0]
        restored[key] = value
    save(
        "baseline-restoration.json",
        {
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "restored_process": restored,
            "excluded_metadata_and_cli_workaround": sorted(excluded),
            "scope": "Saved process fields recognized by installed flattened preset; "
            "machine and filament profiles held constant for all candidates.",
            "selected_preset_prime_volume": process.settings["prime_volume"],
            "other_project_config_keys": sorted(set(saved) - set(restored) - excluded),
        },
    )
    variants = [
        {"label": "Restored saved process", "overrides": restored},
        {
            "label": "U1 default prime volume",
            "overrides": {**restored, "prime_volume": process.settings["prime_volume"]},
        },
        {
            "label": "Normal supports same threshold",
            "overrides": {**restored, "support_type": "normal(auto)"},
        },
        {"label": "No tower experimental", "overrides": {**restored, "enable_prime_tower": False}},
    ]
    spec = save("haku-variants.json", variants)
    cli(
        "haku-comparison",
        "strategies",
        source,
        "--no-printer",
        "--plate",
        "1",
        "--map",
        "1=T0",
        "--map",
        "2=T1",
        "--map",
        "3=T2",
        "--map",
        "4=T3",
        "--variants-file",
        spec,
        "--work-dir",
        OUT / "slices",
        "--json",
    )
    print(f"Evidence: {OUT}", flush=True)


if __name__ == "__main__":
    main()
