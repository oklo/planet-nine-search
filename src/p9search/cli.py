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
    restore = commands.add_parser("restore", help="restore archived field evidence for inspection")
    restore.add_argument("store", type=Path)
    restore.add_argument("name")
    restore.add_argument("--directory", type=Path, required=True)
    worker = commands.add_parser("worker", help="claim and search a field")
    worker.add_argument("--coordinator", default=DEFAULT_COORDINATOR)
    worker.add_argument(
        "--credentials",
        type=Path,
        help="private credential file (default: credentials.json in the work directory)",
    )
    worker.add_argument(
        "--name", default="volunteer", help="public worker label; no personal information needed"
    )
    worker.add_argument(
        "--continuous",
        action="store_true",
        help="keep searching; wait through queue, review and capacity pauses",
    )
    worker.add_argument(
        "--keep-data", action="store_true", help="retain downloaded pixels for local inspection"
    )
    worker.add_argument(
        "--keep-evidence", action="store_true", help="retain local diagnostics for operator review"
    )
    worker.add_argument(
        "--keep-results",
        type=int,
        default=32,
        help="maximum recent compact results to retain (also limited to 256 MiB)",
    )
    local = commands.add_parser("run", help="search a downloaded field specification locally")
    local.add_argument("task", type=Path)
    reviewer = commands.add_parser(
        "review", help="validate a submission using an operator-issued audit plan"
    )
    reviewer.add_argument("task", type=Path)
    reviewer.add_argument("submission", type=Path)
    reviewer.add_argument("--key", type=Path, required=True)
    reviewer.add_argument("--plan", type=Path, help="signed plan from admin.php review-plan")
    reviewer.add_argument(
        "--local-run", type=Path, help="operator-selected trusted local result directory"
    )
    reviewer.add_argument(
        "--approved-source",
        action="append",
        help="approved source hash (default: installed release)",
    )
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
        if args.command == "restore":
            from .archives import EvidenceStore

            with EvidenceStore(args.store) as store:
                store.restore(args.name, args.directory)
            print(f"Restored {args.name} to {args.directory}; image pixels can be reacquired.")
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
        if args.command == "worker":
            from .worker import work

            if args.keep_results < 1:
                raise ValueError("Keep at least one compact result")
            with DirectoryLock(args.directory, "worker.lock"):
                work(args, budget)
            return 0
        budget.check()
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
