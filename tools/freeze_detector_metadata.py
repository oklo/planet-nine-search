"""Freeze projected bright-star positions and lattice spacing before bundling a field."""

import argparse
import hashlib
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from p9search.catalog import catalog
from p9search.provenance import source_hash
from p9search.science.geometry import SectorGeometry, SkyFrame, grids
from p9search.storage import canonical, read_json, sha256, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("geometry", type=Path)
    parser.add_argument("reference", type=int)
    parser.add_argument("sectors", type=int, nargs="+")
    parser.add_argument(
        "--cache", type=Path, help="reuse compact Gaia catalogs across patches on the same detector"
    )
    args = parser.parse_args()
    freeze(args.geometry, args.reference, args.sectors, args.cache)


def detector_stars(directory, meta, cache):
    if cache is None:
        return catalog(directory, detector=True)
    cache.mkdir(parents=True, exist_ok=True)
    with np.load(directory / "cadences.npz") as archive:
        epoch = float(np.mean(archive["time"]))
    key = [
        meta["sector"],
        meta["camera"],
        meta["ccd"],
        epoch,
        sha256(directory / "wcs.txt"),
        source_hash(),
    ]
    record = cache / (hashlib.sha256(canonical(key).encode()).hexdigest() + ".json")
    if not record.exists():
        with tempfile.TemporaryDirectory(dir=cache) as temp:
            root = Path(temp)
            for name in ("wcs.txt", "cadences.npz"):
                shutil.copyfile(directory / name, root / name)
            write_json(
                root / "metadata.json", dict(meta, origin_column=45, origin_row=1, size=2048)
            )
            stars = catalog(root, detector=True)
            staged = root / "catalog.json"
            write_json(staged, {"key": key, "stars": stars})
            staged.replace(record)
    payload = read_json(record)
    if payload["key"] != key:
        raise ValueError("Cached detector catalog identity differs")
    result = []
    for source, x, y, magnitude in payload["stars"]:
        x += 45 - meta["origin_column"]
        y += 1 - meta["origin_row"]
        if -8 <= x < meta["size"] + 8 or -8 <= y < meta["size"] + 8:
            result.append([source, x, y, magnitude])
    return result


def freeze(geometry, reference, sector_ids, cache=None):
    frame = SkyFrame(geometry / f"s{reference:04d}")
    sectors = [SectorGeometry(geometry / f"s{sid:04d}", frame) for sid in sector_ids]
    _, grid = grids(sectors)

    def updated(sector):
        path = sector.directory / "metadata.json"
        meta = read_json(path)
        meta["gaia_dr3_bright_axes"] = detector_stars(sector.directory, meta, cache)
        if sector.meta["sector"] == reference:
            meta["motion_lattice"] = grid
        return path, meta

    with ThreadPoolExecutor(max_workers=2) as pool:
        updates = list(pool.map(updated, sectors))
    for path, meta in updates:
        write_json(path, meta)


if __name__ == "__main__":
    main()
