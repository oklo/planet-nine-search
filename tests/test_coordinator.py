"""Exercise the real PHP/SQLite service, including concurrent and stale claims."""

import concurrent.futures
import json
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
import requests


@pytest.fixture
def coordinator(tmp_path):
    php = shutil.which("php")
    if not php:
        pytest.skip("PHP with pdo_sqlite is required for coordinator integration tests")
    import os

    source = Path(__file__).resolve().parents[1] / "coordinator"
    database = tmp_path / "queue.sqlite"
    environment = dict(os.environ, P9_DATABASE=str(database), PHP_CLI_SERVER_WORKERS="4")
    subprocess.run(
        [php, str(source / "admin.php"), "init"], env=environment, check=True, capture_output=True
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen(
        [php, "-S", f"127.0.0.1:{port}", "-t", str(source)],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def sql(statement, values=()):
        with sqlite3.connect(database) as connection:
            return connection.execute(statement, values).fetchall()

    url = f"http://127.0.0.1:{port}/api.php"
    try:
        for _ in range(100):
            try:
                requests.get(url, timeout=0.1).raise_for_status()
                break
            except requests.RequestException:
                time.sleep(0.05)
        else:
            raise RuntimeError("Test PHP server did not start")
        yield url, sql
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=5)


def call(service, action, body=None, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.post(
        service[0], params={"action": action}, json=body or {}, headers=headers, timeout=20
    )


def worker(service):
    token, lease = secrets.token_hex(32), secrets.token_hex(32)
    response = call(service, "register", {"name": "integration test", "token": token})
    assert response.status_code == 200, response.text
    return token, lease


def seed(service, number=1, priority=1, size=1000):
    spec = {"task_id": f"{number:064x}", "protocol": "tess-gaia-hough-v1"}
    raw = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    service[1](
        "INSERT INTO tasks(id,spec,priority,required_bytes) VALUES(?,?,?,?)",
        [spec["task_id"], raw, priority, size],
    )
    return spec


def claim(service, pair, budget=1000):
    return call(
        service,
        "claim",
        {"protocol": "tess-gaia-hough-v1", "available_bytes": budget, "lease_token": pair[1]},
        pair[0],
    )


def test_simultaneous_workers_cannot_own_the_same_field(coordinator):
    seed(coordinator)
    workers = [worker(coordinator) for _ in range(6)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        responses = list(executor.map(lambda pair: claim(coordinator, pair), workers))
    assert all(r.status_code == 200 for r in responses)
    assert sum(r.json()["task"] is not None for r in responses) == 1
    assert coordinator[1]("SELECT COUNT(*) FROM tasks WHERE state='leased'")[0][0] == 1


def test_expired_owner_cannot_renew_or_submit_after_reclaim(coordinator):
    task = seed(coordinator)
    first, second = worker(coordinator), worker(coordinator)
    before = claim(coordinator, first).json()
    coordinator[1]("UPDATE tasks SET expires=0")
    assert claim(coordinator, second).json()["task"] == task
    body = {"task_id": task["task_id"], "lease_token": first[1]}
    assert call(coordinator, "heartbeat", body, first[0]).status_code == 409
    assert call(coordinator, "release", body, first[0]).status_code == 409
    assert (
        call(
            coordinator,
            "submit",
            {**body, "result": {"spec_sha256": before["spec_sha256"]}},
            first[0],
        ).status_code
        == 409
    )


def test_claim_and_submission_retries_are_idempotent(coordinator):
    task = seed(coordinator)
    pair = worker(coordinator)
    first, repeated = claim(coordinator, pair).json(), claim(coordinator, pair).json()
    assert first == repeated
    assert coordinator[1]("SELECT attempts FROM tasks")[0][0] == 1
    body = {
        "task_id": task["task_id"],
        "lease_token": pair[1],
        "result": {**task, "spec_sha256": first["spec_sha256"], "state": "searched"},
    }
    for _ in range(2):
        response = call(coordinator, "submit", body, pair[0])
        assert response.status_code == 200, response.text
        assert response.json() == {"accepted": True, "verified": False}
    assert coordinator[1]("SELECT COUNT(*),verified FROM results")[0] == (1, 0)
    body["result"]["state"] = "needs_followup"
    assert call(coordinator, "submit", body, pair[0]).status_code == 409


def test_priority_budget_authentication_and_release(coordinator):
    small = seed(coordinator, 1, priority=1, size=100)
    big = seed(coordinator, 2, priority=2, size=10000)
    pair = worker(coordinator)
    assert call(coordinator, "claim", {}).status_code == 401
    assert claim(coordinator, pair, budget=1).json()["task"] is None
    assert claim(coordinator, pair, budget=100).json()["task"] == small
    body = {"task_id": small["task_id"], "lease_token": pair[1]}
    assert call(coordinator, "heartbeat", body, pair[0]).json()["lease_seconds"] == 1800
    assert call(coordinator, "release", body, pair[0]).json()["released"]
    assert claim(coordinator, pair, budget=10000).json()["task"] == big
    assert (
        call(
            coordinator,
            "submit",
            {"task_id": big["task_id"], "lease_token": pair[1], "result": {}},
            pair[0],
        ).status_code
        == 400
    )


def test_repeated_failed_work_is_quarantined(coordinator):
    task = seed(coordinator)
    pair = worker(coordinator)
    for _ in range(3):
        assert claim(coordinator, pair).json()["task"] == task
        body = {
            "task_id": task["task_id"],
            "lease_token": pair[1],
            "failed": True,
            "reason": "test failure",
        }
        assert call(coordinator, "release", body, pair[0]).status_code == 200
    assert claim(coordinator, pair).json()["task"] is None
    assert coordinator[1]("SELECT state FROM tasks")[0][0] == "failed"


def test_voluntary_releases_do_not_exhaust_failure_budget(coordinator):
    task = seed(coordinator)
    pair = worker(coordinator)
    body = {"task_id": task["task_id"], "lease_token": pair[1]}
    for _ in range(3):
        assert claim(coordinator, pair).json()["task"] == task
        assert call(coordinator, "release", body, pair[0]).status_code == 200
    assert claim(coordinator, pair).json()["task"] == task
    assert call(coordinator, "release", {**body, "failed": True}, pair[0]).status_code == 200
    assert coordinator[1]("SELECT state,failures FROM tasks")[0] == ("pending", 1)


def reserve(service, pair, count):
    import hashlib
    import os

    worker_id = service[1](
        "SELECT id FROM workers WHERE token_hash=?", [hashlib.sha256(pair[0].encode()).hexdigest()]
    )[0][0]
    database = service[1]("PRAGMA database_list")[0][2]
    admin = Path(__file__).resolve().parents[1] / "coordinator/admin.php"
    return subprocess.run(
        [shutil.which("php"), str(admin), "reserve", worker_id, str(count)],
        env=dict(os.environ, P9_DATABASE=database),
        capture_output=True,
        text=True,
    )


def test_operator_reserve_preserves_visitors_and_existing_leases(coordinator):
    operator, visitor = worker(coordinator), worker(coordinator)
    assert reserve(coordinator, operator, 1).returncode == 0
    assert reserve(coordinator, operator, -1).returncode != 0
    first = seed(coordinator, 1, priority=2)
    assert claim(coordinator, operator).json()["task"] is None
    seed(coordinator, 2)
    assert claim(coordinator, operator).json()["task"] == first
    # A retry of an existing lease still succeeds when the pool is at its floor.
    assert claim(coordinator, operator).json()["task"] == first
    assert claim(coordinator, visitor).json()["task"] is not None
    coordinator[1]("UPDATE tasks SET expires=0")
    assert claim(coordinator, operator).json()["task"] == first
    body = {"task_id": first["task_id"], "lease_token": operator[1]}
    assert call(coordinator, "release", body, operator[0]).status_code == 200
    coordinator[1]("DELETE FROM tasks WHERE id<>?", [first["task_id"]])
    assert claim(coordinator, operator).json()["task"] is None
    assert reserve(coordinator, operator, 0).returncode == 0
    assert claim(coordinator, operator).json()["task"] == first


def test_concurrent_operator_claims_cannot_drain_the_reserve(coordinator):
    operators = [worker(coordinator) for _ in range(3)]
    for pair in operators:
        assert reserve(coordinator, pair, 4).returncode == 0
    for number in range(1, 6):
        seed(coordinator, number)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        responses = list(executor.map(lambda pair: claim(coordinator, pair), operators))
    assert all(r.status_code == 200 for r in responses)
    assert sum(r.json()["task"] is not None for r in responses) == 1
    assert coordinator[1]("SELECT COUNT(*) FROM tasks WHERE state='pending'")[0][0] == 4
