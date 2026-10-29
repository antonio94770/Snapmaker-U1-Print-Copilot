"""Download the public multi-colour 3MF projects used by the integration tests.

Usage: python download_projects.py <empty folder>

The files are not part of the repository (their licences vary, several are
non-commercial or no-derivatives). The integration tests in
`tests/test_integration_projects.py` use them when the environment variable
PRINT_COPILOT_TEST_PROJECTS points to that folder.

Downloads go through the public Printables GraphQL API, the same calls the
website makes for an anonymous "download" click (checked 2026-10-28).
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

API = "https://api.printables.com/graphql/"
# The API answers 403 to urllib's default User-Agent (checked 2026-10-28).
HEADERS = {"User-Agent": "print-copilot-test-download/0.1"}

# (Printables model id, file id, local name, what it exercises, licence)
PROJECTS = [
    (
        "1561164",
        "6610025",
        "majoras-mask-u1.3mf",
        "saved for the Snapmaker U1, 8 colours, 21 objects on 9 plates, painting, "
        "model_settings ids that match no build item",
        "CC BY-SA",
    ),
    (
        "787670",
        "3349138",
        "nx01.3mf",
        "Bambu Studio 1.8, 5 objects with 44 parts on 4 plates, painting; plate 4's stand "
        "collides with the prime tower at its default position",
        "CC BY",
    ),
    (
        "352180",
        "1564290",
        "spiderman-4color.3mf",
        "Bambu Studio 1.4 (build item with p:path), painted with 4 filaments of 2 colours, "
        "support enforcers",
        "CC BY-NC",
    ),
    (
        "1730141",
        "7216890",
        "reduced_color.3mf",
        "Bambu Studio 2.6, one object painted with 14 filaments",
        "CC BY-NC",
    ),
    (
        "1862466",
        "7782046",
        "mystic-woodland-owl.3mf",
        "PrusaSlicer 2.9 project (Slic3r_PE_model.config)",
        "Standard Digital File License",
    ),
]


def _post(query: str) -> dict:
    request = urllib.request.Request(
        API,
        data=json.dumps({"query": query}).encode(),
        headers={"content-type": "application/json", **HEADERS},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def download(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for model_id, file_id, name, what, licence in PROJECTS:
        target = folder / name
        if target.exists():
            print(f"have {name}")
            continue
        data = _post(
            f'mutation {{ getDownloadLink(id: "{file_id}", printId: "{model_id}", '
            "fileType: stl, source: model_detail) { ok output { link } } }"
        )
        link = data["data"]["getDownloadLink"]["output"]["link"]
        with urllib.request.urlopen(
            urllib.request.Request(link, headers=HEADERS), timeout=600
        ) as response:
            target.write_bytes(response.read())
        print(f"got {name} ({target.stat().st_size} bytes, {licence}): {what}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    download(Path(sys.argv[1]))
