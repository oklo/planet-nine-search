"""Catalog-selected individual stellar flux nuisances for a fixed TESS field.

Each column is a calibrated, finite PRF at a catalog position. Its coefficient
may vary independently in each exposure. Selection never uses a moving-object
candidate or the image residual. Lost moving-source information must be measured
by forwarding its template through the scene projector and by native refits.
"""

import numpy as np

from .point_response import tess_pixel_model


def stellar_columns(
    xy, tmag, metadata, shape, magnitude_limit=14.0, radius=6, renderer=tess_pixel_model
):
    xy = np.asarray(xy, float)
    tmag = np.asarray(tmag, float)
    if xy.shape != (len(tmag), 2) or len(shape) != 2 or min(shape) <= 0:
        raise ValueError("Incompatible catalog or image shape")
    if not np.isfinite(magnitude_limit) or radius < 0 or int(radius) != radius:
        raise ValueError("Invalid magnitude limit or PRF radius")
    height, width = shape
    valid = np.isfinite(xy).all(axis=1) & np.isfinite(tmag) & (tmag <= magnitude_limit)
    # Pixel membership uses exactly the same rounding as native source stamps.
    safe_xy = np.where(np.isfinite(xy), xy, -1e10)
    centers = np.rint(safe_xy).astype(np.int64)
    valid &= (
        (centers[:, 0] + radius >= 0)
        & (centers[:, 0] - radius < width)
        & (centers[:, 1] + radius >= 0)
        & (centers[:, 1] - radius < height)
    )
    indices = np.flatnonzero(valid)
    columns = np.zeros((height * width, len(indices)), np.float32)
    if not len(indices):
        return columns, indices
    stamps = renderer(xy[indices, 0], xy[indices, 1], metadata, radius=radius)
    if (
        stamps.shape != (len(indices), 2 * radius + 1, 2 * radius + 1)
        or not np.isfinite(stamps).all()
    ):
        raise ValueError("Invalid calibrated stellar stamps")
    for j, index in enumerate(indices):
        cx, cy = centers[index]
        x0, x1 = max(0, cx - radius), min(width, cx + radius + 1)
        y0, y1 = max(0, cy - radius), min(height, cy + radius + 1)
        target = columns[:, j].reshape(shape)
        target[y0:y1, x0:x1] = stamps[
            j, y0 - cy + radius : y1 - cy + radius, x0 - cx + radius : x1 - cx + radius
        ]
    # Neither truncated nor edge-clipped stamps are renormalized.
    return columns, indices
