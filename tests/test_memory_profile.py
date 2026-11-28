import hashlib
import importlib.util
import zipfile
from pathlib import Path

import pytest

from .test_bundle_security import bundle_bytes

module = importlib.util.spec_from_file_location(
    "profile_memory", Path(__file__).parents[1] / "coordinator/profile_memory.py"
)
memory = importlib.util.module_from_spec(module)
module.loader.exec_module(memory)


def test_profile_verifies_staged_bundle_and_counts_dimensions(tmp_path):
    data = bundle_bytes().getvalue()
    digest = hashlib.sha256(data).hexdigest()
    bundle = tmp_path / (digest + ".zip")
    bundle.write_bytes(data)
    spec = dict(
        bundle_sha256=digest,
        bundle_bytes=len(data),
        sectors=[1, 28, 68],
        policy={"empirical_star_sectors": []},
        injections=[],
    )
    with zipfile.ZipFile(bundle) as archive:
        sectors = [memory.sector_sizes(archive, i, False) for i in spec["sectors"]]
    assert memory.profile(spec, tmp_path) == memory.required_bytes(sectors, False)
    bundle.write_bytes(b"X" + data[1:])
    with pytest.raises(ValueError, match="digest"):
        memory.profile(spec, tmp_path)


def test_full_search_envelope_flags_observed_four_sector_memory_exit():
    crowded = [
        (1093, 1093, 1302, 0),
        (1217, 10932, 1299, 0),
        (1193, 10701, 1191, 0),
        (1264, 11350, 1310, 0),
    ]
    sparse = [(1243, 3725, 162, 0), (1302, 11694, 157, 0), (1274, 11428, 165, 0)]
    assert memory.required_bytes(crowded, True) > 12 * memory.GIB
    assert memory.required_bytes(sparse, True) < 9 * memory.GIB
    assert memory.required_bytes(crowded[:3], False) < memory.required_bytes(crowded[:3], True)
