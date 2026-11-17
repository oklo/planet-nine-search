"""Bounded local diagnostics; routine waits replace state instead of printing."""

import os
import selectors
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .storage import write_json

LOG_BYTES = 1024 * 1024


class BoundedOutput:
    """Drain subprocess output independently of lease renewal, retaining a tail."""

    def __init__(self, path, limit=LOG_BYTES):
        if limit < 1024:
            raise ValueError("Log limit must be at least 1024 bytes")
        self.path, self.limit = Path(path), limit
        self.error = None
        self.stopping = threading.Event()

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(fd, "r+b", buffering=0)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            self.stream.close()
            raise ValueError("Log must be a private ordinary file")
        os.fchmod(fd, 0o600)
        self.stream.seek(max(0, info.st_size - self.limit // 2))
        tail = self.stream.read(self.limit // 2)
        self.stream.seek(0)
        self.stream.truncate()
        self.stream.write(tail)
        self.reader, self.writer = os.pipe()
        os.set_blocking(self.reader, False)
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()
        return self

    def fileno(self):
        return self.writer

    def _append(self, chunk):
        end = self.stream.seek(0, os.SEEK_END)
        if end + len(chunk) > self.limit:
            size = min(end, self.limit // 2)
            self.stream.seek(end - size)
            tail = self.stream.read(size)
            self.stream.seek(0)
            self.stream.truncate()
            self.stream.write(tail)
        self.stream.write(chunk)

    def _drain(self):
        remaining = self.limit
        with selectors.DefaultSelector() as selector:
            selector.register(self.reader, selectors.EVENT_READ)
            while True:
                ready = selector.select(0.1)
                if not ready:
                    if self.stopping.is_set():
                        return
                    continue
                try:
                    chunk = os.read(self.reader, min(65536, self.limit // 2))
                except OSError as error:
                    self.error = error
                    return
                if not chunk:
                    return
                if self.error is None:
                    try:
                        self._append(chunk)
                    except OSError as error:
                        # Keep draining so a failed log disk cannot deadlock a child.
                        self.error = error
                if self.stopping.is_set():
                    remaining -= len(chunk)
                    if remaining <= 0:
                        return  # An inherited descriptor cannot keep shutdown alive.

    def check(self):
        if self.error is not None:
            raise self.error

    def __exit__(self, exc_type, *_):
        os.close(self.writer)
        self.stopping.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Log writer did not stop")
        os.close(self.reader)
        self.stream.close()
        if exc_type is None:
            self.check()


@contextmanager
def capture_output(path):
    """Bound even dependency/native output in a detached worker's session log."""
    with BoundedOutput(path) as output:
        sys.stdout.flush()
        sys.stderr.flush()
        saved = [os.dup(fd) for fd in (1, 2)]
        try:
            for fd in (1, 2):
                os.dup2(output.fileno(), fd)
            yield
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            for fd, original in zip((1, 2), saved, strict=True):
                os.dup2(original, fd)
                os.close(original)


class State:
    def __init__(self, directory, verbose=False):
        self.path = Path(directory) / "worker-state.json"
        self.verbose = verbose
        self.previous = None

    def __call__(self, state, detail="", **fields):
        record = {"state": state, "detail": str(detail)[:1024], **fields}
        if record == self.previous:
            return
        write_json(self.path, {**record, "updated": time.time()})
        self.previous = record
        if self.verbose:
            print(f"{state}: {record['detail']}", flush=True)
