"""Resident-memory and wall-time supervision independent of search progress."""

import os
import threading
import time

import psutil

from .storage import GIB


def worker_memory_gib(total_bytes):
    """Use 60% of host RAM, rounded down to whole GiB and capped at 12."""
    return max(1, min(12, 3 * total_bytes // (5 * GIB)))


class ResidentMemoryLimit(RuntimeError):
    """The search process tree crossed its configured resident-memory cap."""


def check_process(pid, started, memory_bytes, seconds):
    if time.monotonic() - started > seconds:
        raise RuntimeError("Search runtime limit exceeded")
    try:
        process = psutil.Process(pid)
        processes = [process, *process.children(recursive=True)]
        resident = sum(p.memory_info().rss for p in processes if p.is_running())
    except psutil.NoSuchProcess:
        return
    if resident > memory_bytes:
        raise ResidentMemoryLimit(
            f"Search resident-memory limit exceeded ({resident} > {memory_bytes} bytes)"
        )
    return resident


class ResourceGuard:
    def __init__(self, memory_bytes, seconds):
        if not memory_bytes > 0 or not seconds > 0:
            raise ValueError("Positive memory and runtime limits required")
        self.memory, self.seconds = memory_bytes, seconds
        self.done = threading.Event()

    def __enter__(self):
        self.peak_bytes = 0
        self.started = time.monotonic()

        def watch():
            while not self.done.wait(0.5):
                try:
                    resident = check_process(os.getpid(), self.started, self.memory, self.seconds)
                    self.peak_bytes = max(self.peak_bytes, resident or 0)
                except RuntimeError as error:
                    os.write(2, (str(error) + "\n").encode())
                    os._exit(76 if isinstance(error, ResidentMemoryLimit) else 75)

        self.thread = threading.Thread(target=watch, name="resource-guard", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.done.set()
        self.thread.join()
        if self.peak_bytes:
            os.write(2, f"Sampled peak search RSS: {self.peak_bytes} bytes\n".encode())


class FieldDataError(ValueError):
    """The verified input lacks usable data for this search."""
