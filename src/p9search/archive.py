"""Resume checked TESS row downloads without assembling a huge native cube."""

import gc
import hashlib
import time
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

from .science.binning import bin_detector_cadences
from .storage import read_json, sha256, write_json


def archive_class(record_row=None):
    """Avoid tesscube 1.2's unused import-time spacecraft-kernel download.

    Fixed rows use the archive's BARYCORR. The dependency's class is restored
    immediately; any later spacecraft request still constructs the real class.
    """
    import lkspacecraft

    spacecraft = lkspacecraft.TESSSpacecraft

    class LazySpacecraft:
        def __init__(self):
            self.instance = None

        def __getattr__(self, name):
            if self.instance is None:
                self.instance = spacecraft()
            return getattr(self.instance, name)

    try:
        lkspacecraft.TESSSpacecraft = LazySpacecraft
        from tesscube import TESSCube
    finally:
        lkspacecraft.TESSSpacecraft = spacecraft

    class RowCube(TESSCube):
        async def async_get_data_per_row(self, s3, semaphore, start_column, ncolumns, row=1):
            from tesscube import BUCKET_NAME

            expected = ncolumns * self.nframes * self.nsets * 4
            if self.nsets != 2 or not 0 < expected <= 32 * 2**20:
                raise ValueError("Archive row exceeds the supported layout or byte limit")
            async with semaphore:
                first, last = self.find_byte_range(
                    row=row, column_range=(start_column, start_column + ncolumns)
                )
                if last - first != expected:
                    raise ValueError("Archive byte range differs")
                response = await s3.get_object(
                    Bucket=BUCKET_NAME, Key=self.object_key, Range=f"bytes={first}-{last - 1}"
                )
                if response.get("ContentLength") != expected:
                    raise ValueError("Archive response byte count differs")
                payload = await response["Body"].read()
                values = decode_row(payload, ncolumns, self.nframes, self.nsets)
                if record_row is not None:
                    record_row(
                        {
                            "row": row,
                            "etag": response.get("ETag"),
                            "wire_sha256": hashlib.sha256(payload).hexdigest(),
                            "bytes": len(payload),
                        }
                    )
                return values

    return RowCube


def decode_row(payload, columns, frames, sets):
    """Decode big-endian float32 directly, preserving tesscube's float64 API."""
    if any(type(n) is not int or n <= 0 for n in (columns, frames, sets)):
        raise ValueError("Positive integer row dimensions required")
    if type(payload) is not bytes or len(payload) != columns * frames * sets * 4:
        raise ValueError("Archive row length differs")
    with np.errstate(invalid="ignore"):
        return np.frombuffer(payload, dtype=">f4").astype(np.float64).reshape(columns, frames, sets)


def verify_archive(cube, directory):
    metadata = read_json(directory / "metadata.json")
    with np.load(directory / "cadences.npz") as archive:
        cadences = dict(archive)
    if cube.object_key != metadata["archive_object_key"]:
        raise ValueError("Archive identity changed")
    native = np.asarray(cube.time, float) - np.asarray(cube.last_hdu.data["BARYCORR"], float)
    finite = np.isfinite(native)
    np.testing.assert_allclose(native[finite], cadences["native_time"], rtol=0, atol=1e-10)
    np.testing.assert_array_equal(
        (np.asarray(cube.quality, int)[finite] & 21183) == 0, cadences["native_keep"]
    )
    np.testing.assert_allclose(
        (np.asarray(cube.tstop) - np.asarray(cube.tstart))[finite],
        cadences["exposure"],
        rtol=0,
        atol=1e-12,
    )
    with np.load(directory / "native_wcs.npz") as series:
        for key in series.files:
            np.testing.assert_array_equal(np.asarray(cube.last_hdu.data[key])[finite], series[key])
    origin = np.array([metadata["origin_column"], metadata["origin_row"]])
    probe = origin + np.array([[7, 7], [69.5, 69.5], [132, 132]])
    static = WCS(fits.Header.fromtextfile(directory / "wcs.txt"))
    np.testing.assert_allclose(
        cube.wcs.all_pix2world(probe, 1), static.all_pix2world(probe, 1), rtol=0, atol=1e-10
    )
    return cadences, finite


def acquire(directory, budget, progress):
    directory = Path(directory)
    ready = directory / "download.json"
    if ready.exists():
        manifest = read_json(ready)
        for relative, digest in manifest["sha256"].items():
            if sha256(directory / relative) != digest:
                raise ValueError("Completed download checksum differs")
        return
    meta = read_json(directory / "metadata.json")
    provenance_path = directory / "archive-rows.json"
    provenance = read_json(provenance_path) if provenance_path.exists() else {}

    def record_row(record):
        if not record["etag"]:
            raise ValueError("Archive response has no object version")
        if provenance and record["etag"] != next(iter(provenance.values()))["etag"]:
            raise ValueError("Archive object changed during acquisition")
        key = str(record["row"])
        if key in provenance and provenance[key] != record:
            raise ValueError("Repeated archive row changed")
        provenance[key] = record
        write_json(provenance_path, provenance)

    cube = archive_class(record_row)(sector=meta["sector"], camera=meta["camera"], ccd=meta["ccd"])
    from tesscube.utils import _sync_call

    cadences, finite = verify_archive(cube, directory)
    nt, nb = len(cadences["native_time"]), len(cadences["time"])
    if nt > 25000 or nb > 2000 or meta["size"] != 140:
        raise ValueError("Archive exceeds bounded field geometry")
    strips = directory / "strips"
    strips.mkdir(exist_ok=True)
    checkpoint = directory / "download-progress.json"
    record = read_json(checkpoint) if checkpoint.exists() else {"strips": {}}
    zeros = np.zeros(nt)
    for start in range(0, 140, 4):
        paths = [strips / f"{start:03d}-{name}.npy" for name in ("native", "raw", "error")]
        if str(start) in record["strips"]:
            for path, digest in zip(paths, record["strips"][str(start)], strict=True):
                if sha256(path) != digest:
                    raise ValueError("Resume strip checksum differs")
            continue
        budget.check((nt + 2 * nb) * 4 * 140 * 4 + 4096)
        progress("download", sector=meta["sector"], rows=start, total=140)
        requests = [
            dict(row=meta["origin_row"] + row, start_column=meta["origin_column"], ncolumns=140)
            for row in range(start, start + 4)
        ]
        for attempt in range(3):
            try:
                fetched = _sync_call(cube.async_get_data_per_rows, requests)
                break
            except ValueError:
                raise
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        decoded = np.vstack(fetched).transpose(2, 1, 0)
        if decoded.shape != (2, len(finite), 560):
            raise ValueError("Archive row layout differs")
        raw, error = [np.asarray(x[finite].reshape(nt, 4, 140), dtype=np.float32) for x in decoded]
        for old, new in zip(decoded, [raw, error], strict=True):
            np.testing.assert_array_equal(old[finite].reshape(new.shape), new)
        reduced = bin_detector_cadences(
            raw,
            error,
            cadences["native_time"],
            cadences["native_keep"],
            zeros,
            zeros,
            cadences["exposure"],
        )
        np.testing.assert_allclose(reduced[2], cadences["time"], rtol=0, atol=1e-10)
        np.testing.assert_array_equal(reduced[3], cadences["keep"])
        for path, array in zip(paths, [raw, *reduced[:2]], strict=True):
            temporary = path.with_suffix(".partial.npy")
            np.save(temporary, array)
            temporary.replace(path)
        record["strips"][str(start)] = [sha256(p) for p in paths]
        write_json(checkpoint, record)
        del raw, error, fetched, decoded, reduced
        gc.collect()
    budget.check(2 * nb * 140 * 140 * 4 + 4096)
    hashes = {"archive-rows.json": sha256(provenance_path)}
    for name in ("raw", "error"):
        target = directory / f"{name}.npy"
        temporary = target.with_suffix(".partial.npy")
        output = np.lib.format.open_memmap(
            temporary, mode="w+", dtype=np.float32, shape=(nb, 140, 140)
        )
        for start in range(0, 140, 4):
            output[:, start : start + 4] = np.load(strips / f"{start:03d}-{name}.npy")
        output.flush()
        del output
        temporary.replace(target)
        hashes[target.name] = sha256(target)
    for start, digests in record["strips"].items():
        hashes[f"strips/{int(start):03d}-native.npy"] = digests[0]
    write_json(ready, {"sha256": hashes, "native_cadences": nt, "binned_cadences": nb})
    # These derived strips are redundant after the final checked publication.
    for start in range(0, 140, 4):
        for name in ("raw", "error"):
            (strips / f"{start:03d}-{name}.npy").unlink()
