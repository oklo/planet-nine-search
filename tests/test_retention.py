import hashlib
from pathlib import Path

import pytest

from p9search import retention
from p9search.storage import DiskBudget, canonical, read_json, sha256, write_json

from .test_tasks import specification


def completed_job(root, number=0, *, state="searched", acknowledged=True, payload=1024):
    spec = specification(label=f"field {number}")
    job = root / spec["task_id"]
    write_json(job / "task.json", spec)
    write_json(job / "launch.json", {"source_sha256": "a" * 64})
    sector = job / "data/s0001"
    (sector / "strips").mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name in ("raw.npy", "error.npy", "strips/012-native.npy"):
        path = sector / name
        path.write_bytes(b"x" * payload)
        manifest[name] = sha256(path)
    write_json(sector / "archive-rows.json", {"row": {"etag": "archive-version"}})
    manifest["archive-rows.json"] = sha256(sector / "archive-rows.json")
    receipt = {"sha256": manifest, "native_cadences": 100, "binned_cadences": 10}
    write_json(sector / "download.json", receipt)
    report = {
        "task_id": spec["task_id"],
        "state": state,
        "spec_sha256": hashlib.sha256(canonical(spec).encode()).hexdigest(),
        "inputs": {"data/s0001/download.json": receipt},
        "artifacts": {name: sha256(job / name) for name in ("task.json", "launch.json")},
    }
    write_json(job / "result.json", report)
    if acknowledged:
        write_json(job / "submission.json", retention.RECEIPT)
    return job


@pytest.mark.parametrize("state", ["searched", "quality_rejected", "needs_followup"])
def test_acknowledged_cache_released_but_reports_and_provenance_unchanged(tmp_path, state):
    job = completed_job(tmp_path, state=state)
    before = {str(p): sha256(p) for p in job.rglob("*.json")}
    retention.maintain(tmp_path)
    assert not list(job.rglob("*.npy"))
    assert {p: sha256(p) for p in before} == before
    record = read_json(job / retention.RECORD)
    assert record["state"] == "complete" and record["bytes_released"] == 3072
    retention.maintain(tmp_path)
    assert read_json(job / retention.RECORD) == record


def test_unacknowledged_and_explicit_keep_data_are_not_cleaned(tmp_path):
    pending = completed_job(tmp_path, 1, acknowledged=False)
    kept = completed_job(tmp_path, 2)
    retention.maintain(tmp_path, keep_data=True)
    assert len(list(tmp_path.rglob("*.npy"))) == 6
    retention.maintain(tmp_path, active_task=kept.name)
    assert list(pending.rglob("*.npy")) and list(kept.rglob("*.npy"))


def test_changed_evidence_and_path_escape_never_delete_pixels(tmp_path):
    job = completed_job(tmp_path)
    (job / "launch.json").write_text("changed")
    with pytest.raises(ValueError, match="evidence"):
        retention.maintain(tmp_path)
    assert len(list(job.rglob("*.npy"))) == 3
    job = completed_job(tmp_path)
    report = read_json(job / "result.json")
    report["inputs"]["data/s0001/download.json"]["sha256"]["../../../private.npy"] = "a" * 64
    write_json(job / "result.json", report)
    with pytest.raises(ValueError, match="member"):
        retention.maintain(tmp_path)
    assert len(list(job.rglob("*.npy"))) == 3


def test_symlink_cache_or_parent_is_never_followed(tmp_path):
    job = completed_job(tmp_path)
    external = tmp_path / "important.npy"
    external.write_bytes(b"keep")
    raw = job / "data/s0001/raw.npy"
    raw.unlink()
    raw.symlink_to(external)
    with pytest.raises(ValueError, match="ordinary"):
        retention.maintain(tmp_path)
    assert external.read_bytes() == b"keep"


def test_release_resumes_after_interrupted_unlink(tmp_path, monkeypatch):
    job = completed_job(tmp_path)
    unlink = retention.os.unlink
    count = 0

    def interrupt(*args, **kwargs):
        nonlocal count
        unlink(*args, **kwargs)
        count += 1
        if count == 1:
            raise OSError("interrupted")

    with monkeypatch.context() as context:
        context.setattr(retention.os, "unlink", interrupt)
        with pytest.raises(OSError, match="interrupted"):
            retention.maintain(tmp_path)
    assert read_json(job / retention.RECORD)["state"] == "releasing"
    retention.maintain(tmp_path)
    assert not list(job.rglob("*.npy"))
    assert read_json(job / retention.RECORD)["state"] == "complete"


def test_replaced_cache_is_preserved_on_resume(tmp_path, monkeypatch):
    job = completed_job(tmp_path)
    with monkeypatch.context() as context:
        context.setattr(
            retention.os, "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))
        )
        with pytest.raises(OSError):
            retention.maintain(tmp_path)
    raw = job / "data/s0001/raw.npy"
    raw.write_bytes(b"user replaced these pixels")
    with pytest.raises(ValueError, match="changed during"):
        retention.maintain(tmp_path)
    assert raw.read_bytes() == b"user replaced these pixels"


def test_repeated_cycles_remain_bounded_and_preserve_user_files(tmp_path):
    protected = completed_job(tmp_path, -1)
    note = protected / "my-notes.txt"
    note.write_text("keep my analysis")
    budget = DiskBudget(tmp_path, 500000, 0)
    for number in range(40):
        job = completed_job(tmp_path, number, payload=50000)
        budget.check()
        retention.maintain(tmp_path, keep_results=3)
        budget.check()
        assert budget.used() < 60000
        assert job.exists() and not list(job.rglob("*.npy"))
    assert note.read_text() == "keep my analysis"
    assert len(list(tmp_path.glob("*/submission.json"))) == 4


def test_compact_history_also_has_a_byte_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(retention, "HISTORY_BYTES", 1)
    old = completed_job(tmp_path, 1)
    newest = completed_job(tmp_path, 2)
    retention.maintain(tmp_path)
    assert not old.exists() and newest.exists()


def test_pruning_resumes_after_partial_directory_removal(tmp_path, monkeypatch):
    old = completed_job(tmp_path, 1)
    newest = completed_job(tmp_path, 2)

    def interrupt(path):
        (Path(path) / "task.json").unlink()
        raise OSError("interrupted pruning")

    interrupt.avoids_symlink_attacks = True
    with monkeypatch.context() as context:
        context.setattr(retention.shutil, "rmtree", interrupt)
        with pytest.raises(OSError, match="pruning"):
            retention.maintain(tmp_path, keep_results=1)
    assert (tmp_path / retention.JOURNAL).exists()
    retention.maintain(tmp_path, keep_results=1)
    assert not (tmp_path / retention.JOURNAL).exists()
    assert not old.exists() and newest.exists()
    assert not list(tmp_path.glob(".discard-*"))


def test_cleanup_never_rereads_bulk_pixels(tmp_path, monkeypatch):
    job = completed_job(tmp_path)
    original = retention.sha256

    def small_files_only(path):
        assert Path(path).suffix != ".npy", "cleanup must not rehash bulk pixel caches"
        return original(path)

    monkeypatch.setattr(retention, "sha256", small_files_only)
    retention.maintain(tmp_path)
    assert not list(job.rglob("*.npy"))


def test_master_can_release_completed_pixels_after_external_acceptance(tmp_path):
    job = completed_job(tmp_path, acknowledged=False)
    spec = read_json(job / "task.json")
    before = (job / "result.json").read_bytes()
    retention.release_pixels(job, spec)
    assert not list(job.rglob("*.npy"))
    assert (job / "result.json").read_bytes() == before
    assert not (job / "submission.json").exists()


def test_missing_old_cache_can_be_compacted_without_fabricating_released_bytes(tmp_path):
    job = completed_job(tmp_path)
    (job / "data/s0001/raw.npy").unlink()
    retention.maintain(tmp_path)
    assert read_json(job / retention.RECORD)["bytes_released"] == 2048


def test_malformed_prune_record_does_not_move_a_result(tmp_path):
    job = completed_job(tmp_path)
    write_json(tmp_path / retention.JOURNAL, {"task_id": job.name, "directory": [], "files": {}})
    with pytest.raises(ValueError, match="prune record"):
        retention.maintain(tmp_path)
    assert job.exists() and list(job.rglob("*.npy"))
