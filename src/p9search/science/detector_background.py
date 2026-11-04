"""Fixed broad detector backgrounds with independent row/column illumination.

Allow every column a low-order polynomial along its rows, and every row a
low-order polynomial along its columns. The orthogonal tensor construction
removes duplicate modes. These are deterministic spatial nuisances, independent
of the image and candidate positions. Source templates must use the same fit.
"""

import numpy as np


def detector_background_columns(shape, order=2):
    if len(shape) != 2 or order < 0 or int(order) != order or min(shape) <= order:
        raise ValueError("Invalid detector shape or polynomial order")
    h, w = shape
    x = (np.arange(w) - (w - 1) / 2) / max(w / 2, 1)
    y = (np.arange(h) - (h - 1) / 2) / max(h / 2, 1)
    qx = np.linalg.qr(np.vander(x, order + 1, increasing=True), mode="complete")[0]
    qy = np.linalg.qr(np.vander(y, order + 1, increasing=True), mode="complete")[0]
    pairs = [(iy, ix) for iy in range(order + 1) for ix in range(w)]
    pairs += [(iy, ix) for iy in range(order + 1, h) for ix in range(order + 1)]
    columns = np.empty((h * w, len(pairs)), np.float32)
    for j, (iy, ix) in enumerate(pairs):
        columns[:, j] = np.outer(qy[:, iy], qx[:, ix]).ravel()
    return columns
