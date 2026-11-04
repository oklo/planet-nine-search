import json
import stat

import pytest

from p9search.storage import DirectoryLock, DiskBudget, read_json, write_json


def test_disk_guard_counts_existing_bytes_and_rejects_symlinks(tmp_path):
    budget = DiskBudget(tmp_path, 100, 0)
    (tmp_path / "pixels").write_bytes(b"x" * 80)
    budget.check(20)
    with pytest.raises(RuntimeError, match="allocation"):
        budget.check(21)
    (tmp_path / "alias").symlink_to(tmp_path / "pixels")
    with pytest.raises(ValueError, match="symbolic"):
        budget.check()


def test_private_credentials_and_failed_json_preserve_previous_record(tmp_path):
    path = tmp_path / "credentials.json"
    write_json(path, {"token": "example"}, private=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        write_json(path, {"bad": float("nan")}, private=True)
    assert read_json(path) == {"token": "example"}
    json.loads(path.read_text())


def test_exclusive_directory_lock_is_released_even_after_error(tmp_path):
    with DirectoryLock(tmp_path, "test.lock"):
        with pytest.raises(RuntimeError, match="Another process"):
            with DirectoryLock(tmp_path, "test.lock"):
                pass
    with DirectoryLock(tmp_path, "test.lock"):
        pass
