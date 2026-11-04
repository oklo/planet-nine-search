from __future__ import annotations

import time

import numpy as np
import torch
from scipy.linalg import cho_factor, cho_solve
from scipy.ndimage import binary_dilation

from ..runtime import device


def gaia_design(xy, flux, shape, radius=4, factor=2):
    """Bilinear ePSF basis evaluated at pixel minus catalog-star coordinates."""
    h, w = shape
    n = (2 * radius + 1) * factor + 1
    design = np.zeros((h * w, n * n + 3), np.float32)
    centers = np.rint(xy).astype(int)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            px = centers[:, 0] + dx
            py = centers[:, 1] + dy
            keep = (px >= 0) & (px < w) & (py >= 0) & (py < h)
            pixel = py[keep] * w + px[keep]
            coordinates = (np.column_stack([px[keep], py[keep]]) - xy[keep]) * factor + n // 2
            nodes = np.floor(coordinates).astype(int)
            nodes = np.clip(nodes, 0, n - 2)
            frac = coordinates - nodes
            for ox, oy in [(0, 0), (1, 0), (0, 1), (1, 1)]:
                weight = (frac[:, 0] if ox else 1 - frac[:, 0]) * (
                    frac[:, 1] if oy else 1 - frac[:, 1]
                )
                np.add.at(
                    design, (pixel, (nodes[:, 1] + oy) * n + nodes[:, 0] + ox), flux[keep] * weight
                )
    yy, xx = np.indices(shape)
    design[:, -3:] = np.column_stack(
        [np.ones(h * w), (xx.ravel() - w / 2) / w, (yy.ravel() - h / 2) / h]
    )
    return design


def linear_scene_subtract(raw, keep, design, ridge=1e-3):
    reference = np.nanmedian(raw[keep], axis=0)
    excluded = binary_dilation((reference > 80000) | ~np.isfinite(reference), iterations=1)
    weights = np.where(excluded.ravel(), 0, 1 / np.maximum(np.abs(reference.ravel()), 1) ** 0.8)
    scale = np.sqrt(np.mean((design * weights[:, None]) ** 2, axis=0))
    scale = np.maximum(scale, 1e-12)
    a = design / scale
    weighted = a * weights[:, None]
    normal = weighted.T.astype(float) @ weighted.astype(float)
    regularization = ridge * np.median(np.diag(normal))
    normal += np.eye(normal.shape[0]) * regularization
    projector = cho_solve(cho_factor(normal), weighted.T * weights).astype(np.float32)
    dev = device()
    at = torch.as_tensor(a.T, device=dev)
    pt = torch.as_tensor(projector.T, device=dev)
    out = np.empty_like(raw)
    with torch.no_grad():
        for start in range(0, len(raw), 64):
            data = torch.as_tensor(
                np.nan_to_num(raw[start : start + 64]).reshape(-1, raw.shape[1] * raw.shape[2]),
                device=dev,
            )
            residual = data - (data @ pt) @ at
            out[start : start + 64] = residual.cpu().numpy().reshape(-1, *raw.shape[1:])
    return out, excluded


def temporal_reference(cube, t, keep, gap=1.5, outer=3.0, knot_days=0.25):
    """Fixed-pixel gapped medians at 6-hour knots, linearly interpolated.

    This approximation and its source attenuation are identical in both arms
    and measured by raw-image injection. No moving-source protection is used.
    """
    knots = np.linspace(t.min(), t.max(), int(np.ceil(np.ptp(t) / knot_days)) + 1)
    medians = []
    for knot in knots:
        use = keep & (abs(t - knot) >= gap) & (abs(t - knot) <= outer)
        if use.sum() < 5:
            raise ValueError("Insufficient gapped-reference data")
        selected = cube[use]
        medians.append(
            (np.median if np.isfinite(selected).all() else np.nanmedian)(selected, axis=0)
        )
    medians = np.asarray(medians)
    idx = np.clip(np.searchsorted(knots, t) - 1, 0, len(knots) - 2)
    weight = (t - knots[idx]) / (knots[idx + 1] - knots[idx])
    return (
        cube - (1 - weight[:, None, None]) * medians[idx] - weight[:, None, None] * medians[idx + 1]
    )


def blind_common_modes(residual, keep, n_modes=8):
    """Spatially cross-fit nuisance modes without any source-track masks.

    For each of four interleaved sets of 14-pixel tiles, temporal modes come
    only from the other tiles. Injected sources pass through this same operator;
    attenuation is assessed by the full raw-injection recovery experiment.
    """
    from scipy.linalg import eigh

    t, h, w = residual.shape
    yy, xx = np.indices((h, w))
    group = (xx // 14 % 2) + 2 * (yy // 14 % 2)
    matrix = residual[keep].reshape(keep.sum(), -1)
    scale = 1.4826 * np.median(np.abs(np.diff(matrix, axis=0)), axis=0) / np.sqrt(2)
    scale = np.maximum(scale, 0.1)
    dev = device()
    result = residual.copy()
    corrected = matrix.copy()
    for fold in range(4):
        training = np.flatnonzero(group.ravel() != fold)[::8]
        testing = np.flatnonzero(group.ravel() == fold)
        values = matrix[:, training] / scale[training]
        values -= np.mean(values, axis=0)
        tensor = torch.as_tensor(values.astype(np.float32), device=dev)
        with torch.no_grad():
            covariance = (tensor @ tensor.T / len(training)).cpu().numpy()
        _, vectors = eigh(
            covariance, subset_by_index=[len(matrix) - n_modes, len(matrix) - 1], check_finite=False
        )
        coefficients = vectors.T @ matrix[:, testing]
        corrected[:, testing] -= vectors @ coefficients
    result[keep] = corrected.reshape(-1, h, w)
    return result


def preprocess(raw, error, t, keep, design, method):
    start = time.perf_counter()
    h, w = raw.shape[1:]
    if method == "empirical":
        yy, xx = np.indices((h, w))
        a = np.column_stack(
            [np.ones(h * w), (xx.ravel() - w / 2) / w, (yy.ravel() - h / 2) / h]
        ).astype(np.float32)
    else:
        a = design
    corrected, excluded = linear_scene_subtract(raw, keep, a)
    residual = temporal_reference(corrected, t, keep).astype(np.float32)
    residual[~keep] = 0
    residual = blind_common_modes(residual, keep)
    noise = 1.4826 * np.nanmedian(np.abs(np.diff(residual[keep], axis=0)), axis=0) / np.sqrt(2)
    nominal = np.nanmedian(error[keep] ** 2, axis=0)
    variance = error**2 * np.maximum(1, noise**2 / np.maximum(nominal, 1e-8))[None]
    # A few unflagged scattered-light exposures otherwise dominate millions of
    # trial trajectories. Estimate their inflation from the whole static field.
    frame_scale = (
        np.median((residual**2 / np.maximum(variance, 1e-8))[:, ~excluded], axis=1) / 0.4549364231
    )
    variance *= np.maximum(1, frame_scale)[:, None, None]
    valid = (
        keep[:, None, None]
        & np.isfinite(residual)
        & np.isfinite(variance)
        & (variance > 0)
        & ~excluded[None]
    )
    inv = np.divide(1, variance, out=np.zeros_like(variance), where=valid)
    return (
        np.nan_to_num(residual),
        inv,
        dict(
            seconds=time.perf_counter() - start,
            masked_pixels=int(excluded.sum()),
            residual_rms=float(np.sqrt(np.mean(residual[valid] ** 2))),
            median_pixel_noise=float(np.median(noise[~excluded])),
            max_frame_variance_inflation=float(np.max(frame_scale)),
            median_frame_variance_inflation=float(np.median(frame_scale[keep])),
        ),
    )
