"""Cross-validate a four-times sampled Gaia stellar PRF correction in one sector."""

import argparse
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import requests
from astropy.table import Table
from scipy.ndimage import gaussian_filter, map_coordinates
from scipy.spatial import cKDTree

from p9search.science.point_response import tess_pixel_model


def flux_fit(image, model):
    yy, xx = np.indices(model.shape) - model.shape[0] // 2
    design = np.column_stack([model.ravel(), xx.ravel(), yy.ravel(), np.ones(model.size)])
    finite = np.isfinite(image).ravel()
    coeff = np.linalg.lstsq(design[finite], image.ravel()[finite], rcond=None)[0]
    return coeff[0], image - (design @ coeff).reshape(image.shape)


def colors(ids):
    query = (
        "SELECT source_id,bp_rp FROM gaiadr3.gaia_source WHERE source_id IN ("
        + ",".join(map(str, ids))
        + ")"
    )
    response = requests.post(
        "https://gea.esac.esa.int/tap-server/tap/sync",
        data=dict(REQUEST="doQuery", LANG="ADQL", FORMAT="fits", QUERY=query),
        timeout=(15, 90),
    )
    response.raise_for_status()
    table = Table.read(io.BytesIO(response.content), format="fits")
    return dict(zip(map(int, table["source_id"]), map(float, table["bp_rp"]), strict=True))


def correction(star, nodes):
    return sum(
        a * map_coordinates(grid, star["phase"], order=1, mode="nearest")
        for a, grid in zip(star["features"], nodes, strict=True)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sector", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    meta = json.loads((args.sector / "metadata.json").read_text())
    with np.load(args.sector / "stars.npz") as z:
        xy, mags, ids = z["xy"], z["tmag"], z["source_id"]
    with np.load(args.sector / "geometry.npz") as g:
        keep = g["keep"].astype(bool)
    raw = np.load(args.sector / "raw.npy", mmap_mode="r")
    tree = cKDTree(xy)
    eligible = [
        i
        for i, (pos, mag) in enumerate(zip(xy, mags, strict=True))
        if 9 <= mag <= 14
        and ((pos >= 8) & (pos < 132)).all()
        and not any(j != i and mags[j] < mag + 3 for j in tree.query_ball_point(pos, 10))
    ]
    bp = colors(ids[eligible])
    stars = []
    yy, xx = np.indices((13, 13)) - 6
    for i in eligible:
        x, y = xy[i]
        cx, cy = np.rint(xy[i]).astype(int)
        image = np.nanmedian(raw[keep, cy - 6 : cy + 7, cx - 6 : cx + 7], axis=0)
        if not np.isfinite(image).all() or not np.isfinite(bp.get(ids[i], np.nan)):
            continue
        prf = tess_pixel_model(np.array([x]), np.array([y]), meta, radius=6)[0]
        phase = np.array([28 + 4 * (yy + cy - y), 28 + 4 * (xx + cx - x)])
        features = np.array([1.0, (x - 70) / 70, (y - 70) / 70, np.clip(bp[ids[i]] - 1, -2, 2)])
        held = (
            int.from_bytes(hashlib.sha256(f"prf-v1/{ids[i]}".encode()).digest()[:2], "big") % 4 == 0
        )
        stars.append(
            dict(id=int(ids[i]), image=image, prf=prf, phase=phase, features=features, held=held)
        )
    train = [s for s in stars if not s["held"]]
    test = [s for s in stars if s["held"]]
    if len(train) < 8 or len(test) < 2:
        raise ValueError(f"Too few independent stars ({len(train)} training, {len(test)} test)")
    nodes = np.zeros((4, 57, 57))
    for iteration in range(12):
        normal = np.zeros((57, 57, 4, 4))
        rhs = np.zeros((57, 57, 4))
        for s in train:
            flux, resid = flux_fit(s["image"], s["prf"] + correction(s, nodes))
            if flux <= 0:
                continue
            iy, ix = np.rint(s["phase"]).astype(int).reshape(2, -1)
            a = s["features"]
            np.add.at(normal, (iy, ix), np.outer(a, a))
            np.add.at(rhs, (iy, ix), resid.ravel()[:, None] * a / flux)
        normal = gaussian_filter(normal, (1.5, 1.5, 0, 0)) + np.diag([1, 8, 8, 8])
        rhs = gaussian_filter(rhs, (1.5, 1.5, 0))
        update = np.linalg.solve(normal, rhs[..., None])[..., 0].transpose(2, 0, 1)
        nodes += update
        if abs(update).max() < 1e-5:
            break
    comparison = []
    for s in test:
        errors = []
        for model in (s["prf"], s["prf"] + correction(s, nodes)):
            f, r = flux_fit(s["image"], model)
            errors.append(
                float(np.linalg.norm(r[5:8, 5:8]) / np.linalg.norm((f * model)[5:8, 5:8]))
            )
        comparison.append(dict(source_id=str(s["id"]), library=errors[0], empirical=errors[1]))
    with args.result.open("x") as out:
        json.dump(
            dict(
                sector=meta["sector"],
                training=len(train),
                iterations=iteration + 1,
                held_out=comparison,
            ),
            out,
        )


if __name__ == "__main__":
    main()
