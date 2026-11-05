"""Package an operator-reviewed geometry plan; never read or select on image pixels."""

import argparse
import shutil
import tempfile
import zipfile
from pathlib import Path

import numpy as np

from p9search.bundles import ARRAYS, validate_bundle
from p9search.catalog import catalog
from p9search.policy import default_policy
from p9search.storage import GIB, read_json, sha256, write_json
from p9search.tasks import PROTOCOL, task_id, validate_task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "geometry", type=Path, help="directory with sNNNN geometry and observer files"
    )
    parser.add_argument(
        "masks", type=Path, help="conservative motion-cell masks corresponding to this geometry"
    )
    parser.add_argument("--sectors", type=int, nargs="+", required=True)
    parser.add_argument("--reference", type=int, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--injections", type=Path, help="JSON list of predeclared native injection cases"
    )
    parser.add_argument(
        "--base-url", required=True, help="public HTTPS directory for the content-addressed zip"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--empirical-star-sectors", type=int, nargs="*", default=[])
    parser.add_argument("--prior", type=Path, help="Reviewed local-brown-prior-v1 context JSON")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.out) as temporary:
        staging = Path(temporary)
        required = 512 * 2**20
        for sid in args.sectors:
            source = args.geometry / f"s{sid:04d}"
            destination = staging / source.name
            destination.mkdir()
            for name in (
                "metadata.json",
                "wcs.txt",
                "cadences.npz",
                "geometry.npz",
                "native_wcs.npz",
            ):
                shutil.copyfile(source / name, destination / name)
            meta = read_json(destination / "metadata.json")
            if meta["sector"] != sid or meta["size"] != 140:
                raise ValueError("Plan has wrong sector or patch size")
            observer = f"observer_s{sid}.npz"
            shutil.copyfile(args.geometry / observer, staging / observer)
            if (source / "stars.npz").exists():
                shutil.copyfile(source / "stars.npz", destination / "stars.npz")
            else:
                print(f"Query Gaia for sector {sid}", flush=True)
                catalog(destination)
            with np.load(source / "cadences.npz") as cadences:
                required += (
                    (len(cadences["native_time"]) + 4 * len(cadences["time"])) * 140 * 140 * 4
                )
        # Publish only arrays consumed by the worker, without catalog annotations.
        for path in staging.rglob("*.npz"):
            keys = ARRAYS.get(path.name, {"time": None, "position": None})
            with np.load(path, allow_pickle=False) as saved:
                arrays = {
                    key: np.ascontiguousarray(saved[key]) if saved[key].ndim else saved[key]
                    for key in keys
                }
            np.savez_compressed(path, **arrays)
        shutil.copyfile(args.masks, staging / "motion_masks.npz")
        archive = args.out / "field.partial.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    info = zipfile.ZipInfo(
                        path.relative_to(staging).as_posix(), date_time=(2026, 1, 1, 0, 0, 0)
                    )
                    info.compress_type = zipfile.ZIP_DEFLATED
                    bundle.writestr(info, path.read_bytes())
    with zipfile.ZipFile(archive) as bundle:
        validate_bundle(bundle, args.sectors)
    digest = sha256(archive)
    target = args.out / f"{digest}.zip"
    archive.replace(target)
    spec = {
        "protocol": PROTOCOL,
        "policy": {
            **default_policy(),
            "empirical_star_sectors": args.empirical_star_sectors,
            "prior": read_json(args.prior) if args.prior else None,
        },
        "label": args.label,
        "sectors": args.sectors,
        "reference_sector": args.reference,
        "bundle_url": args.base_url.rstrip("/") + "/" + target.name,
        "bundle_sha256": digest,
        "bundle_bytes": target.stat().st_size,
        "required_bytes": int(np.ceil(required / GIB)) * GIB,
        "injections": read_json(args.injections) if args.injections else [],
    }
    spec["task_id"] = task_id(spec)
    validate_task(spec)
    path = args.out / f"{spec['task_id']}.json"
    write_json(path, spec)
    print(path)


if __name__ == "__main__":
    main()
