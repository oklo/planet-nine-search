"""Build a task's local physical envelope from the pinned reference population."""

import argparse
from pathlib import Path

from p9search.science.geometry import SectorGeometry, SkyFrame, grids
from p9search.science.prior_population import LocalPriorPopulation
from p9search.storage import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("geometry", type=Path)
    parser.add_argument("population", type=Path)
    parser.add_argument("--reference", type=int, required=True)
    parser.add_argument("--sectors", type=int, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.reference not in args.sectors:
        raise ValueError("Reference sector must be included")
    frame = SkyFrame(args.geometry / f"s{args.reference:04d}")
    sectors = [SectorGeometry(args.geometry / f"s{sid:04d}", frame) for sid in args.sectors]
    _, grid = grids(sectors)
    write_json(args.out, LocalPriorPopulation(args.population).context(frame, grid))


if __name__ == "__main__":
    main()
