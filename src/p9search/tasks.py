"""Portable, content-addressed field specifications and bounded metadata bundles."""

import hashlib
import re
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from .bundles import MAX_EXPANDED, validate_bundle
from .network import DEFAULT_ORIGINS, download, public_url
from .policy import VERSION, validate_policy
from .storage import canonical, sha256

PROTOCOL = VERSION
MAX_BUNDLE = 64 * 2**20


def validate_url(url):
    parsed = urlparse(url)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        (parsed.scheme != "https" and not (local and parsed.scheme == "http"))
        or not parsed.hostname
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
        "policy",
    }
    if not isinstance(spec, dict) or set(spec) != required or spec["protocol"] != PROTOCOL:
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
    validate_policy(spec["policy"], sectors)
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
            not isinstance(item, dict)
            or set(item) != {"parameters", "radial_velocity", "flux", "label"}
            or not isinstance(item["parameters"], list)
            or len(item["parameters"]) != 5
            or not isinstance(item["label"], str)
            or len(item["label"]) > 100
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


def fetch_bundle(spec, directory, budget, origins=DEFAULT_ORIGINS):
    validate_task(spec)
    public_url(spec["bundle_url"], origins)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "field.zip"
    if not archive.exists() or sha256(archive) != spec["bundle_sha256"]:
        budget.check(spec["bundle_bytes"] + MAX_EXPANDED)
        temporary = archive.with_suffix(".part")
        try:
            download(spec["bundle_url"], temporary, spec["bundle_bytes"], origins)
            if sha256(temporary) != spec["bundle_sha256"]:
                raise ValueError("Bundle checksum differs")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    data = directory / "data"
    with zipfile.ZipFile(archive) as bundle:
        records = validate_bundle(bundle, spec["sectors"])
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
