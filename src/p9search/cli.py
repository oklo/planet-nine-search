"""Command line entry points; scientific imports are deferred until a search."""

import argparse
import os
import sys
from pathlib import Path

from .client import DEFAULT_COORDINATOR, Client
from .storage import GIB, DirectoryLock, DiskBudget, canonical, read_json, sha256


def parser():
    root = argparse.ArgumentParser(
        description="Search TESS fields with Gaia subtraction and joint-sector Hough accumulation."
    )
    commands = root.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="show the shared queue")
    status.add_argument("--coordinator", default=DEFAULT_COORDINATOR)
    worker = commands.add_parser("worker", help="claim and search a field")
    worker.add_argument("--coordinator", default=DEFAULT_COORDINATOR)
    worker.add_argument(
        "--name", default="volunteer", help="public worker label; no personal information needed"
    )
    worker.add_argument(
        "--continuous",
        action="store_true",
        help="keep claiming work until interrupted or the disk budget is exhausted",
    )
    local = commands.add_parser("run", help="search a downloaded field specification locally")
    local.add_argument("task", type=Path)
    for command in (worker, local):
        command.add_argument("--directory", type=Path, default=Path("work"))
        command.add_argument(
            "--max-disk-gb",
            type=float,
            default=12.0,
            help="maximum GiB owned by this work directory",
        )
        command.add_argument(
            "--reserve-gb", type=float, default=20.0, help="minimum free GiB to leave on the volume"
        )
        command.add_argument("--device", choices=("auto", "cpu", "mps", "cuda"), default="auto")
        command.add_argument("--threads", type=int, default=4)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            print(canonical(Client(args.coordinator).request("status")))
            return 0
        budget = DiskBudget(args.directory, int(args.max_disk_gb * GIB), int(args.reserve_gb * GIB))
        if args.threads < 1:
            raise ValueError("threads must be positive")
        budget.check()
        if args.command == "worker":
            from .worker import work

            with DirectoryLock(args.directory, "worker.lock"):
                work(args, budget)
            return 0
        for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            os.environ[name] = str(args.threads)
        from . import runtime
        from .search import run
        from .tasks import validate_task

        runtime.configure(args.device, args.threads)
        spec = validate_task(read_json(args.task))
        folder = args.directory.resolve() / spec["task_id"]
        result = folder / "result.json"
        if result.exists():
            record = read_json(result)
            if record["task_id"] != spec["task_id"]:
                raise ValueError("Completed result identity differs")
            for name, digest in record["artifacts"].items():
                if sha256(folder / name) != digest:
                    raise ValueError("Completed result evidence changed")
            print(f"Completed result retained: {result}")
        else:
            with DirectoryLock(args.directory, "search.lock"):
                run(spec, folder, budget)
        return 0
    except KeyboardInterrupt:
        print("Stopped; local evidence retained.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"p9: {error}", file=sys.stderr)
        return 1
