"""GPU PRF rendering and vectorized native-to-analysis template accumulation."""

import numpy as np

from .spline_point_response import tess_pixel_model


def bin_template(positions, metadata, cadences, size=140, device=None):
    positions = np.asarray(positions, float)
    centers = np.rint(positions).astype(int)
    lower = np.maximum(0, centers.min(axis=0) - 6)
    upper = np.minimum(size, centers.max(axis=0) + 7)
    if np.any(upper <= lower):
        raise ValueError("Template never enters the detector patch")
    nt, nk, exposure = (cadences[k] for k in ["native_time", "native_keep", "exposure"])
    if len(positions) != len(nt):
        raise ValueError("Native path length mismatch")
    cadence = float(np.median(np.diff(nt)))
    width = 30 / (24 * 60)
    if cadence >= 0.75 * width:
        labels = np.arange(len(nt))
        weight = np.ones(len(nt))
        selected = np.ones(len(nt), bool)
    else:
        group = np.floor((nt - nt[0] + 0.5 * cadence) / width).astype(int)
        _, labels = np.unique(group, return_inverse=True)
        count = np.bincount(labels, weights=nk)
        selected = nk | (count[labels] == 0)
        weight = np.where(selected, exposure, 0.0)
        weight /= np.bincount(labels, weights=weight)[labels]
    shape = (labels.max() + 1, upper[1] - lower[1], upper[0] - lower[0])
    contributes = (
        selected
        & (centers[:, 0] + 6 >= 0)
        & (centers[:, 0] - 6 < size)
        & (centers[:, 1] + 6 >= 0)
        & (centers[:, 1] - 6 < size)
    )
    indices = np.flatnonzero(contributes)
    if len(indices):
        models = tess_pixel_model(
            positions[indices, 0], positions[indices, 1], metadata, radius=6, device=device
        ).astype(np.float32)
        delta = np.arange(-6, 7)
        x = centers[indices, 0, None, None] + delta[None, None] - lower[0]
        y = centers[indices, 1, None, None] + delta[None, :, None] - lower[1]
        inside = (x >= 0) & (x < shape[2]) & (y >= 0) & (y < shape[1])
        linear = (labels[indices, None, None] * shape[1] + y) * shape[2] + x
        values = models * weight[indices, None, None]
        template = (
            np.bincount(linear[inside], weights=values[inside], minlength=int(np.prod(shape)))
            .reshape(shape)
            .astype(np.float32)
        )
    else:
        template = np.zeros(shape, np.float32)
    return template, (slice(None), slice(lower[1], upper[1]), slice(lower[0], upper[0]))
