"""Quarantine remote reports and attest only a fresh, trusted full replay."""

import base64
import hashlib
import hmac
import math
import os
import subprocess
import sys
from pathlib import Path

from .provenance import execution_manifest, source_hash
from .storage import bounded_json, canonical, load_bounded, read_json, sha256, write_json
from .tasks import validate_task
from .worker import stop_child


def outcomes_agree(submitted, replay):
    """Differences require operator review; no best-effort acceptance."""
    exact = (
        "task_id",
        "spec_sha256",
        "protocol",
        "package_version",
        "policy",
        "state",
        "candidate_count",
        "source_sha256",
        "inputs",
        "dependencies",
    )
    if any(submitted.get(k) != replay.get(k) for k in exact):
        return False
    if submitted.get("launch", {}).get("source_sha256") != replay["source_sha256"]:
        return False

    def close(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, dict):
            return a.keys() == b.keys() and all(close(a[k], b[k]) for k in a)
        if isinstance(a, list):
            return len(a) == len(b) and all(close(x, y) for x, y in zip(a, b, strict=True))
        if isinstance(a, float):
            return (
                math.isfinite(a)
                and math.isfinite(b)
                and math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-8)
            )
        return a == b

    return all(
        close(submitted.get(k), replay.get(k))
        for k in (
            "quality",
            "candidates",
            "control_maximum",
            "arm_maxima",
            "injections",
            "grid",
            "restricted_motions",
        )
    )


def signed_receipt(payload, key):
    raw = canonical(payload).encode()
    return {
        "payload": base64.b64encode(raw).decode(),
        "signature": hmac.new(key, raw, hashlib.sha256).hexdigest(),
    }


def review(args):
    spec = validate_task(load_bounded(args.task))
    submission = load_bounded(args.submission)
    if set(submission) != {"task_id", "worker_id", "submission_digest", "result_json"}:
        raise ValueError("Expected one exported coordinator submission")
    raw = submission["result_json"]
    if (
        not isinstance(raw, str)
        or hashlib.sha256(raw.encode()).hexdigest() != submission["submission_digest"]
    ):
        raise ValueError("Submission digest differs")
    remote = bounded_json(raw)
    spec_hash = hashlib.sha256(canonical(spec).encode()).hexdigest()
    if (
        submission["task_id"] != spec["task_id"]
        or remote.get("task_id") != spec["task_id"]
        or remote.get("spec_sha256") != spec_hash
    ):
        raise ValueError("Submission belongs to a different trusted task")
    keypath = Path(args.key)
    if keypath.stat().st_mode & 0o077:
        raise ValueError("Review key must be private (chmod 600)")
    key = bytes.fromhex(keypath.read_text().strip())
    if len(key) != 32:
        raise ValueError("Expected a 32-byte hexadecimal review key")
    root = Path(args.directory).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Remote data is never placed in the replay's geometry, source, or cache.
    write_json(root / "quarantine" / "submission.json", submission, private=True)
    task = root / "task.json"
    write_json(task, spec)
    launch = execution_manifest()
    write_json(root / "review-launch.json", launch, private=True)
    command = [
        sys.executable,
        "-m",
        "p9search",
        "run",
        str(task),
        "--directory",
        str(root / "replay"),
        "--max-disk-gb",
        str(args.max_disk_gb),
        "--reserve-gb",
        str(args.reserve_gb),
        "--max-memory-gb",
        str(args.max_memory_gb),
        "--max-hours",
        str(args.max_hours),
        "--device",
        args.device,
        "--threads",
        str(args.threads),
    ]
    for origin in args.bundle_origin:
        command.extend(["--bundle-origin", origin])
    with (root / "replay.log").open("w") as log:
        child = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name == "posix"
        )
        try:
            code = child.wait(timeout=args.max_hours * 3600 + 30)
        finally:
            stop_child(child)
    if code != 0:
        raise RuntimeError("Trusted replay failed; no acceptance record created")
    result_path = root / "replay" / spec["task_id"] / "result.json"
    replay = read_json(result_path)
    if not (
        source_hash()
        == launch["source_sha256"]
        == replay["source_sha256"]
        == replay["launch"]["source_sha256"]
    ):
        raise RuntimeError("Replay source identity changed")
    disposition = "accepted" if outcomes_agree(remote, replay) else "disagrees"
    payload = {
        "contract": "trusted-full-replay-v1",
        "task_id": spec["task_id"],
        "spec_sha256": spec_hash,
        "submission_digest": submission["submission_digest"],
        "worker_id": submission["worker_id"],
        "disposition": disposition,
        "replay_result_sha256": sha256(result_path),
        "trusted_inputs": replay["inputs"],
        "execution": launch,
        "policy": spec["policy"],
        "checks": [
            "archive reacquired",
            "full blind search and controls",
            "native candidate photometry",
            "launch and completion source identity",
        ],
        "replay_state": replay["state"],
        "replay_seconds": replay["elapsed_seconds"],
        "sensitivity_credit": 0,
    }
    if replay["state"] == "searched" and spec["injections"]:
        payload["checks"].append(
            "declared native-injection response tests and admitted blind searches"
        )
    # Record the checks actually reached by an early baseline rejection.
    if replay["state"] == "quality_rejected":
        payload["checks"] = [
            "archive reacquired for screened sectors",
            "foreground quality rejection",
            "launch and completion source identity",
        ]
    write_json(root / "receipt.json", signed_receipt(payload, key), private=True)
    print(f"Trusted replay {disposition}: {root / 'receipt.json'}")
