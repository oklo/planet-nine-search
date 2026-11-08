"""Release completed pixel caches and bound acknowledged local history."""

import gzip
import hashlib
import os
import re
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path

from .bundles import ARRAYS
from .storage import DirectoryLock, canonical, load_bounded, sha256, write_json
from .tasks import validate_task

RECEIPT = {"accepted": True, "verified": False}
RECORD = "retention.json"
JOURNAL = ".p9-prune.json"
HISTORY_BYTES = 256 * 2**20


def identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def parent(directory, relative):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in (".", "..") for p in parts):
        raise ValueError("Unsafe cache pathname")
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def files(directory):
    result = {}
    for base, dirs, names in os.walk(directory, followlinks=False):
        for name in [*dirs, *names]:
            path = Path(base) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                raise ValueError("Managed results must contain only ordinary files and directories")
            if stat.S_ISREG(info.st_mode):
                result[str(path.relative_to(directory))] = info
    return result


def completed(directory, spec=None):
    if directory.is_symlink() or directory.resolve() != directory:
        raise ValueError("Result directory is redirected")
    spec = validate_task(load_bounded(directory / "task.json") if spec is None else spec)
    report = load_bounded(directory / "result.json")
    if (
        directory.name != spec["task_id"]
        or report["task_id"] != spec["task_id"]
        or report["spec_sha256"] != hashlib.sha256(canonical(spec).encode()).hexdigest()
        or report["state"] not in ("searched", "quality_rejected", "needs_followup")
    ):
        raise ValueError("No matching completed result")
    for name, expected in report["artifacts"].items():
        if (
            Path(name).name != name
            or not name.endswith(".json")
            or sha256(directory / name) != expected
        ):
            raise ValueError("Completed result evidence differs")
    return spec, report


def pixel_paths(spec, report):
    paths = []
    for name, receipt in report["inputs"].items():
        match = re.fullmatch(r"data/s([0-9]{4})/download.json", name)
        if not match or int(match[1]) not in spec["sectors"]:
            raise ValueError("Unexpected input manifest")
        for member in receipt["sha256"]:
            if member == "archive-rows.json":
                continue
            strip = re.fullmatch(r"strips/([0-9]{3})-native.npy", member)
            if member not in ("raw.npy", "error.npy") and not (
                strip and 0 <= int(strip[1]) < 140 and int(strip[1]) % 4 == 0
            ):
                raise ValueError("Unexpected pixel-cache member")
            paths.append(str(Path(name).parent / member))
    if len(paths) != len(set(paths)):
        raise ValueError("Duplicate pixel-cache member")
    return paths


def release_pixels(directory, spec=None):
    """Discard a completed download cache; the caller controls its release policy."""
    directory = Path(directory).absolute()
    if directory.is_symlink() or directory.resolve() != directory:
        raise ValueError("Result directory is redirected")
    inventory = files(directory)
    spec, report = completed(directory, spec)
    binding = {
        "spec_sha256": report["spec_sha256"],
        "result_sha256": sha256(directory / "result.json"),
    }
    record_path = directory / RECORD
    expected = pixel_paths(spec, report)
    if record_path.exists():
        record = load_bounded(record_path)
        if record["binding"] != binding or set(record["pixels"]) != set(expected):
            raise ValueError("Pixel-release record differs from the submitted result")
    else:
        for name, receipt in report["inputs"].items():
            if load_bounded(directory / name) != receipt:
                raise ValueError("Input receipt changed")
        record = {
            "binding": binding,
            "pixels": {p: identity(inventory[p]) if p in inventory else None for p in expected},
            "state": "releasing",
            "completed_ns": inventory["result.json"].st_mtime_ns,
        }
        write_json(record_path, record)
        sync_directory(directory)
    changed = set()
    for path, original in record["pixels"].items():
        with parent(directory, path) as (descriptor, name):
            try:
                observed = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue  # A committed release can resume after any unlink.
            if not stat.S_ISREG(observed.st_mode) or identity(observed) != original:
                raise ValueError("Pixel cache changed during release")
            os.unlink(name, dir_fd=descriptor)
            changed.add((directory / path).parent)
    for path in changed:
        sync_directory(path)
    if record["state"] != "complete":
        record.update(
            state="complete",
            bytes_released=sum(v[2] for v in record["pixels"].values() if v is not None),
        )
        write_json(record_path, record)
    return spec, report, record


def compact(directory):
    """The coordinator has the report; its trusted replay reacquires the images."""
    directory = Path(directory)
    receipt = load_bounded(directory / "submission.json")
    if receipt.get("accepted") is not True or receipt.get("verified") is not False:
        raise ValueError("No matching submission acknowledgement")
    from .evidence import acknowledged

    acknowledged(directory)
    return release_pixels(directory)


def generated_names(spec, report):
    names = {
        "task.json",
        "result.json",
        "submission.json",
        "worker.log",
        "progress.json",
        "field.zip",
        RECORD,
        *report["artifacts"],
    }
    names.add("data/motion_masks.npz")
    for sector in spec["sectors"]:
        names.add(f"data/observer_s{sector}.npz")
        names.update(
            f"data/s{sector:04d}/{name}"
            for name in (
                *ARRAYS,
                "metadata.json",
                "wcs.txt",
                "archive-rows.json",
                "download.json",
                "download-progress.json",
            )
        )
    return names


def finish_prune(root):
    journal = root / JOURNAL
    if journal.is_symlink():
        raise ValueError("History-prune record is redirected")
    if not journal.exists():
        return
    record = load_bounded(journal)
    if (
        not isinstance(record, dict)
        or set(record) != {"task_id", "directory", "files"}
        or not isinstance(record["directory"], list)
        or len(record["directory"]) != 2
        or any(type(value) is not int or value < 0 for value in record["directory"])
        or not isinstance(record["files"], dict)
        or any(
            not isinstance(digest, str)
            or Path(name).is_absolute()
            or ".." in Path(name).parts
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            for name, digest in record["files"].items()
        )
    ):
        raise ValueError("Invalid history-prune record")
    task = record["task_id"]
    if not isinstance(task, str) or not re.fullmatch(r"[a-f0-9]{64}", task):
        raise ValueError("Invalid history-prune record")
    original, discarded = root / task, root / (".discard-" + task)
    if original.is_symlink() or discarded.is_symlink():
        raise ValueError("History directory is redirected")
    if original.exists():
        if discarded.exists() or list(identity(original.lstat())[:2]) != record["directory"]:
            raise ValueError("Result directory changed before pruning")
        original.rename(discarded)
        sync_directory(root)
    if discarded.exists():
        if discarded.is_symlink() or list(identity(discarded.lstat())[:2]) != record["directory"]:
            raise ValueError("Discard directory changed")
        remaining = files(discarded)
        if not remaining.keys() <= record["files"].keys() or any(
            sha256(discarded / name) != record["files"][name] for name in remaining
        ):
            raise ValueError("Compact history changed before pruning")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("Safe directory removal is unavailable on this platform")
        shutil.rmtree(discarded)
        sync_directory(root)
    journal.unlink()
    sync_directory(root)


def retain_receipt(root, directory, spec, report):
    """Commit a small local receipt before removing centrally acknowledged evidence."""
    record = {
        "task": spec,
        "result": report,
        "submission": load_bounded(directory / "submission.json"),
    }
    raw = canonical(record).encode()
    history = root / "receipts"
    history.mkdir(exist_ok=True)
    if history.is_symlink():
        raise ValueError("Receipt history is redirected")
    destination = history / f"{spec['task_id']}-{hashlib.sha256(raw).hexdigest()}.json.gz"
    if destination.exists():
        if destination.is_symlink() or gzip.decompress(destination.read_bytes()) != raw:
            raise ValueError("Existing receipt differs")
    else:
        temporary = destination.with_suffix(".tmp")
        if temporary.exists():
            if temporary.is_symlink() or gzip.decompress(temporary.read_bytes()) != raw:
                raise ValueError("Interrupted receipt differs")
        else:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(gzip.compress(raw, compresslevel=6, mtime=0))
                stream.flush()
                os.fsync(stream.fileno())
        if gzip.decompress(temporary.read_bytes()) != raw:
            raise ValueError("Receipt verification failed")
        temporary.replace(destination)
        sync_directory(history)


def maintain(root, *, keep_results=32, keep_data=False, keep_evidence=False, active_task=None):
    """Only acknowledged, generated results are eligible for automatic cleanup."""
    if keep_data:
        return
    if type(keep_results) is not int or keep_results < 1:
        raise ValueError("Keep at least one compact result")
    root = Path(root).resolve()
    if not (root / JOURNAL).exists() and not any(root.glob("*/submission.json")):
        return
    with DirectoryLock(root, "retention.lock"), DirectoryLock(root, "search.lock"):
        finish_prune(root)
        history = []
        for directory in sorted(root.iterdir()):
            if (
                not re.fullmatch(r"[a-f0-9]{64}", directory.name)
                or directory.name == active_task
                or not (directory / "submission.json").exists()
            ):
                continue
            spec, report, record = compact(directory)
            inventory = files(directory)
            if not inventory.keys() <= generated_names(spec, report):
                continue  # Preserve files the user added to a result directory.
            if not keep_evidence and "evidence_sha256" in load_bounded(
                directory / "submission.json"
            ):
                retain_receipt(root, directory, spec, report)
                write_json(
                    root / JOURNAL,
                    {
                        "task_id": directory.name,
                        "directory": identity(directory.lstat())[:2],
                        "files": {name: sha256(directory / name) for name in inventory},
                    },
                )
                sync_directory(root)
                finish_prune(root)
                continue
            history.append((record["completed_ns"], directory, inventory))
        history.sort(key=lambda item: (item[0], item[1].name))
        total = sum(info.st_size for _, _, inventory in history for info in inventory.values())
        while len(history) > keep_results or (len(history) > 1 and total > HISTORY_BYTES):
            _, directory, inventory = history.pop(0)
            write_json(
                root / JOURNAL,
                {
                    "task_id": directory.name,
                    "directory": identity(directory.lstat())[:2],
                    "files": {name: sha256(directory / name) for name in inventory},
                },
            )
            sync_directory(root)
            finish_prune(root)
            total -= sum(info.st_size for info in inventory.values())
        receipts = root / "receipts"
        if receipts.exists():
            if receipts.is_symlink():
                raise ValueError("Receipt history is redirected")
            old = sorted(receipts.glob("*.json.gz"), key=lambda p: (p.stat().st_mtime_ns, p.name))
            total = sum(p.stat().st_size for p in old)
            while len(old) > keep_results or (len(old) > 1 and total > HISTORY_BYTES):
                path = old.pop(0)
                if path.is_symlink() or not re.fullmatch(
                    r"[a-f0-9]{64}-[a-f0-9]{64}\.json\.gz", path.name
                ):
                    raise ValueError("Unexpected receipt history member")
                total -= path.stat().st_size
                path.unlink()
            sync_directory(receipts)
