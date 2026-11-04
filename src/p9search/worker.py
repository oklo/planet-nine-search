"""Supervise one isolated search process while maintaining its server lease."""

import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

from .client import Client, CoordinatorError
from .storage import canonical, read_json, write_json
from .tasks import PROTOCOL, validate_task


def stop_child(child):
    if child.poll() is not None:
        return
    if os.name == "posix":
        os.killpg(child.pid, signal.SIGTERM)
    else:
        child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(child.pid, signal.SIGKILL)
        else:
            child.kill()
        child.wait()


def submit(client, job_directory):
    result = read_json(job_directory / "result.json")
    reply = client.request("submit", client.lease_body(result=result))
    if reply != {"accepted": True, "verified": False}:
        raise ValueError("Unexpected submission receipt")
    write_json(job_directory / "submission.json", reply)
    client.clear_claim()
    print("Submitted for independent review.", flush=True)


def work(args, budget):
    root = Path(args.directory).resolve()
    client = Client(args.coordinator, root / "credentials.json")
    client.register(args.name)
    # If the previous response was lost, repeat the identical submission first.
    if "task_id" in client.credentials:
        old = root / client.credentials["task_id"]
        if (old / "result.json").exists():
            try:
                submit(client, old)
            except CoordinatorError as error:
                if error.status != 409:
                    raise
                client.clear_claim()
    while True:
        budget.check()
        if budget.available() < 128 * 2**20:
            raise RuntimeError(
                "No disk allocation remains; retain results or choose another work directory"
            )
        if "lease_token" not in client.credentials:
            client.credentials["lease_token"] = secrets.token_hex(32)
            client.save()
        claim = client.request(
            "claim",
            {
                "protocol": PROTOCOL,
                "available_bytes": budget.available(),
                "lease_token": client.credentials["lease_token"],
            },
        )
        if claim["task"] is None:
            print("No eligible field is available for this disk allocation.", flush=True)
            if not args.continuous:
                return
            time.sleep(min(60, claim.get("retry_after", 60)))
            continue
        spec = validate_task(claim["task"])
        if claim["task_id"] != spec["task_id"]:
            raise ValueError("Claim and field identity differ")
        import hashlib

        if claim["spec_sha256"] != hashlib.sha256(canonical(spec).encode()).hexdigest():
            raise ValueError("Coordinator specification hash differs")
        client.credentials["task_id"] = spec["task_id"]
        client.save()
        folder = root / spec["task_id"]
        folder.mkdir(exist_ok=True)
        write_json(folder / "task.json", spec)
        print(f"Claimed {spec['label']}: {folder}", flush=True)
        child = None
        deadline = time.monotonic() + claim["lease_seconds"] - 60
        next_heartbeat = time.monotonic() + min(300, claim["lease_seconds"] / 3)
        try:
            with (folder / "worker.log").open("a") as log:
                command = [
                    sys.executable,
                    "-m",
                    "p9search",
                    "run",
                    str(folder / "task.json"),
                    "--directory",
                    str(root),
                    "--max-disk-gb",
                    str(args.max_disk_gb),
                    "--reserve-gb",
                    str(args.reserve_gb),
                    "--device",
                    args.device,
                    "--threads",
                    str(args.threads),
                ]
                environment = dict(os.environ)
                for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
                    environment[name] = str(args.threads)
                child = subprocess.Popen(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    start_new_session=os.name == "posix",
                )
                while child.poll() is None:
                    budget.check()
                    now = time.monotonic()
                    if now >= deadline:
                        raise RuntimeError("Lease could not be renewed; stopping search")
                    if now >= next_heartbeat:
                        try:
                            reply = client.request("heartbeat", client.lease_body())
                            deadline = time.monotonic() + reply["lease_seconds"] - 60
                        except (requests.RequestException, CoordinatorError) as error:
                            if isinstance(error, CoordinatorError) and error.status < 500:
                                raise
                            print(
                                "Heartbeat temporarily unavailable; retrying within the lease.",
                                flush=True,
                            )
                        next_heartbeat = time.monotonic() + 60
                    time.sleep(2)
            if child.returncode != 0:
                raise RuntimeError(
                    f"Search exited {child.returncode}; inspect {folder / 'worker.log'}"
                )
        except BaseException as error:
            if child is not None:
                stop_child(child)
            try:
                client.request(
                    "release",
                    client.lease_body(
                        failed=not isinstance(error, KeyboardInterrupt), reason=str(error)
                    ),
                )
                client.clear_claim()
            except (requests.RequestException, CoordinatorError):
                pass  # An unreachable coordinator will expire the lease.
            raise
        # Keep the claim and exact result if acknowledgement is lost; next run
        # retries submission before claiming anything else.
        submit(client, folder)
        if not args.continuous:
            return
