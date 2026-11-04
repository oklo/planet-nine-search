"""Cross-fitted temporal modes trained in the original measurement-noise units.

The input noise weights, held fixed in native injection tests, replace the
per-pixel MAD estimated anew from each residual cube. This removes one source
of self-normalization. It does not make learned modes independent of data;
native refits and false-alarm calibration remain necessary.
"""

import numpy as np


def learn_noise_weighted_basis(
    data, keep, time_weight, pixel_weight, n_modes=16, tile_size=14, seed=20261102, clip_sigma=30.0
):
    data = np.asarray(data)
    keep = np.asarray(keep, bool)
    u = np.asarray(time_weight, float)
    v = np.asarray(pixel_weight, float)
    if (
        data.ndim != 3
        or keep.shape != (len(data),)
        or u.shape != keep.shape
        or v.shape != data.shape[1:]
    ):
        raise ValueError("Incompatible data, weights and keep mask")
    if (
        not np.isfinite(u).all()
        or not np.isfinite(v).all()
        or np.any(u < 0)
        or np.any(v < 0)
        or not isinstance(n_modes, int)
        or n_modes < 1
        or not isinstance(tile_size, int)
        or tile_size < 1
        or not np.isfinite(clip_sigma)
        or clip_sigma <= 0
    ):
        raise ValueError("Finite nonnegative weights and positive model settings required")
    selected = keep & (u > 0)
    if selected.sum() < n_modes + 2:
        raise ValueError("Insufficient positive-weight epochs")
    t, h, w = data.shape
    yy, xx = np.indices((h, w))
    fold = (xx // tile_size % 2) + 2 * (yy // tile_size % 2)
    valid = (v > 0) & np.isfinite(data[selected]).all(axis=0)
    root_u = np.sqrt(u[selected])
    root_v = np.sqrt(np.where(valid, v, 0.0)).ravel()
    values = np.nan_to_num(
        data[selected].reshape(selected.sum(), -1).astype(float), nan=0.0, posinf=0.0, neginf=0.0
    )
    values[:, ~valid.ravel()] = 0.0
    values *= root_u[:, None] * root_v[None]
    values = np.clip(values, -clip_sigma, clip_sigma)
    constant = root_u / np.linalg.norm(root_u)
    values -= constant[:, None] * (constant @ values)[None]
    bases = []
    for group in range(4):
        columns = np.flatnonzero((fold.ravel() != group) & valid.ravel())[::8]
        a = values[:, columns]
        rank = min(n_modes + 8, min(a.shape) - 1)
        if rank < n_modes:
            raise ValueError("Insufficient independent donor pixels")
        rng = np.random.default_rng(seed + group)
        q, _ = np.linalg.qr(a @ rng.standard_normal((len(columns), rank)), mode="reduced")
        for _ in range(2):
            q, _ = np.linalg.qr(a @ (a.T @ q), mode="reduced")
        left, _, _ = np.linalg.svd(q.T @ a, full_matrices=False)
        modes = (q @ left)[:, :n_modes]
        modes -= constant[:, None] * (constant @ modes)[None]
        modes, _ = np.linalg.qr(modes, mode="reduced")
        basis = np.zeros((t, n_modes + 1), float)
        basis[selected] = np.column_stack([constant, modes]) / root_u[:, None]
        bases.append(basis)
    return np.asarray(bases), fold
