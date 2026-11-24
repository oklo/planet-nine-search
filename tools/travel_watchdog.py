"""Notify the logged-in Mac operator of a sustained service interruption."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def last_worker_activity(path, state):
    updated = float(state.get("updated", 0))
    task = state.get("task_id", "")
    if len(task) == 64 and all(c in "0123456789abcdef" for c in task):
        progress = path.parent / task / "progress.json"
        if progress.exists():
            updated = max(updated, progress.stat().st_mtime)
    return updated


def issues(worker, publication, reviewer, now):
    problems = set()
    for name, path, timestamp in (
        ("worker", worker, "updated"),
        ("publication", publication / "status.json", "checked_epoch"),
        ("reviewer", reviewer / "status.json", "checked_epoch"),
    ):
        try:
            record = json.loads(path.read_text())
            age = now - (
                last_worker_activity(path, record) if name == "worker" else float(record[timestamp])
            )
            if record.get("state") in {
                "held",
                "stopped",
                "retry",
                "transport_wait",
                "capacity_wait",
            }:
                problems.add(name)
            elif age > 900:
                problems.add(name)
        except (KeyError, OSError, ValueError):
            problems.add(name)
    if (publication / "hold.json").exists():
        problems.add("publication")
    error = reviewer / "last-error.json"
    status = reviewer / "status.json"
    if error.exists() and (not status.exists() or error.stat().st_mtime > status.stat().st_mtime):
        problems.add("reviewer")
    return problems


def check(args):
    now = time.time()
    old = json.loads(args.state.read_text()) if args.state.exists() else {}
    state = {}
    for name in sorted(issues(args.worker, args.publication, args.reviewer, now)):
        incident = old.get(name, {"since": now})
        if now - incident["since"] >= 900 and not incident.get("sent"):
            subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    f'display notification "{name} needs attention" with title "Planet Nine"',
                ],
                check=True,
            )
            incident["sent"] = True
        state[name] = incident
    tmp = args.state.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, args.state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("worker", "publication", "reviewer", "state"):
        parser.add_argument(key, type=Path)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    assertion = None
    try:
        while True:
            check(args)
            if not args.watch:
                return
            try:
                state = json.loads(args.worker.read_text())
            except (OSError, ValueError):
                state = {}
            busy = (
                state.get("state") in {"searching", "submitting"}
                and time.time() - last_worker_activity(args.worker, state) < 900
            )
            if busy and (assertion is None or assertion.poll() is not None):
                assertion = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
            if not busy and assertion is not None:
                assertion.terminate()
                assertion.wait()
                assertion = None
            time.sleep(60)
    finally:
        if assertion is not None:
            assertion.terminate()
            assertion.wait()


if __name__ == "__main__":
    main()
