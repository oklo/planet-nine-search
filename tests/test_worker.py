import subprocess
import sys
from types import SimpleNamespace

import pytest
import requests

from p9search import worker
from p9search.storage import DirectoryLock, DiskBudget, read_json, write_json

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
    write_json(tmp_path / "result.json", {"task_id": "example", "state": "searched"})

    class LostReply:
        def lease_body(self, **extra):
            return {"lease_token": "private", **extra}

        def request(self, action, body):
            assert action == "submit"
            raise requests.Timeout("acknowledgement lost")

        def clear_claim(self):
            raise AssertionError("An unacknowledged claim must remain retryable")

    with pytest.raises(requests.Timeout):
        worker.submit(LostReply(), tmp_path)
    assert read_json(tmp_path / "result.json") == {"task_id": "example", "state": "searched"}
    assert not (tmp_path / "submission.json").exists()


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
    assert not list(work.iterdir())


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

    class Coordinator:
        def __init__(self, *args):
            self.credentials = {}

        def register(self, name):
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
    assert len(list(tmp_path.glob("*/submission.json"))) == 3
    assert completed_child.exists()
    assert budget.used() < 60000
    assert waits == [60, 60, 300]


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
