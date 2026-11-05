"""Check nested array headers before allocating or extracting geometry."""

import io
import math
import re
import stat
import zipfile
from pathlib import PurePosixPath

import numpy as np

MAX_EXPANDED = 128 * 2**20
WCS_KEYS = {"CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2", "CD1_1", "CD1_2", "CD2_1", "CD2_2"}
ARRAYS = {
    "cadences.npz": {
        "native_time": ("f", 25000),
        "native_keep": ("b", 25000),
        "exposure": ("f", 25000),
        "time": ("f", 2000),
        "keep": ("b", 2000),
    },
    "geometry.npz": {
        "native_pointing": ("f", 25000, 2),
        "pointing": ("f", 2000, 2),
        "time": ("f", 2000),
        "keep": ("b", 2000),
        "dt": ("f", 2000),
        "reference_time": ("f",),
    },
    "native_wcs.npz": {k: ("f", 25000) for k in WCS_KEYS},
    "stars.npz": {"xy": ("f", 20000, 2), "tmag": ("f", 20000), "flux": ("f", 20000)},
}


def ordinary(record):
    mode = record.external_attr >> 16
    if record.is_dir() or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or record.flag_bits & 1:
        raise ValueError("Only ordinary unencrypted bundle members are allowed")


def array_headers(content, name):
    shapes = {}
    total = 0
    base = PurePosixPath(name).name
    expected = ARRAYS.get(base)
    if re.fullmatch(r"observer_s[0-9]+\.npz", base):
        expected = {"time": ("f", 4000), "position": ("f", 4000, 3)}
    motion = base == "motion_masks.npz"
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if not entries or len(entries) > 128:
            raise ValueError("Invalid nested array count")
        keys = [r.filename.removesuffix(".npy") for r in entries]
        if len(set(keys)) != len(keys) or (expected is not None and set(keys) != set(expected)):
            raise ValueError("Unexpected or duplicate nested array members")
        if motion and keys != [f"slice_{i:02d}" for i in range(len(keys))]:
            raise ValueError("Invalid motion mask members")
        if expected is None and not motion:
            raise ValueError("Unknown array archive")
        if sum(r.file_size for r in entries) > MAX_EXPANDED:
            raise ValueError("Nested expanded arrays exceed limit")
        for key, record in zip(keys, entries, strict=True):
            ordinary(record)
            if record.filename != key + ".npy":
                raise ValueError("Expected NPY members")
            with archive.open(record) as stream:
                version = np.lib.format.read_magic(stream)
                if version == (1, 0):
                    shape, order, dtype = np.lib.format.read_array_header_1_0(
                        stream, max_header_size=4096
                    )
                elif version == (2, 0):
                    shape, order, dtype = np.lib.format.read_array_header_2_0(
                        stream, max_header_size=4096
                    )
                else:
                    raise ValueError("Unsupported NPY version")
                rule = ("b", 4_000_000) if motion else expected[key]
                if (
                    dtype.hasobject
                    or dtype.fields is not None
                    or dtype.kind != rule[0]
                    or dtype.itemsize not in ((1,) if rule[0] == "b" else (4, 8))
                    or len(shape) != len(rule) - 1
                    or order
                    or any(type(n) is not int or n < 1 for n in shape)
                    or (shape and shape[0] > rule[1])
                    or (len(shape) > 1 and shape[1:] != rule[2:])
                ):
                    raise ValueError("Invalid array dtype or shape")
                allocation = math.prod(shape) * dtype.itemsize
                if allocation + stream.tell() != record.file_size:
                    raise ValueError("Array payload differs from declared shape")
                total += allocation
                shapes[key] = shape
        if motion and sum(math.prod(s) for s in shapes.values()) > 4_000_000:
            raise ValueError("Motion bank exceeds allocation limit")
    return total, shapes


def validate_bundle(bundle, sectors):
    expected = {"motion_masks.npz"}
    for sid in sectors:
        expected.add(f"observer_s{sid}.npz")
        expected.update(f"s{sid:04d}/{n}" for n in (*ARRAYS, "metadata.json", "wcs.txt"))
    records = bundle.infolist()
    if len(records) != len(expected) or {r.filename for r in records} != expected:
        raise ValueError("Unexpected or duplicate bundle members")
    total = sum(r.file_size for r in records)
    if total > MAX_EXPANDED:
        raise ValueError("Expanded bundle exceeds limit")
    headers = {}
    for record in records:
        ordinary(record)
        if record.filename.endswith(".npz"):
            size, headers[record.filename] = array_headers(bundle.read(record), record.filename)
            total += size
        elif record.file_size > 32768:
            raise ValueError("Geometry text exceeds limit")
        if total > MAX_EXPANDED:
            raise ValueError("Aggregate nested allocation exceeds limit")
    for sid in sectors:
        stem = f"s{sid:04d}/"
        c, g, w, stars = [headers[stem + n] for n in ARRAYS]
        nt, nb = c["native_time"], c["time"]
        if (
            nt != c["native_keep"]
            or nt != c["exposure"]
            or nb != c["keep"]
            or nb != g["time"]
            or nb != g["dt"]
            or nb != g["keep"]
            or g["pointing"] != (*nb, 2)
            or g["native_pointing"] != (*nt, 2)
            or any(shape != nt for shape in w.values())
            or stars["xy"] != (*stars["tmag"], 2)
            or stars["flux"] != stars["tmag"]
        ):
            raise ValueError("Inconsistent geometry array dimensions")
        observer = headers[f"observer_s{sid}.npz"]
        if observer["position"] != (*observer["time"], 3):
            raise ValueError("Inconsistent observer dimensions")
    return records
