import subprocess
import sys
from types import SimpleNamespace

import pytest
import requests

from p9search import worker
from p9search.storage import DirectoryLock, DiskBudget, read_json

from .test_tasks import specification


def test_stopping_a_search_waits_for_actual_process_exit():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        worker.stop_child(child)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_lost_submission_acknowledgement_retains_exact_claim_and_result(tmp_path):
    from .test_retention import completed_job

    job = completed_job(tmp_path, acknowledged=False)
    original = read_json(job / "result.json")

    class LostReply:
        def lease_body(self, **extra):
            return {"lease_token": "private", **extra}

        def request(self, action, body):
            assert action == "submit"
            raise requests.Timeout("acknowledgement lost")

        def clear_claim(self):
            raise AssertionError("An unacknowledged claim must remain retryable")

    with pytest.raises(requests.Timeout):
        worker.submit(LostReply(), job)
    assert read_json(job / "result.json") == original
    assert not (job / "submission.json").exists()


def test_lost_lease_stops_child_and_never_submits(tmp_path, monkeypatch):
    import hashlib

    from p9search.storage import canonical

    spec = specification()
    actions = []

    class LeaseLost:
        credentials = {}

        def __init__(self, *args):
            self.credentials = {}

        def register(self, name):
            pass

        def save(self):
            pass

        def clear_claim(self):
            self.credentials.clear()

        def lease_body(self, **extra):
            return extra

        def request(self, action, body):
            actions.append(action)
            if action == "claim":
                return {
                    "task": spec,
                    "task_id": spec["task_id"],
                    "lease_seconds": 1800,
                    "spec_sha256": hashlib.sha256(canonical(spec).encode()).hexdigest(),
                }
            if action == "submit":
                raise AssertionError("Lost lease must never submit")
            return {"released": True}

    class Child:
        returncode = None

        def poll(self):
            return self.returncode

    child = Child()
    clock = iter([0.0, 0.0, 1801.0])
    monkeypatch.setattr(worker, "Client", LeaseLost)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(worker, "stop_child", lambda p: setattr(p, "returncode", -15))
    args = SimpleNamespace(
        directory=tmp_path,
        credentials=None,
        coordinator="https://example.org/api",
        name="test",
        continuous=False,
        max_disk_gb=2,
        reserve_gb=0,
        device="cpu",
        threads=1,
        max_memory_gb=24,
        max_hours=4,
        bundle_origin=["https://oklo.org"],
    )
    with pytest.raises(RuntimeError, match="Lease"):
        worker.work(args, DiskBudget(tmp_path, 2**30, 0))
    assert child.returncode == -15
    assert actions == ["claim", "release"]


def test_external_work_keeps_credentials_on_private_disk(tmp_path, monkeypatch):
    from p9search.cli import parser

    work = tmp_path / "shared"
    credentials = tmp_path / "private" / "worker.json"
    args = parser().parse_args(
        [
            "worker",
            "--directory",
            str(work),
            "--credentials",
            str(credentials),
        ]
    )

    def request(self, action, body):
        if action == "register":
            return {"worker_id": "approved-worker"}
        assert action == "claim"
        return {"task": None}

    monkeypatch.setattr(worker.Client, "request", request)
    worker.work(args, DiskBudget(work, 2**30, 0))
    saved = read_json(credentials)
    assert saved["worker_id"] == "approved-worker"
    assert saved["token"] and saved["lease_token"]
    assert credentials.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in work.iterdir()] == ["worker-state.json"]


def test_one_worker_identity_cannot_run_in_two_work_directories(tmp_path):
    from p9search.cli import parser

    credentials = tmp_path / "private" / "worker.json"
    args = parser().parse_args(
        [
            "worker",
            "--directory",
            str(tmp_path / "second-work"),
            "--credentials",
            str(credentials),
        ]
    )
    with DirectoryLock(credentials.parent, credentials.name + ".lock"):
        with pytest.raises(RuntimeError, match="Another process"):
            worker.work(args, DiskBudget(args.directory, 2**30, 0))
    assert not credentials.exists()


def test_continuous_worker_reuses_space_across_download_search_submission_cycles(
    tmp_path, monkeypatch
):
    import hashlib

    from p9search.cli import parser
    from p9search.storage import canonical

    from .test_retention import completed_job

    count = 20
    claimed = []
    submitted = []
    waits = []
    transient_claim = False
    transient_submit = False
    completed_child = None
    shortages = set()
    registrations = []

    class Coordinator:
        def __init__(self, *args):
            self.credentials = {}

        def register(self, name):
            registrations.append(name)
            self.credentials["worker_id"] = "test-worker"

        def save(self):
            pass

        def clear_claim(self):
            self.credentials.pop("task_id", None)
            self.credentials.pop("lease_token", None)

        def lease_body(self, **extra):
            return extra

        def request(self, action, body):
            nonlocal transient_claim, transient_submit
            if action == "claim":
                if len(submitted) == count:
                    return {"task": None, "retry_after": 300}
                if len(submitted) in (0, 7) and len(submitted) not in shortages:
                    shortages.add(len(submitted))
                    return {
                        "task": None,
                        "reason": "Waiting for coordinator replenishment",
                        "retry_after": 300,
                    }
                if len(submitted) == 3 and not transient_claim:
                    transient_claim = True
                    raise requests.Timeout("temporary claim interruption")
                spec = specification(label=f"field {len(submitted)}")
                claimed.append(spec["task_id"])
                return {
                    "task": spec,
                    "task_id": spec["task_id"],
                    "lease_seconds": 1800,
                    "spec_sha256": hashlib.sha256(canonical(spec).encode()).hexdigest(),
                }
            assert action == "submit"
            if len(submitted) == 5 and not transient_submit:
                transient_submit = True
                raise requests.Timeout("lost acknowledgement")
            submitted.append(body["result"]["task_id"])
            return {"accepted": True, "verified": False}

    class Child:
        returncode = 0

        def __init__(self, *args, **kwargs):
            nonlocal completed_child
            completed_child = completed_job(
                tmp_path, len(submitted), acknowledged=False, payload=50000
            )

        def poll(self):
            return 0

    def sleep(seconds):
        waits.append(seconds)
        if len(submitted) == count:
            raise KeyboardInterrupt

    monkeypatch.setattr(worker, "Client", Coordinator)
    monkeypatch.setattr(worker.subprocess, "Popen", Child)
    monkeypatch.setattr(worker.time, "sleep", sleep)
    args = parser().parse_args(
        [
            "worker",
            "--continuous",
            "--directory",
            str(tmp_path),
            "--keep-results",
            "3",
            "--reserve-gb",
            "0",
        ]
    )
    budget = DiskBudget(tmp_path, 2**30, 0)
    with pytest.raises(KeyboardInterrupt):
        worker.work(args, budget)
    assert len(claimed) == len(submitted) == count
    assert len(set(submitted)) == count
    assert not list(tmp_path.rglob("*.npy"))
    assert len(list(tmp_path.glob("receipts/*.json.gz"))) == 3
    assert not completed_child.exists()
    assert budget.used() < 60000
    assert len(registrations) == 1
    assert shortages == {0, 7}
    assert waits == [300, 60, 60, 300, 300]


def test_continuous_capacity_pause_recovers_without_registering_another_worker(
    tmp_path, monkeypatch
):
    from p9search.cli import parser
    from p9search.storage import DiskCapacityError

    calls = []
    checks = 0

    class Coordinator:
        def __init__(self, *args):
            self.credentials = {}

        def register(self, name):
            calls.append("register")

        def save(self):
            pass

        def request(self, action, body):
            calls.append(action)
            raise KeyboardInterrupt

    def check(*args):
        nonlocal checks
        checks += 1
        if checks == 1:
            raise DiskCapacityError("temporary low free space")

    monkeypatch.setattr(worker, "Client", Coordinator)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: calls.append(seconds))
    budget = DiskBudget(tmp_path, 2**30, 0)
    monkeypatch.setattr(budget, "check", check)
    args = parser().parse_args(["worker", "--continuous", "--directory", str(tmp_path)])
    with pytest.raises(KeyboardInterrupt):
        worker.work(args, budget)
    assert calls == ["register", 300, "claim"]


def test_lost_ack_retries_identical_evidence_after_pixels_are_removed(tmp_path):
    from .test_retention import completed_job

    job = completed_job(tmp_path, acknowledged=False)
    payloads = []

    class Coordinator:
        def lease_body(self, **extra):
            return extra

        def request(self, action, body):
            assert not list(job.rglob("*.npy"))
            payloads.append(body)
            if len(payloads) == 1:
                raise requests.Timeout("lost response")
            return {"accepted": True, "verified": False}

        def clear_claim(self):
            pass

    client = Coordinator()
    with pytest.raises(requests.Timeout):
        worker.submit(client, job)
    worker.submit(client, job)
    assert payloads[0] == payloads[1]
    assert read_json(job / "submission.json")["verified"] is False


@pytest.mark.parametrize(
    "code,recoverable,delay", [(73, True, 300), (69, True, 60), (75, False, None), (1, False, None)]
)
def test_mid_search_recovery_is_limited_to_capacity_and_transport(
    tmp_path, monkeypatch, capsys, code, recoverable, delay
):
    import hashlib

    from p9search.cli import parser
    from p9search.storage import canonical

    from .test_retention import completed_job

    actions, releases, waits = [], [], []
    spec = specification(label="field 0")

    class Coordinator:
        def __init__(self, *args):
            self.credentials = {}

        def register(self, name):
            actions.append("register")

        def save(self):
            pass

        def clear_claim(self):
            self.credentials.pop("task_id", None)
            self.credentials.pop("lease_token", None)

        def lease_body(self, **extra):
            return extra

        def request(self, action, body):
            actions.append(action)
            if action == "claim":
                if actions.count("claim") == 2:
                    assert not (tmp_path / spec["task_id"]).exists()
                    raise KeyboardInterrupt
                return {
                    "task": spec,
                    "task_id": spec["task_id"],
                    "lease_seconds": 1800,
                    "spec_sha256": hashlib.sha256(canonical(spec).encode()).hexdigest(),
                }
            if action == "release":
                if recoverable:
                    assert not list(tmp_path.rglob("*.npy"))
                releases.append(body)
                if recoverable and len(releases) == 1:
                    raise requests.Timeout("release acknowledgement unavailable")
                return {"released": True}
            pytest.fail("An interrupted search must never submit")

    class Child:
        returncode = code

        def __init__(self, *args, **kwargs):
            job = completed_job(tmp_path, acknowledged=False)
            (job / "result.json").unlink()

        def poll(self):
            return self.returncode

    monkeypatch.setattr(worker, "Client", Coordinator)
    monkeypatch.setattr(worker.subprocess, "Popen", Child)
    monkeypatch.setattr(worker.time, "sleep", waits.append)
    args = parser().parse_args(["worker", "--continuous", "--directory", str(tmp_path)])
    with pytest.raises(KeyboardInterrupt if recoverable else RuntimeError):
        worker.work(args, DiskBudget(tmp_path, 2**30, 0))
    assert actions.count("register") == 1
    assert releases[0]["failed"] is (not recoverable)
    assert waits == ([60, delay] if recoverable else [])
    assert capsys.readouterr().out == ""
    if not recoverable:
        assert list(tmp_path.rglob("*.npy"))
        assert read_json(tmp_path / "worker-state.json")["state"] == "held"


def test_approval_wait_preserves_identity_and_retries_quietly(tmp_path, monkeypatch, capsys):
    from p9search.cli import parser

    calls, waits, tokens = [], [], []

    class Coordinator:
        def __init__(self, *args):
            self.credentials = {}

        def register(self, name):
            calls.append("register")

        def save(self):
            pass

        def request(self, action, body):
            calls.append(action)
            tokens.append(body["lease_token"])
            if len(tokens) < 4:
                raise worker.CoordinatorError(403, "Worker needs operator approval")
            raise KeyboardInterrupt

    monkeypatch.setattr(worker, "Client", Coordinator)
    times = iter([0, 599, 600])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(worker.time, "sleep", waits.append)
    args = parser().parse_args(["worker", "--continuous", "--directory", str(tmp_path)])
    with pytest.raises(KeyboardInterrupt):
        worker.work(args, DiskBudget(tmp_path, 2**30, 0))
    assert calls.count("register") == 1 and len(set(tokens)) == 1
    assert waits == [15, 15, 300]
    assert not capsys.readouterr().out


@pytest.mark.parametrize("number,expected", [(28, 73), (13, 1)])
def test_actual_disk_write_errors_are_classified_without_retrying_permissions(
    tmp_path, monkeypatch, number, expected
):
    import errno

    from p9search import cli

    # ENOSPC and EACCES have these portable POSIX values on supported platforms.
    error_number = errno.ENOSPC if number == 28 else errno.EACCES

    def fail(*args, **kwargs):
        raise OSError(error_number, "test disk failure")

    monkeypatch.setattr(cli.DiskBudget, "check", fail)
    assert cli.main(["run", str(tmp_path / "task.json"), "--directory", str(tmp_path)]) == expected
