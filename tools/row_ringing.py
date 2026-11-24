"""Compare the released foreground fit with zero row curvature near saturated stars."""

import argparse
import gc
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np

from p9search import foreground, runtime
from p9search.archive import acquire
from p9search.science.detector_background import detector_background_columns
from p9search.science.geometry import SectorGeometry, SkyFrame
from p9search.storage import GIB, DiskBudget
from p9search.tasks import fetch_bundle


def constrained(shape, rows):
    h, w = shape
    base = detector_background_columns(shape)
    # All modes with x degree 2 span arbitrary row amplitudes. Replace that
    # subspace by one independently normalized mode per unaffected row.
    removed = [2, w + 2, 2 * w + 2, *range(3 * w + 2, base.shape[1], 3)]
    quadratic = np.linalg.qr(np.vander(np.linspace(-1, 1, w), 3, increasing=True))[0][:, 2]
    keep = np.flatnonzero(~rows)
    modes = np.zeros((h, w, len(keep)), np.float32)
    modes[keep, :, np.arange(len(keep))] = quadratic
    return np.column_stack([np.delete(base, removed, axis=1), modes.reshape(h * w, -1)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path, help="task.json and optionally its field.zip")
    parser.add_argument("sector", type=int)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    spec = json.loads((args.work / "task.json").read_text())
    budget = DiskBudget(args.work, 8 * GIB, 20 * GIB)
    data = fetch_bundle(spec, args.work, budget)
    directory = data / f"s{args.sector:04d}"
    acquire(directory, budget, lambda *a, **kw: None)
    runtime.configure("cpu", 2)
    sector = SectorGeometry(directory, SkyFrame(directory))
    with np.load(directory / "stars.npz") as stars:
        centers = stars["xy"][stars["tmag"] < 8]
    rows = (np.abs(np.arange(140)[:, None] - centers[:, 1]) <= 3).any(axis=1)
    if not rows.any():
        raise ValueError("No in-catalog T<8 star band in this sector")
    raw = np.load(directory / "raw.npy", mmap_mode="r")
    good = np.flatnonzero(sector.geom["keep"])
    sky = np.array([np.nanmedian(raw[i]) for i in good])
    epochs = good[np.argsort(sky)[:128]]
    arms = {}
    for arm in ("released", "zero_curvature"):
        basis = (
            detector_background_columns
            if arm == "released"
            else lambda shape, order: constrained(shape, rows)
        )
        with patch.object(foreground, "detector_background_columns", basis):
            model = foreground.fit(
                sector, empirical_stars=args.sector in spec["policy"]["empirical_star_sectors"]
            )
        image = np.median(model["scene"].residual[epochs], axis=0)
        valid = model["scene"].pixel_weight > 0
        # Median in ten-column bins suppresses isolated stellar cores.
        profiles = np.nanmedian(
            np.where(valid[rows], image[rows], np.nan).reshape(-1, 14, 10), axis=2
        )
        arms[arm] = dict(
            row_profiles=profiles.tolist(),
            absolute_p90=float(np.nanpercentile(abs(profiles), 90)),
            sign_changes=int(((profiles[:, 1:] * profiles[:, :-1]) < 0).sum()),
        )
        del model
        gc.collect()
    with args.result.open("x") as output:
        json.dump(
            dict(
                task_id=spec["task_id"],
                sector=args.sector,
                rows=np.flatnonzero(rows).tolist(),
                arms=arms,
            ),
            output,
        )


if __name__ == "__main__":
    main()
