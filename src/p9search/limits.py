"""Resident-memory and wall-time supervision independent of search progress."""

import os
import threading
import time

import psutil


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
        raise RuntimeError("Search resident-memory limit exceeded")


class ResourceGuard:
    def __init__(self, memory_bytes, seconds):
        if not memory_bytes > 0 or not seconds > 0:
            raise ValueError("Positive memory and runtime limits required")
        self.memory, self.seconds = memory_bytes, seconds
        self.done = threading.Event()

    def __enter__(self):
        self.started = time.monotonic()

        def watch():
            while not self.done.wait(0.5):
                try:
                    check_process(os.getpid(), self.started, self.memory, self.seconds)
                except RuntimeError as error:
                    os.write(2, (str(error) + "\n").encode())
                    os._exit(75)

        self.thread = threading.Thread(target=watch, name="resource-guard", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.done.set()
        self.thread.join()
