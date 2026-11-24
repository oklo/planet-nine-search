"""Offline native injections and time-shift controls. Writes no coordinator result."""

import argparse
import gc
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
from band_mask import masked_screen

from p9search import foreground, runtime, search
from p9search.policy import default_policy
from p9search.provenance import source_hash
from p9search.storage import GIB, DiskBudget, read_json, sha256, write_json
from p9search.tasks import PROTOCOL, fetch_bundle, task_id


def shifts(times, keep, rng):
    index = np.arange(len(times))
    for segment in np.split(index, np.flatnonzero(np.diff(times) > 1) + 1):
        valid = segment[keep[segment]]
        if len(valid) >= 4:
            offset = rng.integers(max(1, len(valid) // 4), 1 + 3 * len(valid) // 4)
            index[valid] = np.roll(valid, offset)
    return index


class NullMeasured(Exception):
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path)
    parser.add_argument("design", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--nulls", type=int, default=64)
    parser.add_argument(
        "--axes",
        type=Path,
        help="frozen CCD-wide Gaia rows keyed by sector; masks within three pixels",
    )
    args = parser.parse_args()
    if not 1 <= args.nulls <= 10000:
        parser.error("Choose 1 to 10000 time-shift controls")
    args.out.mkdir(parents=True, exist_ok=True)
    design = read_json(args.design)
    spec = read_json(args.task)
    if spec["reference_sector"] != design["reference_sector"]:
        raise ValueError("Reference sector differs from the calibration design")
    spec.update(protocol=PROTOCOL, policy=spec.get("policy", default_policy()), injections=[])
    spec["task_id"] = task_id(spec)
    identity = dict(
        task=sha256(args.task),
        design=sha256(args.design),
        source=source_hash(),
        experiment=sha256(Path(__file__)),
        nulls=args.nulls,
        axes=sha256(args.axes) if args.axes else None,
    )
    freeze = args.out / "protocol.json"
    if freeze.exists() and read_json(freeze) != identity:
        raise ValueError("Calibration inputs changed")
    write_json(freeze, identity)
    runtime.configure("cpu", 1)
    mask_screen = masked_screen(foreground.screen, read_json(args.axes), 3) if args.axes else None
    budget = DiskBudget(args.out, 12 * GIB, 20 * GIB)
    with tempfile.TemporaryDirectory(dir=args.out) as temp:
        root = Path(temp)
        original = args.out / "inputs"
        original.mkdir(exist_ok=True)
        bundle = args.task.with_name("field.zip")
        if bundle.exists():
            if sha256(bundle) != spec["bundle_sha256"]:
                raise ValueError("Local bundle differs from task")
            if not (original / "field.zip").exists():
                os.link(bundle, original / "field.zip")
        data = fetch_bundle(spec, original, budget)
        geom = [data / f"s{s:04d}" for s in sorted(spec["sectors"])]
        if [sha256(d / "geometry.npz") for d in geom] != design["geometry_sha256"]:
            raise ValueError("Geometry differs from frozen design")
        for d in geom:
            search.acquire(d, budget, lambda *a, **kw: None)
        n = len(design["injections"])
        threshold = search.control_threshold
        for arm in [
            *(f"injection-{i}" for i in range(0, n, 8)),
            *(f"null-{i}" for i in range(args.nulls)),
        ]:
            target = args.out / f"{arm}.json"
            if target.exists():
                if read_json(target)["protocol"] != identity:
                    raise ValueError("Existing measurement belongs to another calibration")
                continue
            label, i = arm.split("-")
            i = int(i)
            spec["injections"] = design["injections"][i : i + 8] if label == "injection" else []
            spec["task_id"] = task_id(spec)
            rng = np.random.default_rng([design["seed"], i])
            with tempfile.TemporaryDirectory(dir=root) as stage:
                stage = Path(stage)
                os.link(original / "field.zip", stage / "field.zip")
                trial = fetch_bundle(spec, stage, budget)
                for d in geom:
                    dest = trial / d.name
                    if label == "injection":
                        (dest / "strips").mkdir()
                        for strip in (d / "strips").glob("*-native.npy"):
                            os.link(strip, dest / "strips" / strip.name)
                    keep = np.load(d / "geometry.npz")
                    permutation = shifts(keep["time"], keep["keep"].astype(bool), rng)
                    for name in ("raw.npy", "error.npy"):
                        if label == "injection":
                            os.link(d / name, dest / name)
                        else:
                            values = np.load(d / name, mmap_mode="r")
                            copied = np.lib.format.open_memmap(
                                dest / name, "w+", values.dtype, values.shape
                            )
                            for t in range(0, len(values), 32):
                                copied[t : t + 32] = values[permutation[t : t + 32]]
                            del copied
                measurement = {}

                def measure(arms):
                    limit, flags, maxima = threshold(arms)
                    measurement["baseline_flags"] = len(flags)
                    if label == "null":
                        scores = [
                            r["native"]["coherent_score"]
                            for r in arms["physical"]
                            if r["native"]
                            and r.get("prior", {}).get("eligible_for_p9_followup", True)
                        ]
                        measurement["maximum"] = max(scores, default=None)
                        raise NullMeasured()
                    return limit, [], maxima  # Measure injection recovery even in a flagged field.

                with ExitStack() as stack:
                    stack.enter_context(patch.object(search, "acquire", lambda *a: None))
                    stack.enter_context(patch.object(search, "control_threshold", measure))
                    if mask_screen is not None:
                        stack.enter_context(patch.object(search, "screen", mask_screen))
                        stack.enter_context(patch.object(foreground, "screen", mask_screen))
                    try:
                        report = search.run(spec, stage, budget)
                        measurement.update(state=report["state"], injections=report["injections"])
                    except NullMeasured:
                        pass
                write_json(target, dict(protocol=identity, **measurement))
                gc.collect()
                print(arm, flush=True)
    # The directory was populated and verified by fetch_bundle/acquire.
    # Keep the compact study records; these inputs are reproducible.
    if original.is_dir() and not original.is_symlink():
        for d in geom:
            for item in [d / "raw.npy", d / "error.npy", *(d / "strips").glob("*-native.npy")]:
                item.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
