"""Notify the logged-in Mac operator of a sustained service interruption."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def issues(worker, publication, reviewer, now):
    problems = set()
    for name, path, timestamp in (
        ("worker", worker, "updated"),
        ("publication", publication / "status.json", "checked_epoch"),
        ("reviewer", reviewer / "status.json", "checked_epoch"),
    ):
        try:
            record = json.loads(path.read_text())
            age = now - float(record[timestamp])
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("worker", "publication", "reviewer", "state"):
        parser.add_argument(key, type=Path)
    args = parser.parse_args()
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


if __name__ == "__main__":
    main()
