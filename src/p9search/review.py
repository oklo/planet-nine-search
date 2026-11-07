"""Apply a master-issued review plan and attest the checks actually performed."""

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
from .validation import check_local_run, check_report, require
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


def verify_signed(document, key):
    payload = base64.b64decode(document["payload"], validate=True)
    require(
        hmac.compare_digest(
            hmac.new(key, payload, hashlib.sha256).hexdigest(), document["signature"]
        ),
        "Untrusted review plan signature",
    )
    return bounded_json(payload.decode())


def review(args):
    spec = validate_task(load_bounded(args.task))
    submission = load_bounded(args.submission)
    require(
        set(submission) == {"task_id", "worker_id", "submission_digest", "result_json"},
        "Expected one exported coordinator submission",
    )
    raw = submission["result_json"]
    require(
        isinstance(raw, str)
        and hashlib.sha256(raw.encode()).hexdigest() == submission["submission_digest"],
        "Submission digest differs",
    )
    remote = bounded_json(raw)
    binding = {
        "task_id": spec["task_id"],
        "spec_sha256": hashlib.sha256(canonical(spec).encode()).hexdigest(),
        "submission_digest": submission["submission_digest"],
        "worker_id": submission["worker_id"],
    }
    require(
        submission["task_id"] == spec["task_id"]
        and remote.get("task_id") == spec["task_id"]
        and remote.get("spec_sha256") == binding["spec_sha256"],
        "Submission belongs to a different trusted task",
    )
    keypath = Path(args.key)
    require(not keypath.stat().st_mode & 0o077, "Review key must be private (chmod 600)")
    key = bytes.fromhex(keypath.read_text().strip())
    require(len(key) == 32, "Expected a 32-byte hexadecimal review key")
    plan_path = getattr(args, "plan", None)
    plan_document, plan = None, None
    method, issue = "full_replay", None
    if plan_path:
        plan_document = load_bounded(plan_path)
        plan = verify_signed(plan_document, key)
        require(
            plan.get("contract") == "review-plan-v1"
            and all(plan.get(k) == v for k, v in binding.items())
            and plan.get("source_sha256") == remote.get("source_sha256")
            and plan.get("mode") in ("full_replay", "trusted_local", "statistical"),
            "Review plan does not bind this submission",
        )
        method = plan["mode"]
        approved = getattr(args, "approved_source", None) or [source_hash()]
        require(remote.get("source_sha256") in approved, "Unapproved scientific release")
        try:
            check_report(spec, remote, approved)
        except (ValueError, KeyError, TypeError) as error:
            method, issue = "full_replay", str(error)
        if (
            remote.get("state") == "needs_followup"
            or remote.get("candidates")
            or remote.get("candidate_count", 0)
        ):
            method = "full_replay"
    root = Path(args.directory).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_json(root / "quarantine" / "submission.json", submission, private=True)
    write_json(root / "task.json", spec)
    launch = execution_manifest()
    write_json(root / "review-launch.json", launch, private=True)
    if plan_document:
        write_json(root / "plan.json", plan_document, private=True)
    checks = ["task and release identity", "bounded report and numerical consistency"]
    result = remote
    if method == "full_replay":
        require(
            remote.get("source_sha256") == source_hash(),
            "Full replay requires the submitted scientific release",
        )
        result = replay(args, root, spec)
        if plan:
            check_report(spec, result, [source_hash()])
        checks = [
            "archive reacquired",
            "full blind search and controls",
            "native candidate photometry",
            "launch and completion source identity",
        ]
        if result["state"] == "searched" and spec["injections"]:
            checks.append("declared native-injection response tests and admitted blind searches")
        if result["state"] == "quality_rejected":
            checks = [
                "archive reacquired for screened sectors",
                "foreground quality rejection",
                "launch and completion source identity",
            ]
    elif method == "trusted_local":
        local_run = getattr(args, "local_run", None)
        require(local_run is not None, "Trusted-local review requires an operator-selected run")
        check_local_run(local_run, spec, remote)
        checks.append("operator-selected local artifacts and archive provenance")
    require(source_hash() == launch["source_sha256"], "Review source changed during execution")
    accepted = issue is None and (method != "full_replay" or outcomes_agree(remote, result))
    write_json(root / "reviewed-result.json", result)
    payload = {
        **binding,
        "contract": "statistical-review-v1" if plan else "trusted-full-replay-v1",
        "disposition": "accepted" if accepted else "disagrees",
        "execution": launch,
        "policy": spec["policy"],
        "checks": checks,
        "replay_state": result["state"],
        "sensitivity_credit": 0,
    }
    if plan:
        payload.update(
            plan_signature=plan_document["signature"],
            review_method=method,
            audit_probability=plan["audit_probability"],
            audit_reason=plan["reason"],
            consistency_failure=issue,
            result_sha256=sha256(root / "reviewed-result.json"),
            reported_inputs=result["inputs"],
            full_replay_verified=method == "full_replay" and accepted,
            replay_seconds=result["elapsed_seconds"] if method == "full_replay" else 0,
        )
    else:
        payload.update(
            replay_result_sha256=sha256(root / "replay" / spec["task_id"] / "result.json"),
            trusted_inputs=result["inputs"],
            replay_seconds=result["elapsed_seconds"],
        )
    write_json(root / "receipt.json", signed_receipt(payload, key), private=True)
    print(f"Review {method}: {payload['disposition']}: {root / 'receipt.json'}")


def replay(args, root, spec):
    command = [
        sys.executable,
        "-m",
        "p9search",
        "run",
        str(root / "task.json"),
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
    require(code == 0, "Trusted replay failed; no acceptance record created")
    result = read_json(root / "replay" / spec["task_id"] / "result.json")
    require(
        source_hash() == result["source_sha256"] == result["launch"]["source_sha256"],
        "Replay source identity changed",
    )
    return result
