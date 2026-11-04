"""Portable, content-addressed field specifications and bounded metadata bundles."""

import hashlib
import re
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import requests

from .storage import canonical, sha256

PROTOCOL = "tess-gaia-hough-v1"
MAX_BUNDLE = 64 * 2**20
MAX_EXPANDED = 128 * 2**20


def validate_url(url):
    parsed = urlparse(url)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        (parsed.scheme != "https" and not (local and parsed.scheme == "http"))
        or parsed.username
        or parsed.password
    ):
        raise ValueError("An HTTPS endpoint is required (HTTP is allowed on localhost)")
    return url


def task_id(spec):
    value = {key: val for key, val in spec.items() if key != "task_id"}
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_task(spec):
    required = {
        "protocol",
        "task_id",
        "label",
        "sectors",
        "reference_sector",
        "bundle_url",
        "bundle_sha256",
        "bundle_bytes",
        "required_bytes",
        "injections",
    }
    if set(spec) != required or spec["protocol"] != PROTOCOL:
        raise ValueError("Unsupported task schema or protocol")
    if not isinstance(spec["label"], str) or len(spec["label"]) > 100:
        raise ValueError("Invalid field label")
    sectors = spec["sectors"]
    if (
        not isinstance(sectors, list)
        or not 3 <= len(sectors) <= 4
        or len(set(sectors)) != len(sectors)
    ):
        raise ValueError("Three or four distinct sectors required")
    if (
        any(type(s) is not int or not 1 <= s <= 9999 for s in sectors)
        or spec["reference_sector"] not in sectors
    ):
        raise ValueError("Invalid sector identifiers")
    if not re.fullmatch(r"[a-f0-9]{64}", spec["bundle_sha256"]) or spec["task_id"] != task_id(spec):
        raise ValueError("Task identity differs from its contents")
    validate_url(spec["bundle_url"])
    if type(spec["bundle_bytes"]) is not int or not 1 <= spec["bundle_bytes"] <= MAX_BUNDLE:
        raise ValueError("Invalid bundle size")
    if (
        type(spec["required_bytes"]) is not int
        or not MAX_EXPANDED <= spec["required_bytes"] <= 32 * 2**30
    ):
        raise ValueError("Invalid task disk estimate")
    if not isinstance(spec["injections"], list) or len(spec["injections"]) > 8:
        raise ValueError("At most eight predeclared injections per task")
    import math

    for item in spec["injections"]:
        if (
            set(item) != {"parameters", "radial_velocity", "flux", "label"}
            or len(item["parameters"]) != 5
        ):
            raise ValueError("Invalid injection schema")
        if not all(
            type(v) in (int, float) and math.isfinite(v)
            for v in [*item["parameters"], item["radial_velocity"], item["flux"]]
        ):
            raise ValueError("Nonfinite injection")
        if not (0 < item["flux"] <= 1000 and 1 / 1500 <= item["parameters"][4] <= 1 / 200):
            raise ValueError("Injection outside search scope")
    return spec


def fetch_bundle(spec, directory, budget):
    validate_task(spec)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "field.zip"
    if not archive.exists() or sha256(archive) != spec["bundle_sha256"]:
        budget.check(spec["bundle_bytes"] + MAX_EXPANDED)
        temporary = archive.with_suffix(".part")
        with requests.get(spec["bundle_url"], stream=True, timeout=(15, 120)) as response:
            response.raise_for_status()
            validate_url(response.url)
            count = 0
            with temporary.open("wb") as output:
                for block in response.iter_content(1024 * 1024):
                    count += len(block)
                    if count > spec["bundle_bytes"]:
                        raise ValueError("Bundle exceeds declared size")
                    output.write(block)
        if count != spec["bundle_bytes"] or sha256(temporary) != spec["bundle_sha256"]:
            raise ValueError("Bundle checksum or size differs")
        temporary.replace(archive)
    expected = {"motion_masks.npz"}
    for sid in spec["sectors"]:
        expected.add(f"observer_s{sid}.npz")
        expected.update(
            f"s{sid:04d}/{name}"
            for name in [
                "metadata.json",
                "cadences.npz",
                "geometry.npz",
                "native_wcs.npz",
                "wcs.txt",
                "stars.npz",
            ]
        )
    data = directory / "data"
    with zipfile.ZipFile(archive) as bundle:
        records = bundle.infolist()
        if len(records) != len(expected) or {r.filename for r in records} != expected:
            raise ValueError("Unexpected or duplicate bundle members")
        if sum(r.file_size for r in records) > MAX_EXPANDED:
            raise ValueError("Expanded bundle exceeds limit")
        budget.check(sum(r.file_size for r in records))
        for record in records:
            path = data.joinpath(*PurePosixPath(record.filename).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            content = bundle.read(record)
            if path.exists():
                if path.read_bytes() != content:
                    raise ValueError("Existing geometry differs from the immutable bundle")
            else:
                path.write_bytes(content)
    return data
