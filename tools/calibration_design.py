"""Freeze disjoint catalog samples and off-grid injections for a research field."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np
from placement import evaluation_sample

from p9search.science.geometry import SectorGeometry, SkyFrame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("population", type=Path)
    parser.add_argument("geometry", type=Path)
    parser.add_argument("reference_sector", type=int)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    frame = SkyFrame(args.geometry / f"s{args.reference_sector:04d}")
    sectors = [
        SectorGeometry(p, frame) for p in sorted(args.geometry.glob("s[0-9][0-9][0-9][0-9]"))
    ]
    population = args.population.read_bytes()
    rows = csv.DictReader(
        (line for line in population.decode().splitlines() if not line.startswith("#")),
        skipinitialspace=True,
    )
    halves = {"placement": [], "evaluation": []}
    for row in rows:
        index = int(row["index"])
        bit = int(evaluation_sample(index))
        halves[("placement", "evaluation")[bit]].append(index)
    sector_identity = [
        hashlib.sha256((s.directory / "geometry.npz").read_bytes()).hexdigest() for s in sectors
    ]
    seed = int.from_bytes(hashlib.sha256("".join(sector_identity).encode()).digest()[:8], "big")
    rng = np.random.default_rng(seed)
    trials = []
    for _ in range(20000):
        d = np.exp(rng.uniform(np.log(200), np.log(1200)))
        # Tangential velocity below local escape speed, uniform in disk area.
        v = np.sqrt(rng.random() * 2 / d) * 0.0172021 / d
        angle = rng.uniform(0, 2 * np.pi)
        p = [*rng.uniform(7, 132, size=2), v * np.cos(angle), v * np.sin(angle), 1 / d]
        anchors = 0
        for s in sectors:
            xy = s.exact_positions(p, kepler=True)[s.geom["keep"].astype(bool)]
            anchors += bool(len(xy) and np.mean(((xy >= 6) & (xy <= 133)).all(axis=1)) >= 0.95)
        if anchors >= 3:
            trials.append(p)
        if len(trials) == 32:
            break
    if len(trials) < 32:
        raise ValueError("Insufficient three-sector geometry for this calibration design")
    mags = rng.permutation(np.repeat([18.5, 19, 19.5, 20, 20.5, 21, 21.5, 22], 4))
    injections = [
        dict(
            parameters=p,
            flux=10 ** ((20.44 - m) / 2.5),
            radial_velocity=0.0,
            label=f"calibration {i}",
        )
        for i, (p, m) in enumerate(zip(trials, mags, strict=True))
    ]
    with args.output.open("x") as output:
        json.dump(
            dict(
                population_sha256=hashlib.sha256(population).hexdigest(),
                geometry_sha256=sector_identity,
                reference_sector=args.reference_sector,
                samples=halves,
                seed=seed,
                injections=injections,
            ),
            output,
        )


if __name__ == "__main__":
    main()
