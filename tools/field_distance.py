"""Choose a reference sector and distance cut from cadence geometry before reading pixels."""

import argparse
from pathlib import Path

import numpy as np

from p9search.policy import DISTANCE_CUTS
from p9search.science.geometry import SectorGeometry, SkyFrame
from p9search.storage import write_json


def audit(directory, sector_ids, reference):
    frame = SkyFrame(directory / f"s{reference:04d}")
    sectors = [SectorGeometry(directory / f"s{s:04d}", frame) for s in sector_ids]
    scale = max(np.linalg.norm(s.jacobian, 2) for s in sectors)
    duration = max(np.std(s.geom["dt"][s.geom["keep"]]) for s in sectors)
    step = 0.25 / (duration * scale)
    rng = np.random.default_rng(2026110206)
    worst = np.zeros(len(DISTANCE_CUTS))
    for _ in range(48):
        distance = np.exp(rng.uniform(np.log(200), np.log(1500)))
        angle = rng.uniform(0, 2 * np.pi)
        angular = (
            0.85
            * np.sqrt(2)
            * 0.01720209895
            / distance**1.5
            * np.array([np.cos(angle), np.sin(angle)])
        )
        parameters = np.r_[rng.uniform(25, 115, 2), angular, 1 / distance]
        error = np.rint(angular / step) * step - angular
        for sector in sectors:
            path = sector.approximate_positions(parameters)
            path += sector.geom["dt"][:, None] * (error @ sector.jacobian.T)
            delta = sector.exact_positions(parameters) - path
            rms = np.sqrt(np.mean(np.sum(delta**2, axis=1)))
            if not np.isfinite(rms):
                raise ValueError("Nonfinite field geometry")
            covered = np.asarray(DISTANCE_CUTS) <= distance
            worst[covered] = np.maximum(worst[covered], rms)
    return dict(zip(DISTANCE_CUTS, worst.tolist(), strict=True))


def choose(directory, sectors, original):
    if original not in sectors:
        raise ValueError("Reference sector must be in the field")
    refs = dict.fromkeys([original, sorted(sectors)[len(sectors) // 2]])
    audits = {r: audit(directory, sectors, r) for r in refs}
    for minimum in DISTANCE_CUTS:
        for ref in refs:
            if audits[ref][minimum] <= 0.7:
                return dict(
                    reference_sector=ref,
                    distance_au=[minimum, 1500],
                    threshold_pixels=0.7,
                    max_rms_by_reference=audits,
                )
    raise ValueError("No audited distance range passes 0.7 pixels")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("geometry", type=Path)
    parser.add_argument("--sectors", nargs="+", type=int, required=True)
    parser.add_argument("--reference", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    write_json(args.out, choose(args.geometry, args.sectors, args.reference))
