"""Render calibrated TESS point responses at native detector coordinates."""

from functools import lru_cache

import numpy as np


@lru_cache(maxsize=8)
def _prf(camera, ccd, sector):
    from lkprf import TESSPRF

    return TESSPRF(camera=camera, ccd=ccd, sector=sector)


def tess_pixel_model(x, y, metadata, radius=3):
    """Use exact subpixel phases and a field location within four pixels.

    Group nearby detector positions to avoid reloading/interpolating the
    calibrated spatial PRF for every cadence. Finite stamps are not renormalized.
    """
    if metadata.get("analysis_transposed", False):
        native_metadata = dict(metadata, analysis_transposed=False)
        return tess_pixel_model(y, x, native_metadata, radius).swapaxes(1, 2)
    x, y = np.asarray(x), np.asarray(y)
    dx = x + metadata["origin_column"]
    dy = y + metadata["origin_row"]
    bins = np.column_stack([np.floor(dx / 8), np.floor(dy / 8)]).astype(int)
    result = np.empty((len(x), 2 * radius + 1, 2 * radius + 1))
    model = _prf(metadata["camera"], metadata["ccd"], metadata["sector"])
    for key in np.unique(bins, axis=0):
        selected = np.all(bins == key, axis=1)
        cx, cy = int(np.rint(np.mean(dx[selected]))), int(np.rint(np.mean(dy[selected])))
        targets = list(
            zip(cy + y[selected] - np.rint(y[selected]), cx + x[selected] - np.rint(x[selected]))
        )
        result[selected] = model.evaluate(
            targets=targets,
            origin=(cy - radius, cx - radius),
            shape=(2 * radius + 1, 2 * radius + 1),
        )
    return result
