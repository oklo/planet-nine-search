"""Atomic small records and explicit bounds on a worker's own files."""

import hashlib
import json
import math
import os
import shutil
from pathlib import Path

GIB = 2**30


class DiskCapacityError(RuntimeError):
    """The work allocation or volume reserve currently leaves insufficient space."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        if private:
            os.fchmod(stream.fileno(), 0o600)
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 2**20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DiskBudget:
    def __init__(self, directory, maximum_bytes, reserve_bytes):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        if maximum_bytes <= 0 or reserve_bytes < 0:
            raise ValueError("Invalid disk budget")
        self.maximum = maximum_bytes
        self.reserve = reserve_bytes

    def used(self):
        total = 0
        for root, dirs, files in os.walk(self.directory, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
            for name in files:
                path = Path(root) / name
                if path.is_symlink():
                    raise ValueError("Worker directories must not contain symbolic links")
                try:
                    total += path.stat().st_size
                except FileNotFoundError:  # A child may just have renamed its checkpoint.
                    continue
        return total

    def available(self):
        return max(
            0,
            min(self.maximum - self.used(), shutil.disk_usage(self.directory).free - self.reserve),
        )

    def check(self, additional=0):
        if additional < 0 or self.used() + additional > self.maximum:
            raise DiskCapacityError("Worker disk allocation exhausted")
        if shutil.disk_usage(self.directory).free - additional < self.reserve:
            raise DiskCapacityError("Free-space reserve reached")


class DirectoryLock:
    """Prevent two local processes from writing into the same workspace."""

    def __init__(self, directory, name):
        self.path = Path(directory) / name
        self.stream = None

    def __enter__(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a")
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise RuntimeError(f"Another process is using {self.path.parent}") from None
        return self

    def __exit__(self, *_):
        self.stream.close()


MAX_REPORT = 2_000_000


def bounded_json(raw):
    if len(raw) > MAX_REPORT:
        raise ValueError("Report exceeds limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON member")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("Nonfinite JSON number")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except RecursionError as error:
        raise ValueError("Report nesting exceeds limit") from error

    def check(item, depth=0):
        if depth > 24:
            raise ValueError("Report nesting exceeds limit")
        if isinstance(item, dict):
            for member in item.values():
                check(member, depth + 1)
        elif isinstance(item, list):
            for member in item:
                check(member, depth + 1)
        elif isinstance(item, float) and not math.isfinite(item):
            invalid(item)

    check(value)
    return value


def load_bounded(path):
    with Path(path).open("rb") as stream:
        return bounded_json(stream.read(MAX_REPORT + 1))
