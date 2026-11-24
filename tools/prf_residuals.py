"""Measure calibrated PRF residuals on isolated T=10–12 stars in a real sector."""

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from p9search.archive import acquire
from p9search.science.point_response import tess_pixel_model
from p9search.storage import GIB, DiskBudget


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sector_directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.sector_directory
    acquire(root, DiskBudget(root, 6 * GIB, 20 * GIB), lambda *a, **k: None)
    meta = json.loads((root / "metadata.json").read_text())
    raw = np.load(root / "raw.npy", mmap_mode="r")
    with np.load(root / "geometry.npz") as g:
        keep = g["keep"].astype(bool)
    with np.load(root / "stars.npz") as c:
        xy, mag = c["xy"], c["tmag"]
    indices = np.flatnonzero((mag >= 10) & (mag <= 12) & ((xy >= 7) & (xy < 132)).all(axis=1))
    tree = cKDTree(xy)
    measurements = []
    for i in indices:
        if any(j != i and mag[j] < mag[i] + 3 for j in tree.query_ball_point(xy[i], 10)):
            continue
        x, y = xy[i]
        cx, cy = np.rint(xy[i]).astype(int)
        stamp = np.nanmedian(raw[keep, cy - 6 : cy + 7, cx - 6 : cx + 7], axis=0)
        prf = tess_pixel_model(np.array([x]), np.array([y]), meta, radius=6)[0]
        yy, xx = np.indices(stamp.shape) - 6
        design = np.column_stack([prf.ravel(), np.ones(169), xx.ravel(), yy.ravel()])
        valid = np.isfinite(stamp).ravel()
        if valid.sum() < 140 or not np.all(valid.reshape(13, 13)[5:8, 5:8]):
            continue
        coeff = np.linalg.lstsq(design[valid], stamp.ravel()[valid], rcond=None)[0]
        if coeff[0] <= 0:
            continue
        residual = stamp - (design @ coeff).reshape(13, 13)
        core = np.s_[5:8, 5:8]
        error = np.linalg.norm(residual[core]) / np.linalg.norm(coeff[0] * prf[core])
        measurements.append(dict(x=x, y=y, T=mag[i], core_fractional_error=error))
    with args.output.open("x") as out:
        json.dump(dict(sector=meta["sector"], isolated_stars=measurements), out)


if __name__ == "__main__":
    main()
