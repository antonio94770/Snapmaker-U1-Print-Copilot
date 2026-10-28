"""A watertight, miniature-like test model: a bust whose face looks towards -Y.

Surface of revolution around Z (round base, torso, neck, spherical head) with
a nose, a chin and a brow pushed out on the -Y side. As loaded (upright), the
underside of the head and chin overhang the neck, so supports would touch the
lower face: the situation the `quality` goal and protected regions are for.

Run as a script to write `bust.stl` for manual tests:
`python tests/synthetic_bust.py <out.stl>`.
"""

from __future__ import annotations

import sys

import numpy as np

from print_copilot.model.mesh import Mesh, write_stl

HEAD_CENTRE_Z = 26.0
HEAD_RADIUS = 8.0
TOP_Z = HEAD_CENTRE_Z + HEAD_RADIUS
# Region around the face, in model coordinates (mm).
FACE_BOX_MIN = [-7.0, -13.0, 19.0]
FACE_BOX_MAX = [7.0, -3.0, 32.0]


def _radius(z: np.ndarray) -> np.ndarray:
    r = np.empty_like(z)
    base = z <= 3
    r[base] = 12.0
    torso = (z > 3) & (z <= 14)
    r[torso] = 10.0 - 5.0 * (z[torso] - 3) / 11
    neck = (z > 14) & (z <= HEAD_CENTRE_Z - np.sqrt(HEAD_RADIUS**2 - 4.0**2))
    r[neck] = 4.0
    head = z > HEAD_CENTRE_Z - np.sqrt(HEAD_RADIUS**2 - 4.0**2)
    r[head] = np.sqrt(np.clip(HEAD_RADIUS**2 - (z[head] - HEAD_CENTRE_Z) ** 2, 0, None))
    return r


def _bump(z, phi, z0, phi0, amplitude, width_z, width_phi):
    dphi = np.angle(np.exp(1j * (phi - phi0)))
    return amplitude * np.exp(-(((z - z0) / width_z) ** 2) - (dphi / width_phi) ** 2)


def bust_mesh(n_z: int = 90, n_phi: int = 72) -> Mesh:
    z = np.concatenate([[0.0], np.linspace(0.0, TOP_Z, n_z)])
    phi = np.linspace(0, 2 * np.pi, n_phi, endpoint=False)
    zz, pp = np.meshgrid(z, phi, indexing="ij")
    r = _radius(zz)
    r[0] = 0.0  # bottom centre closes the base
    front = -np.pi / 2  # -Y
    in_head = zz > 18
    r = r + in_head * (
        _bump(zz, pp, 26.0, front, 2.5, 1.6, 0.25)  # nose
        + _bump(zz, pp, 20.5, front, 1.2, 1.2, 0.5)  # chin
        + _bump(zz, pp, 28.8, front, 0.8, 0.8, 0.8)  # brow
    )
    r[-1] = 0.0  # top pole
    points = np.stack([r * np.cos(pp), r * np.sin(pp), zz], axis=-1)
    a = points[:-1, :]
    b = np.roll(points[:-1, :], -1, axis=1)
    c = np.roll(points[1:, :], -1, axis=1)
    d = points[1:, :]
    # Counter-clockwise seen from outside: z up, phi increasing.
    lower = np.stack([a, b, c], axis=2).reshape(-1, 3, 3)
    upper = np.stack([a, c, d], axis=2).reshape(-1, 3, 3)
    tris = np.concatenate([lower, upper])
    mesh = Mesh(tris)
    keep = mesh.areas_mm2 > 1e-9  # triangles collapsed at the poles
    return Mesh(mesh.triangles[keep])


if __name__ == "__main__":
    write_stl(bust_mesh(), sys.argv[1] if len(sys.argv) > 1 else "bust.stl")
