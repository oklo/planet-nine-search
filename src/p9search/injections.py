"""Inject native float32 pixels before exposure averaging, then refit the scene."""

import numpy as np

from .science.binning import bin_detector_cadences
from .science.point_response import tess_pixel_model


def inject(sector, positions, flux):
    cadences = sector.cadences
    nt = len(cadences["native_time"])
    xy = np.asarray(positions, float)
    if xy.shape != (nt, 2) or not np.isfinite(xy).all() or not np.isfinite(flux) or flux <= 0:
        raise ValueError("Finite native source path and positive flux required")
    centers = np.rint(xy).astype(int)
    overlap = (
        (centers[:, 0] + 6 >= 0)
        & (centers[:, 0] - 6 < 140)
        & (centers[:, 1] + 6 >= 0)
        & (centers[:, 1] - 6 < 140)
    )
    stamps = np.zeros((nt, 13, 13), np.float32)
    stamps[overlap] = (
        tess_pixel_model(xy[overlap, 0], xy[overlap, 1], sector.meta, radius=6).astype(np.float32)
        * flux
    )
    result = np.load(sector.directory / "raw.npy").copy()
    zeros = np.zeros(nt)
    for start in range(0, 140, 4):
        stop = start + 4
        if not np.any(overlap & (centers[:, 1] + 6 >= start) & (centers[:, 1] - 6 < stop)):
            continue
        raw = np.load(sector.directory / "strips" / f"{start:03d}-native.npy", mmap_mode="r")

        def add(values, indices):
            for j, index in enumerate(indices):
                x, y = centers[index]
                x0, x1 = max(0, x - 6), min(140, x + 7)
                y0, y1 = max(start, y - 6), min(stop, y + 7)
                if x0 < x1 and y0 < y1:
                    values[j, y0 - start : y1 - start, x0:x1] += stamps[
                        index, y0 - y + 6 : y1 - y + 6, x0 - x + 6 : x1 - x + 6
                    ]
            return values

        # Error values do not enter image averaging. Original fitted noise
        # weights remain fixed, so no native error cube needs to be retained.
        reduced = bin_detector_cadences(
            raw,
            np.zeros_like(raw),
            cadences["native_time"],
            cadences["native_keep"],
            zeros,
            zeros,
            cadences["exposure"],
            transform=add,
        )
        np.testing.assert_allclose(reduced[2], sector.geom["time"], rtol=0, atol=1e-10)
        result[:, start:stop] = reduced[0]
    return result
