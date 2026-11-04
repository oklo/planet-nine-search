from __future__ import annotations

import numpy as np


def bin_detector_cadences(raw, error, time, keep, x, y, exposure, minutes=30, transform=None):
    """Exposure-average good samples in fixed time bins, propagating errors.

    Empty time bins are omitted; bins with only flagged data remain flagged.
    Source positions use the same exposure weights as the images. The original
    archive arrays are never modified. Cadences already near the requested
    duration are returned unchanged.
    """
    width = minutes / (24 * 60)
    cadence = float(np.median(np.diff(time)))
    if cadence >= 0.75 * width:
        if transform is not None:
            raw = transform(np.asarray(raw).copy(), np.arange(len(time)))
        return raw, error, time, keep, x, y, False
    groups = np.floor((time - time[0] + 0.5 * cadence) / width).astype(int)
    labels = np.unique(groups)
    data = np.empty((len(labels), *raw.shape[1:]), dtype=np.float32)
    uncertainty = np.empty_like(data)
    times = np.empty(len(labels))
    xx = np.empty(len(labels))
    yy = np.empty(len(labels))
    quality = np.empty(len(labels), dtype=bool)
    for j, label in enumerate(labels):
        indices = np.flatnonzero(groups == label)
        good = indices[keep[indices]]
        quality[j] = len(good) > 0
        selected = good if len(good) else indices
        weights = np.asarray(exposure[selected], dtype=float)
        if np.any(~np.isfinite(weights)) or np.any(weights <= 0):
            raise ValueError("Exposure durations must be finite and positive.")
        weights /= weights.sum()
        values = raw[selected]
        if transform is not None:
            values = transform(values.copy(), selected)
        data[j] = np.sum(values * weights[:, None, None], axis=0)
        uncertainty[j] = np.sqrt(np.sum((error[selected] * weights[:, None, None]) ** 2, axis=0))
        times[j] = np.sum(weights * time[selected])
        xx[j] = np.sum(weights * x[selected])
        yy[j] = np.sum(weights * y[selected])
    return data, uncertainty, times, quality, xx, yy, True
