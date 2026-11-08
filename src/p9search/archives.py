"""Immutable evidence archives with shared, losslessly compressed contents."""

import gzip
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import zipfile
from pathlib import Path

from .storage import canonical

MAX_FILE = 128 * 2**20
MAX_ARCHIVE = 512 * 2**20


def digest(data):
    return hashlib.sha256(data).hexdigest()


def ordinary(path):
    path = Path(path).absolute()
    if path.resolve() != path or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"Not an ordinary evidence file: {path}")
    return path


def identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


class EvidenceStore:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.resolve() != self.path:
            raise ValueError("Evidence store is redirected")
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS objects (
                digest TEXT PRIMARY KEY, size INTEGER NOT NULL, payload BLOB,
                compressed INTEGER, container TEXT, member TEXT);
            CREATE TABLE IF NOT EXISTS archives (
                name TEXT PRIMARY KEY, digest TEXT NOT NULL, manifest TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def put(self, data):
        key = digest(data)
        if len(data) > MAX_FILE:
            raise ValueError("Evidence object exceeds limit")
        packed = gzip.compress(data, compresslevel=6, mtime=0)
        compressed = len(packed) < len(data)
        self.db.execute(
            "INSERT OR IGNORE INTO objects VALUES(?,?,?,?,NULL,NULL)",
            (key, len(data), packed if compressed else data, int(compressed)),
        )
        return key

    def bundle(self, data):
        key = self.put(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if (
                len({i.filename for i in entries}) != len(entries)
                or sum(i.file_size for i in entries) > MAX_FILE
            ):
                raise ValueError("Invalid evidence bundle size or members")
            for info in entries:
                if info.is_dir() or info.file_size > MAX_FILE:
                    raise ValueError("Invalid evidence bundle member")
                content = archive.read(info)
                self.db.execute(
                    "INSERT OR IGNORE INTO objects VALUES(?,?,NULL,NULL,?,?)",
                    (digest(content), len(content), key, info.filename),
                )

    def get(self, key, *, container=False):
        row = self.db.execute(
            "SELECT size,payload,compressed,container,member FROM objects WHERE digest=?", (key,)
        ).fetchone()
        if row is None or not 0 <= row[0] <= MAX_FILE:
            raise ValueError("Missing or oversized evidence object")
        size, payload, compressed, bundle, member = row
        if bundle is not None:
            if container:
                raise ValueError("Nested evidence reference")
            with zipfile.ZipFile(io.BytesIO(self.get(bundle, container=True))) as archive:
                if archive.getinfo(member).file_size != size:
                    raise ValueError("Bundle member size differs")
                data = archive.read(member)
        elif compressed:
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
                data = stream.read(size + 1)
        else:
            data = payload
        if not isinstance(data, bytes) or len(data) != size or digest(data) != key:
            raise ValueError("Evidence object checksum differs")
        return data

    def manifest(self, name):
        row = self.db.execute(
            "SELECT digest,manifest FROM archives WHERE name=?", (name,)
        ).fetchone()
        if row is None or digest(row[1].encode()) != row[0]:
            raise ValueError("Missing or changed archive manifest")
        record = json.loads(row[1])
        if record.get("format") != "p9-evidence-v1":
            raise ValueError("Unknown archive format")
        return record

    def contains(self, name):
        return (
            self.db.execute("SELECT 1 FROM archives WHERE name=?", (name,)).fetchone() is not None
        )

    def add(self, name, roots, *, exclude=(), bundles=()):
        if self.contains(name):
            self.verify(name)
            return self.manifest(name)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", name):
            raise ValueError("Invalid archive name")
        roots = {k: Path(p).absolute() for k, p in roots.items()}
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", k) or p.resolve() != p
            for k, p in roots.items()
        ):
            raise ValueError("Invalid archive roots")
        exclude = {str(Path(p).absolute()) for p in exclude}
        entries, total = {}, 0
        for label, root in roots.items():
            for base, dirs, files in os.walk(root, followlinks=False):
                if any((Path(base) / n).is_symlink() for n in dirs):
                    raise ValueError("Redirected evidence directory")
                for filename in sorted(files):
                    p = ordinary(Path(base) / filename)
                    if str(p) in exclude:
                        continue
                    info = p.stat()
                    total += info.st_size
                    if info.st_size > MAX_FILE or total > MAX_ARCHIVE:
                        raise ValueError(
                            "Archive exceeds metadata limit; exclude replayable pixels"
                        )
                    entries[f"{label}/{p.relative_to(root).as_posix()}"] = (p, identity(info))
        with self.db:
            for path in bundles:
                self.bundle(ordinary(path).read_bytes())
            record = {
                "format": "p9-evidence-v1",
                "roots": {k: str(v) for k, v in roots.items()},
                "files": {},
                "reacquire": sorted(exclude),
            }
            for relative, (path, before) in entries.items():
                data = path.read_bytes()
                if identity(path.stat()) != before:
                    raise ValueError("Evidence changed during archiving")
                key = self.put(data)
                record["files"][relative] = {"sha256": key, "identity": before}
            raw = canonical(record)
            self.db.execute("INSERT INTO archives VALUES(?,?,?)", (name, digest(raw.encode()), raw))
            self.verify(name)
        return record

    def verify(self, name):
        record = self.manifest(name)
        verified = set()
        for relative, item in record["files"].items():
            parts = Path(relative).parts
            if (
                len(parts) < 2
                or parts[0] not in record["roots"]
                or ".." in parts
                or Path(relative).is_absolute()
            ):
                raise ValueError("Unsafe archive member")
            key = item["sha256"]
            if key not in verified:
                self.get(key)
                verified.add(key)
        return record

    def restore(self, name, destination):
        record = self.verify(name)
        destination = Path(destination).absolute()
        if destination.resolve() != destination or destination.exists():
            raise ValueError("Restore requires a new, unredirected directory")
        destination.mkdir(parents=True)
        for relative, item in record["files"].items():
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(self.get(item["sha256"]))
            if digest(path.read_bytes()) != item["sha256"]:
                raise ValueError("Restored evidence checksum differs")
        return record
