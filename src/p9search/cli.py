"""Command line entry points; scientific imports are deferred until a search."""

import argparse
import math
import os
import sys
from pathlib import Path

from .client import DEFAULT_COORDINATOR, Client
from .limits import ResourceGuard
from .network import DEFAULT_ORIGINS
from .storage import GIB, DirectoryLock, DiskBudget, canonical, load_bounded, read_json, sha256


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
    reviewer = commands.add_parser(
        "review", help="fully replay an exported submission on the trusted master"
    )
    reviewer.add_argument("task", type=Path)
    reviewer.add_argument("submission", type=Path)
    reviewer.add_argument("--key", type=Path, required=True)
    for command in (worker, local, reviewer):
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
        command.add_argument("--max-memory-gb", type=float, default=24.0)
        command.add_argument("--max-hours", type=float, default=4.0)
        command.add_argument("--bundle-origin", action="append", default=list(DEFAULT_ORIGINS))
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            print(canonical(Client(args.coordinator).request("status")))
            return 0
        if not all(math.isfinite(v) and v > 0 for v in (args.max_memory_gb, args.max_hours)):
            raise ValueError("Positive finite memory and runtime limits required")
        if args.threads < 1:
            raise ValueError("threads must be positive")
        if args.command == "review":
            from .review import review

            review(args)
            return 0
        budget = DiskBudget(args.directory, int(args.max_disk_gb * GIB), int(args.reserve_gb * GIB))
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

        runtime.configure(args.device, args.threads, max_memory_bytes=int(args.max_memory_gb * GIB))
        spec = validate_task(load_bounded(args.task))
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
            with (
                DirectoryLock(args.directory, "search.lock"),
                ResourceGuard(int(args.max_memory_gb * GIB), args.max_hours * 3600),
            ):
                run(spec, folder, budget, origins=args.bundle_origin)
        return 0
    except KeyboardInterrupt:
        print("Stopped; local evidence retained.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"p9: {error}", file=sys.stderr)
        return 1
