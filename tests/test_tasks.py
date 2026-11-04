import hashlib
import zipfile

import pytest

from p9search.storage import DiskBudget
from p9search.tasks import PROTOCOL, fetch_bundle, task_id, validate_task


def specification(**changes):
    value = {
        "protocol": PROTOCOL,
        "label": "test field",
        "sectors": [1, 28, 68],
        "reference_sector": 68,
        "bundle_url": "https://example.org/field.zip",
        "bundle_sha256": "0" * 64,
        "bundle_bytes": 1000,
        "required_bytes": 2**30,
        "injections": [],
    }
    value.update(changes)
    value["task_id"] = task_id(value)
    return value


def test_task_identity_binds_all_inputs_and_rejects_invalid_scope():
    value = specification()
    assert validate_task(value) is value
    value["reference_sector"] = 28
    with pytest.raises(ValueError, match="identity"):
        validate_task(value)
    for changes in [
        {"sectors": [1, 1, 68]},
        {"bundle_url": "file:///tmp/field.zip"},
        {"bundle_bytes": 100 * 2**20},
        {"protocol": "unknown"},
    ]:
        with pytest.raises(ValueError):
            validate_task(specification(**changes))


def test_bundle_cannot_escape_its_directory_or_smuggle_extra_files(tmp_path):
    archive = tmp_path / "field.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../../escape.txt", "untrusted")
    spec = specification(
        bundle_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        bundle_bytes=archive.stat().st_size,
    )
    with pytest.raises(ValueError, match="members"):
        fetch_bundle(spec, tmp_path, DiskBudget(tmp_path, 2**30, 0))
    assert not (tmp_path.parent / "escape.txt").exists()
    assert not (tmp_path / "data").exists()
