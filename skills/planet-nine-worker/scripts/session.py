"""Run an ordinary continuous worker independently of an agent's conversation."""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from p9search.cli import parser as worker_parser
from p9search.diagnostics import BoundedOutput
from p9search.storage import DirectoryLock, load_bounded, write_json


def live_process(record):
    if not record:
        return None
    try:
        process = psutil.Process(record["pid"])
        if (
            process.create_time() == record["created"]
            # macOS Python replaces its launcher and changes argv[0].
            and process.cmdline()[1:] == record["command"][1:]
            and process.status() != psutil.STATUS_ZOMBIE
        ):
            return process
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        pass
    return None


def tail(path, size=8192):
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - size))
            return stream.read(size).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def status(session, details=False):
    state = session / "session.json"
    if not state.exists():
        return {"state": "not_started"}
    record = load_bounded(state)
    process = live_process(record)
    root = Path(record["directory"])
    log = Path(record["log"])
    result = {
        "state": "running" if process else "exited",
        "pid": record["pid"],
        "started": record["created"],
        "directory": str(root),
        "log": str(log),
        "log_updated": log.stat().st_mtime if log.exists() else None,
        "free_gib": round(shutil.disk_usage(root).free / 2**30, 2) if root.exists() else None,
        "retained_receipts": len(list((root / "receipts").glob("*.json.gz"))),
    }
    if details or not process:
        result["last_output"] = tail(log).splitlines()[-8:]
    state_path = root / "worker-state.json"
    if state_path.exists() and state_path.stat().st_mtime >= record["created"]:
        result["worker"] = load_bounded(state_path)
    # A stale task directory is not proof of an active search. Use the live child.
    if process:
        for child in process.children():
            try:
                command = child.cmdline()
                if len(command) < 5 or command[1:4] != ["-m", "p9search", "run"]:
                    continue
                task = Path(command[4]).resolve()
                if task.name != "task.json" or task.parent.parent != root:
                    continue
                result["task_id"] = task.parent.name
                progress = task.with_name("progress.json")
                if progress.exists():
                    result["progress"] = load_bounded(progress)
                    result["progress_updated"] = progress.stat().st_mtime
            except (psutil.NoSuchProcess, FileNotFoundError):
                continue
    return result


def start(session, arguments):
    record_path = session / "session.json"
    resuming = False
    if record_path.exists():
        record = load_bounded(record_path)
        if live_process(record):
            return status(session)
        if not arguments:
            arguments = record["command"][4:]
            resuming = True
    args = worker_parser().parse_args(["worker", "--continuous", *arguments])
    root = args.directory.expanduser().resolve()
    credentials = (args.credentials or session / "credentials.json").expanduser().resolve()
    # Catch another worker or an orphaned search before detaching. The worker
    # acquires its own locks again, closing the race with independently started jobs.
    with (
        DirectoryLock(root, "worker.lock"),
        DirectoryLock(root, "search.lock"),
        DirectoryLock(credentials.parent, credentials.name + ".lock"),
    ):
        pass
    log = session / "worker.log"
    command = [sys.executable, "-m", "p9search", "worker"]
    if resuming:
        command.extend(arguments)
        if args.log_file is None:  # Upgrade a session created before bounded logs.
            command.extend(["--log-file", str(log)])
    else:
        command.extend(
            [
                "--continuous",
                *arguments,
                "--directory",
                str(root),
                "--credentials",
                str(credentials),
                "--log-file",
                str(log),
            ]
        )
    # One previous log is enough for restart diagnosis; search evidence has its
    # own retention policy. Never truncate the log of a live worker.
    if log.exists():
        with BoundedOutput(log):
            pass  # Also cap a log inherited from an older release.
        log.replace(session / "worker.previous.log")
    child = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=dict(os.environ, PYTHONUNBUFFERED="1"),
    )
    try:
        process = psutil.Process(child.pid)
        write_json(
            record_path,
            {
                "pid": child.pid,
                "created": process.create_time(),
                "command": process.cmdline(),
                "directory": str(root),
                "log": str(log),
            },
            private=True,
        )
    except BaseException:
        child.send_signal(signal.SIGINT)
        raise
    try:
        child.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    return status(session)


def stop(session):
    record_path = session / "session.json"
    process = live_process(load_bounded(record_path)) if record_path.exists() else None
    if process:
        # SIGINT follows the worker's lease-release and child-cleanup path.
        process.send_signal(signal.SIGINT)
        deadline = time.monotonic() + 45
        while live_process(load_bounded(record_path)):
            if time.monotonic() >= deadline:
                raise RuntimeError("Worker is still stopping; inspect its log before retrying")
            time.sleep(0.2)
    return status(session)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, default=Path.home() / ".local/state/planet-nine")
    parser.add_argument("--details", action="store_true", help="include recent log output")
    parser.add_argument("action", choices=("start", "status", "stop"))
    parser.add_argument("worker_arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    arguments = args.worker_arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if args.action != "start" and arguments:
        parser.error("Worker arguments belong after start")
    session = args.session.expanduser().resolve()
    session.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with DirectoryLock(session, "control.lock"):
            if args.action == "start":
                result = start(session, arguments)
            elif args.action == "stop":
                result = stop(session)
            else:
                result = status(session, args.details)
        print(json.dumps(result, indent=2))
        return int(args.action == "start" and result["state"] != "running")
    except (OSError, RuntimeError, ValueError, psutil.Error) as error:
        print(f"session: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
