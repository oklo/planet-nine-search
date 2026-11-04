"""Fast proposal numerators with time-dependent foreground source forwarding.

Apply the spatial/static-source adjoint first, then the moving-foreground
adjoint, then PRF correlation and time blocking. The existing information bank
still omits both spatial and foreground source projection: native scoring and
blind injection recovery remain necessary. No exact noise claim is made.
"""

import numpy as np

from .covariance_hough import temporal_blocks


def source_adjoint_images(scene, temporal, foreground=None, device="cpu"):
    import torch

    if scene.shape != temporal.shape:
        raise ValueError("Scene and temporal shapes differ")
    if foreground is not None and foreground.shape != scene.shape:
        raise ValueError("Foreground shape differs")
    u = scene.time_weight
    v = scene.pixel_weight.ravel()
    dtype = torch.float64 if str(device) == "cpu" else torch.float32

    def tensor(x):
        return torch.as_tensor(np.asarray(x).copy(), device=device, dtype=dtype)

    b = tensor(scene.basis)
    vb = tensor(scene.basis.T * v)
    weighted = np.asarray(temporal.weighted_residual).reshape(len(u), -1)
    correction = tensor(weighted.sum(axis=0, dtype=np.float64) / u.sum())
    output = np.empty(scene.shape, dtype=np.float64 if str(device) == "cpu" else np.float32)
    with torch.no_grad():
        for start in range(0, len(u), 64):
            r = (
                tensor(weighted[start : start + 64])
                - tensor(u[start : start + 64, None]) * correction
            )
            r = r - (r @ b) @ vb
            output[start : start + 64] = r.reshape(-1, *scene.shape[1:]).cpu().numpy()
    if foreground is not None:
        output = foreground.adjoint(output)
    return output


def prepare_foreground_covariance_maps(
    scene, temporal, geom, psf, foreground=None, block_days=0.25, device="cpu"
):
    import torch
    from torch.nn import functional as F

    u = scene.time_weight
    groups, weights, _ = temporal_blocks(geom["time"], u, temporal.bases, block_days)
    values = source_adjoint_images(scene, temporal, foreground, device)
    represented = np.zeros(len(u), bool)
    represented[np.concatenate(groups)] = True
    # A data-derived reference may in principle feed an unweighted epoch into
    # other frames. Never silently discard that numerator when forming blocks.
    if np.any(~represented) and np.any(values[~represented] != 0):
        raise ValueError("Foreground adjoint has support outside weighted time blocks")
    dtype = torch.float64 if str(device) == "cpu" else torch.float32
    kernel = torch.as_tensor(np.asarray(psf)[None, None].copy(), device=device, dtype=dtype)
    numerator = np.empty(scene.shape, np.float32)
    with torch.no_grad():
        for start in range(0, len(u), 64):
            batch = torch.as_tensor(
                values[start : start + 64, None].copy(), device=device, dtype=dtype
            )
            convolved = F.conv2d(batch, kernel, padding=(psf.shape[0] // 2, psf.shape[1] // 2))
            numerator[start : start + 64] = convolved[:, 0].cpu().numpy()
    maps = np.stack([numerator[g].sum(axis=0, dtype=np.float64) for g in groups]).astype(np.float32)
    coarse = {
        name: np.array([np.average(geom[name][g], axis=0, weights=u[g]) for g in groups])
        for name in ["dt", "curvature", "pointing"]
    }
    coarse["weight"] = weights
    return maps, coarse, groups
