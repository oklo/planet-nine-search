"""Freeze projected bright-star positions and lattice spacing before bundling a field."""

import argparse
from pathlib import Path

from p9search.catalog import catalog
from p9search.science.geometry import SectorGeometry, SkyFrame, grids
from p9search.storage import read_json, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("geometry", type=Path)
    parser.add_argument("reference", type=int)
    parser.add_argument("sectors", type=int, nargs="+")
    args = parser.parse_args()
    freeze(args.geometry, args.reference, args.sectors)


def freeze(geometry, reference, sector_ids):
    frame = SkyFrame(geometry / f"s{reference:04d}")
    sectors = [SectorGeometry(geometry / f"s{sid:04d}", frame) for sid in sector_ids]
    _, grid = grids(sectors)
    updates = []
    for sector in sectors:
        path = sector.directory / "metadata.json"
        meta = read_json(path)
        meta["gaia_dr3_bright_axes"] = catalog(sector.directory, detector=True)
        if sector.meta["sector"] == reference:
            meta["motion_lattice"] = grid
        updates.append((path, meta))
    for path, meta in updates:
        write_json(path, meta)


if __name__ == "__main__":
    main()
