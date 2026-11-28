"""Profile immutable, locally staged field bundles before the coordinator grants a lease."""

import argparse
import ast
import hashlib
import io
import json
import math
import re
import sqlite3
import struct
import sys
import zipfile
from pathlib import Path

GIB = 2**30
PROFILE_KEY = "ram-v1:"
PIXELS = 140**2


def member(archive, name, limit):
    entry = archive.getinfo(name)
    if entry.file_size > limit:
        raise ValueError(f"Oversized geometry member {name}")
    return archive.read(entry)


def floats(archive, name, width, limit):
    data = member(archive, name + ".npy", limit * width * 8 + 4108)
    stream = io.BytesIO(data)
    if stream.read(6) != b"\x93NUMPY":
        raise ValueError("Expected NPY geometry")
    version = stream.read(2)
    if version not in (b"\x01\x00", b"\x02\x00"):
        raise ValueError("Unsupported NPY format")
    size = int.from_bytes(stream.read(2 if version[0] == 1 else 4), "little")
    if size > 4096:
        raise ValueError("Oversized NPY header")
    header = ast.literal_eval(stream.read(size).decode("ascii").strip())
    shape = header["shape"]
    dtype = header["descr"]
    if (
        not isinstance(shape, tuple)
        or len(shape) != (1 if width == 1 else 2)
        or type(shape[0]) is not int
        or not 1 <= shape[0] <= limit
        or (width > 1 and shape[1] != width)
        or header["fortran_order"] is not False
        or dtype not in ("<f4", "<f8", ">f4", ">f8")
    ):
        raise ValueError("Unexpected geometry array format")
    fmt = struct.Struct(dtype[0] + ("f" if dtype[-1] == "4" else "d") * width)
    payload = stream.read()
    if len(payload) != shape[0] * fmt.size:
        raise ValueError("Geometry length mismatch")
    return shape[0], fmt.iter_unpack(payload)


def sector_sizes(bundle, sector, empirical):
    prefix = f"s{sector:04d}/"
    with zipfile.ZipFile(io.BytesIO(member(bundle, prefix + "cadences.npz", 2**20))) as cadence:
        frames, _ = floats(cadence, "time", 1, 2000)
        native, _ = floats(cadence, "native_time", 1, 25000)
    with zipfile.ZipFile(io.BytesIO(member(bundle, prefix + "stars.npz", 2**20))) as stars:
        count, xy = floats(stars, "xy", 2, 20000)
        mags, values = floats(stars, "tmag", 1, 20000)
        if count != mags:
            raise ValueError("Mismatched catalog columns")
        fitted = templates = 0
        for (x, y), (mag,) in zip(xy, values):
            if math.isfinite(x) and math.isfinite(y) and math.isfinite(mag):
                fitted += mag <= 15 and -6 <= round(x) < 146 and -6 <= round(y) < 146
                templates += empirical and mag < 12 and 10 <= x <= 129 and 10 <= y <= 129
    return frames, native, fitted, templates


def required_bytes(sectors, injections):
    # Full-search envelope. 364 Gaia, 36 tangent, and 831 detector columns.
    # Retained models overlap the transient dense solve for the current sector.
    retained = scratch = 0
    for frames, native, stars, empirical in sectors:
        design = 364 + 36 + 831 + empirical
        columns = design + stars
        retained += PIXELS * (4 * design + 16 * columns + 16 * frames)
        solve = PIXELS * (48 * columns + (24 if injections else 16) * frames) + 32 * columns**2
        strips = 560 * native * 32
        scratch = max(scratch, solve, strips)
    # Initial engineering allowance for libraries, allocator and backend workspaces.
    return math.ceil((GIB + retained + scratch) * 1.5)


def profile(spec, fields):
    digest = spec["bundle_sha256"]
    if not re.fullmatch("[0-9a-f]{64}", digest):
        raise ValueError("Invalid bundle digest")
    path = fields / (digest + ".zip")
    if path.stat().st_size != spec["bundle_bytes"] or path.stat().st_size > 32 * 2**20:
        raise ValueError("Staged bundle length mismatch")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Staged bundle digest mismatch")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        empirical = spec["policy"]["empirical_star_sectors"]
        sizes = [sector_sizes(archive, sector, sector in empirical) for sector in spec["sectors"]]
    return required_bytes(sizes, bool(spec["injections"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("fields", type=Path)
    args = parser.parse_args()
    failed = False
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=rw", uri=True, timeout=10) as db:
        rows = db.execute(
            "SELECT id,spec FROM tasks WHERE state IN ('pending','leased') AND NOT EXISTS "
            "(SELECT 1 FROM settings WHERE key=?||tasks.id)",
            (PROFILE_KEY,),
        ).fetchall()
        for task, raw in rows:
            try:
                amount = profile(json.loads(raw), args.fields)
            except (OSError, KeyError, ValueError, SyntaxError, zipfile.BadZipFile) as error:
                print(f"{task}: {error}", file=sys.stderr)
                failed = True
                continue
            with db:
                db.execute(
                    "INSERT OR IGNORE INTO settings VALUES(?,?)", (PROFILE_KEY + task, amount)
                )
            print(f"{task} {amount / GIB:.2f} GiB")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
