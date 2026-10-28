import struct
import zlib
from pathlib import Path

import numpy as np
import pytest

from print_copilot.model.mesh import load_stl
from print_copilot.model.render import (
    BACKGROUND,
    CELL_PX,
    VIEWS,
    encode_png,
    render_sheet,
)

FIXTURES = Path(__file__).parent / "fixtures"


def decode_png(data: bytes) -> np.ndarray:
    """Minimal decoder for the PNGs `encode_png` writes (RGB, filter 0)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, width, height = 8, b"", 0, 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + payload) & 0xFFFFFFFF
        if kind == b"IHDR":
            width, height = struct.unpack(">II", payload[:8])
        elif kind == b"IDAT":
            idat += payload
        pos += 12 + length
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(height, width * 3 + 1)
    assert (raw[:, 0] == 0).all()
    return raw[:, 1:].reshape(height, width, 3)


def test_png_round_trip() -> None:
    image = np.zeros((2, 3, 3))
    image[0, 0] = [1, 0, 0]
    pixels = decode_png(encode_png(image))
    assert pixels.shape == (2, 3, 3)
    assert pixels[0, 0].tolist() == [255, 0, 0]


def test_view_axes_are_orthonormal() -> None:
    for name, (right, up) in VIEWS.items():
        assert np.dot(right, up) == pytest.approx(0, abs=1e-9), name
        assert np.linalg.norm(right) == pytest.approx(1)
        assert np.linalg.norm(up) == pytest.approx(1)
    # "front" looks from -Y: the camera side is right x up.
    right, up = VIEWS["front"]
    assert np.cross(right, up).tolist() == [0, -1, 0]


def test_sheet_layout_and_mapping() -> None:
    tee = load_stl(FIXTURES / "tee.stl")
    sheet = render_sheet(tee, views=["front", "top"])
    pixels = decode_png(sheet.png)
    assert (sheet.width_px, sheet.height_px) == (2 * CELL_PX, CELL_PX)
    assert pixels.shape == (CELL_PX, 2 * CELL_PX, 3)
    front = sheet.views[0]
    assert front.cell_px == [0, 0]
    assert sheet.views[1].cell_px == [CELL_PX, 0]
    # The model (30 mm wide) is drawn where the mapping says: the stem centre
    # at x = 15 mm, z = 10 mm is model-coloured, a point left of the cap is not.
    col = int(front.origin_px[0] + 15 / front.mm_per_px)
    row = int(front.origin_px[1] - 10 / front.mm_per_px)
    background = np.round(BACKGROUND * 255).astype(int).tolist()
    assert pixels[row, col].tolist() != background
    outside = int(front.origin_px[0] - 3 / front.mm_per_px)
    assert pixels[row, outside].tolist() != pixels[row, col].tolist()


def test_face_colours_are_used() -> None:
    tee = load_stl(FIXTURES / "tee.stl")
    colors = np.tile([0.0, 0.0, 1.0], (tee.face_count, 1))
    pixels = decode_png(render_sheet(tee, views=["iso"], face_colors=colors).png)
    blueish = (pixels[:, :, 2] > 60) & (pixels[:, :, 0] < 10) & (pixels[:, :, 1] < 10)
    assert blueish.sum() > 1000


def test_legend_cell_is_added() -> None:
    tee = load_stl(FIXTURES / "tee.stl")
    sheet = render_sheet(tee, views=["front"], legend=[(np.array([1.0, 0, 0]), "RED")])
    assert sheet.width_px == 2 * CELL_PX
    assert sheet.legend == {"RED": "#ff0000"}


def test_unknown_view_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown view"):
        render_sheet(load_stl(FIXTURES / "tee.stl"), views=["sideways"])
