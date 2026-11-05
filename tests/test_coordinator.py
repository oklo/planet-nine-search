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
    import os

    php = shutil.which("php")
    docker = shutil.which("docker") if os.getenv("P9_TEST_DOCKER") == "1" else None
    if not php and not docker:
        pytest.skip("PHP with pdo_sqlite, or P9_TEST_DOCKER=1 with cached php:8.2-cli, required")
    source = Path(__file__).resolve().parents[1] / "coordinator"
    database = tmp_path / "queue.sqlite"
    environment = dict(os.environ, P9_DATABASE=str(database), PHP_CLI_SERVER_WORKERS="4")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    network = None
    container = None
    server = None
    if php:

        def admin(*args, database_path=None):
            return subprocess.run(
                [php, str(source / "admin.php"), *map(str, args)],
                env={**environment, "P9_DATABASE": str(database_path or database)},
                capture_output=True,
                text=True,
            )
    else:

        def admin(*args, database_path=None):
            converted = [str(a).replace(str(tmp_path), "/state") for a in args]
            return subprocess.run(
                [
                    docker,
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--memory",
                    "256m",
                    "--cpus",
                    "1",
                    "--tmpfs",
                    "/tmp:size=32m",
                    "-v",
                    f"{source}:/app:ro",
                    "-v",
                    f"{tmp_path}:/state",
                    "-e",
                    "P9_DATABASE="
                    + str(database_path or database).replace(str(tmp_path), "/state"),
                    "-e",
                    "P9_REVIEW_KEY=/state/review.key",
                    "php:8.2-cli",
                    "php",
                    "/app/admin.php",
                    *converted,
                ],
                capture_output=True,
                text=True,
            )

    initialized = admin("init")
    assert initialized.returncode == 0, initialized.stderr

    def sql(statement, values=()):
        if php:
            with sqlite3.connect(database) as connection:
                return connection.execute(statement, values).fetchall()
        code = (
            '$db=new PDO("sqlite:/state/queue.sqlite"); $db->setAttribute(PDO::ATTR_ERRMODE,PDO::ERRMODE_EXCEPTION); '
            '$db->exec("PRAGMA busy_timeout=10000"); $args=json_decode($argv[1],true); '
            "$s=$db->prepare($args[0]); $s->execute($args[1]); echo json_encode($s->fetchAll(PDO::FETCH_NUM));"
        )
        result = subprocess.run(
            [docker, "exec", container, "php", "-r", code, json.dumps([statement, values])],
            capture_output=True,
            text=True,
            check=True,
        )
        return [tuple(row) for row in json.loads(result.stdout)]

    url = f"http://127.0.0.1:{port}/api.php"
    try:
        if php:
            environment["P9_REVIEW_KEY"] = str(tmp_path / "review.key")
            server = subprocess.Popen(
                [php, "-S", f"127.0.0.1:{port}", "-t", str(source)],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            network = "p9-test-" + secrets.token_hex(6)
            subprocess.run(
                [docker, "network", "create", network],
                check=True,
                capture_output=True,
            )
            command = [
                docker,
                "run",
                "-d",
                "--rm",
                "--read-only",
                "--network",
                network,
                "--memory",
                "256m",
                "--cpus",
                "1",
                "--tmpfs",
                "/tmp:size=32m",
                "-p",
                f"127.0.0.1:{port}:8080",
                "-v",
                f"{source}:/app:ro",
                "-v",
                f"{tmp_path}:/state",
                "-e",
                "P9_DATABASE=/state/queue.sqlite",
                "-e",
                "PHP_CLI_SERVER_WORKERS=4",
                "php:8.2-cli",
                "php",
                "-S",
                "0.0.0.0:8080",
                "-t",
                "/app",
            ]
            container = subprocess.check_output(command, text=True).strip()
        for _ in range(100):
            try:
                requests.get(url, timeout=0.1).raise_for_status()
                break
            except requests.RequestException:
                time.sleep(0.05)
        else:
            raise RuntimeError("Test PHP server did not start")
        yield url, sql, admin, tmp_path
    finally:
        if server:
            os.killpg(server.pid, signal.SIGTERM)
            server.wait(timeout=5)
        if container:
            subprocess.run([docker, "rm", "-f", container], check=True, capture_output=True)
        if network:
            subprocess.run([docker, "network", "rm", network], check=True, capture_output=True)


def call(service, action, body=None, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.post(
        service[0], params={"action": action}, json=body or {}, headers=headers, timeout=20
    )


def worker(service, approved=True):
    token, lease = secrets.token_hex(32), secrets.token_hex(32)
    response = call(service, "register", {"name": "integration test", "token": token})
    assert response.status_code == 200, response.text
    if approved:
        assert service[2]("approve", response.json()["worker_id"]).returncode == 0
    return token, lease


def seed(service, number=1, priority=1, size=1000):
    spec = {"task_id": f"{number:064x}", "protocol": "tess-gaia-hough-v2"}
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
        {"protocol": "tess-gaia-hough-v2", "available_bytes": budget, "lease_token": pair[1]},
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


def test_repeated_failures_suspend_worker_without_retiring_task(coordinator):
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
    assert claim(coordinator, pair).status_code == 403
    assert coordinator[1]("SELECT state FROM tasks")[0][0] == "pending"
    assert claim(coordinator, worker(coordinator)).json()["task"] == task


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

    worker_id = service[1](
        "SELECT id FROM workers WHERE token_hash=?", [hashlib.sha256(pair[0].encode()).hexdigest()]
    )[0][0]
    return service[2]("reserve", worker_id, count)


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


def submission(service, pair):
    claimed = claim(service, pair).json()
    body = {
        "task_id": claimed["task_id"],
        "lease_token": pair[1],
        "result": {**claimed["task"], "spec_sha256": claimed["spec_sha256"], "state": "searched"},
    }
    assert call(service, "submit", body, pair[0]).status_code == 200
    return body


def test_registration_requires_approval_and_one_worker_cannot_drain_queue(coordinator):
    for i in range(5):
        seed(coordinator, i + 1)
    pending = worker(coordinator, approved=False)
    assert claim(coordinator, pending).status_code == 403
    attacker = worker(coordinator)
    submission(coordinator, attacker)
    for _ in range(4):
        response = claim(coordinator, attacker)
        assert response.status_code == 200
        assert response.json()["task"] is None
    assert coordinator[1]("SELECT COUNT(*) FROM tasks WHERE state='pending'")[0][0] == 4
    assert coordinator[1]("SELECT SUM(verified) FROM results")[0][0] == 0


def test_review_timeout_requeues_and_requires_reapproval(coordinator):
    seed(coordinator)
    pair = worker(coordinator)
    submission(coordinator, pair)
    coordinator[1]("UPDATE tasks SET expires=0")
    assert coordinator[2]("expire").returncode == 0
    assert coordinator[1]("SELECT state FROM tasks")[0][0] == "pending"
    assert coordinator[1]("SELECT status,verified FROM results")[0] == ("expired", 0)
    assert claim(coordinator, pair).status_code == 403
    assert claim(coordinator, worker(coordinator)).json()["task"] is not None


def test_absolute_lease_cannot_be_renewed_forever(coordinator):
    task = seed(coordinator)
    pair = worker(coordinator)
    claim(coordinator, pair)
    coordinator[1]("UPDATE tasks SET started=?", [int(time.time()) - 21601])
    body = {"task_id": task["task_id"], "lease_token": pair[1]}
    assert call(coordinator, "heartbeat", body, pair[0]).status_code == 409


def test_revocation_rotation_and_requeue_preserve_history(coordinator):
    task = seed(coordinator)
    pair = worker(coordinator)
    submission(coordinator, pair)
    worker_id = coordinator[1]("SELECT id FROM workers")[0][0]
    assert coordinator[2]("revoke", worker_id).returncode == 0
    assert claim(coordinator, pair).status_code == 403
    assert coordinator[1]("SELECT state FROM tasks")[0][0] == "pending"
    assert coordinator[1]("SELECT status,verified FROM results")[0] == ("rejected", 0)
    token = secrets.token_hex(32)
    path = coordinator[3] / "replacement.token"
    path.write_text(token)
    assert coordinator[2]("rotate", worker_id, path).returncode == 0
    assert claim(coordinator, pair).status_code == 401
    assert coordinator[2]("approve", worker_id).returncode == 0
    replacement = (token, secrets.token_hex(32))
    assert claim(coordinator, replacement).json()["task"] == task
    assert coordinator[2]("requeue", task["task_id"], "operator retry").returncode == 0
    assert coordinator[1]("SELECT COUNT(*) FROM events")[0][0] >= 7


def test_enqueue_checks_full_schema_and_identity(coordinator):
    from p9search.storage import write_json

    from .test_tasks import specification

    path = coordinator[3] / "task.json"
    value = specification()
    write_json(path, value)
    response = coordinator[2]("add", path)
    assert response.returncode == 0, response.stderr
    value["label"] = "tampered"
    write_json(path, value)
    assert coordinator[2]("add", path).returncode != 0
    assert coordinator[1]("SELECT COUNT(*) FROM tasks")[0][0] == 1


def test_only_master_signed_current_replay_can_accept(coordinator):
    import hashlib

    from p9search.review import signed_receipt
    from p9search.storage import write_json

    seed(coordinator)
    pair = worker(coordinator)
    submission(coordinator, pair)
    exported = json.loads(coordinator[2]("results").stdout)
    raw_task = coordinator[1]("SELECT spec FROM tasks")[0][0]
    payload = {
        "contract": "trusted-full-replay-v1",
        "task_id": exported["task_id"],
        "worker_id": exported["worker_id"],
        "submission_digest": exported["submission_digest"],
        "spec_sha256": hashlib.sha256(raw_task.encode()).hexdigest(),
        "disposition": "accepted",
    }
    key = secrets.token_bytes(32)
    (coordinator[3] / "review.key").write_text(key.hex())
    receipt = coordinator[3] / "receipt.json"
    write_json(receipt, signed_receipt(payload, bytes(32)))
    assert coordinator[2]("accept", receipt).returncode != 0
    assert coordinator[1]("SELECT verified FROM results")[0][0] == 0
    write_json(receipt, signed_receipt({**payload, "submission_digest": "f" * 64}, key))
    assert coordinator[2]("accept", receipt).returncode != 0
    write_json(receipt, signed_receipt(payload, key))
    accepted = coordinator[2]("accept", receipt)
    assert accepted.returncode == 0, accepted.stderr
    assert coordinator[1]("SELECT state FROM tasks")[0][0] == "reviewed"
    assert coordinator[1]("SELECT verified FROM results")[0][0] == 1
    assert coordinator[2]("accept", receipt).returncode != 0


def test_backup_is_consistent_with_active_wal(coordinator):
    import sqlite3

    seed(coordinator, 1)
    seed(coordinator, 2)
    pair = worker(coordinator)
    submission(coordinator, pair)
    backup = coordinator[3] / "backup.sqlite"
    response = coordinator[2]("backup", backup)
    assert response.returncode == 0, response.stderr
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1
    assert coordinator[2]("backup", backup).returncode != 0


def test_restore_preserves_pending_leased_and_submitted_work(coordinator):
    for i in (1, 2, 3):
        seed(coordinator, i)
    submission(coordinator, worker(coordinator))
    assert claim(coordinator, worker(coordinator)).json()["task"] is not None
    backup = coordinator[3] / "snapshot.sqlite"
    restored = coordinator[3] / "restored.sqlite"
    assert coordinator[2]("backup", backup).returncode == 0
    response = coordinator[2]("restore", backup, database_path=restored)
    assert response.returncode == 0, response.stderr
    with sqlite3.connect(restored) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert sorted(row[0] for row in db.execute("SELECT state FROM tasks")) == [
            "leased",
            "pending",
            "submitted",
        ]
        assert db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1
    assert coordinator[2]("restore", backup, database_path=restored).returncode != 0


def test_v1_migration_and_rollback_backup_preserve_evidence(coordinator):
    old = coordinator[3] / "legacy.sqlite"
    backup = coordinator[3] / "before-migration.sqlite"
    restored = coordinator[3] / "rollback.sqlite"
    with sqlite3.connect(old) as db:
        db.executescript("""
          CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE workers(id TEXT PRIMARY KEY,name TEXT,token_hash TEXT,address_hash TEXT,created INTEGER);
          CREATE TABLE tasks(id TEXT PRIMARY KEY,spec TEXT,priority REAL,required_bytes INTEGER,state TEXT,worker_id TEXT,lease_hash TEXT,expires INTEGER,attempts INTEGER,failures INTEGER,last_error TEXT);
          CREATE TABLE results(task_id TEXT PRIMARY KEY,worker_id TEXT,submitted INTEGER,digest TEXT,result TEXT,verified INTEGER);
          INSERT INTO workers VALUES('worker','name','hash','address',0);
          INSERT INTO tasks VALUES('task','{"protocol":"tess-gaia-hough-v1"}',1,100,'submitted','worker','lease',9999999999,1,0,NULL);
          INSERT INTO results VALUES('task','worker',0,'digest','{"state":"searched"}',0);
        """)
    assert coordinator[2]("backup", backup, database_path=old).returncode == 0
    for _ in range(2):
        response = coordinator[2]("migrate", database_path=old)
        assert response.returncode == 0, response.stderr
    with sqlite3.connect(old) as db:
        assert db.execute("SELECT state FROM tasks").fetchone()[0] == "legacy"
        assert db.execute("SELECT result,status FROM results").fetchone() == (
            '{"state":"searched"}',
            "legacy",
        )
        assert db.execute("SELECT status FROM workers").fetchone()[0] == "pending"
        assert db.execute("SELECT COUNT(*) FROM events WHERE action='migrate'").fetchone()[0] == 1
    assert coordinator[2]("restore", backup, database_path=restored).returncode == 0
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT state FROM tasks").fetchone()[0] == "submitted"
        assert db.execute("SELECT result FROM results").fetchone()[0] == '{"state":"searched"}'


def test_submission_export_preserves_floating_point_types(coordinator):
    seed(coordinator)
    pair = worker(coordinator)
    claimed = claim(coordinator, pair).json()
    result = {
        **claimed["task"],
        "spec_sha256": claimed["spec_sha256"],
        "state": "searched",
        "parameters": [1.0, 0.0, 1e-6],
        "count": 3,
    }
    body = {"task_id": claimed["task_id"], "lease_token": pair[1], "result": result}
    assert call(coordinator, "submit", body, pair[0]).status_code == 200
    exported = json.loads(coordinator[2]("results").stdout)
    restored = json.loads(exported["result_json"])
    assert all(isinstance(v, float) for v in restored["parameters"])
    assert isinstance(restored["count"], int)


def test_full_replay_contract_rejects_an_omitted_candidate(coordinator, monkeypatch):
    import copy
    import hashlib
    from types import SimpleNamespace

    from p9search import review
    from p9search.provenance import execution_manifest
    from p9search.storage import canonical, write_json

    from .test_tasks import specification

    spec = specification()
    task = coordinator[3] / "trusted-task.json"
    write_json(task, spec)
    assert coordinator[2]("add", task).returncode == 0
    pair = worker(coordinator)
    claimed = claim(coordinator, pair, budget=2**30).json()
    launch = execution_manifest()
    honest = {
        "task_id": spec["task_id"],
        "spec_sha256": hashlib.sha256(canonical(spec).encode()).hexdigest(),
        "protocol": spec["protocol"],
        "policy": spec["policy"],
        "package_version": "0.2.0",
        "source_sha256": launch["source_sha256"],
        "launch": launch,
        "inputs": {},
        "dependencies": {},
        "quality": [],
        "state": "needs_followup",
        "candidate_count": 1,
        "candidates": [{"parameters": [1.0, 2.0, 0.0, 0.0, 0.002], "score": 10.0}],
        "injections": [],
        "elapsed_seconds": 1.0,
    }
    remote = copy.deepcopy(honest)
    remote.update(state="searched", candidate_count=0, candidates=[])
    assert (
        call(
            coordinator,
            "submit",
            {"task_id": claimed["task_id"], "lease_token": pair[1], "result": remote},
            pair[0],
        ).status_code
        == 200
    )
    exported = json.loads(coordinator[2]("results").stdout)
    submission_path = coordinator[3] / "submission.json"
    write_json(submission_path, exported)
    key = coordinator[3] / "review.key"
    key.write_text(secrets.token_hex(32))
    key.chmod(0o600)
    directory = coordinator[3] / "trusted-review"
    launched = []

    class Replay:
        def __init__(self, command, **kwargs):
            launched.append(command)
            assert command[3] == "run"
            assert json.loads(Path(command[4]).read_text()) == spec
            replay_root = Path(command[command.index("--directory") + 1])
            assert not replay_root.exists()  # No worker pixels or checkpoints can be adopted.
            write_json(replay_root / spec["task_id"] / "result.json", honest)

        def wait(self, timeout):
            return 0

        def poll(self):
            return 0

    args = SimpleNamespace(
        task=task,
        submission=submission_path,
        key=key,
        directory=directory,
        max_disk_gb=2,
        reserve_gb=0,
        max_memory_gb=1,
        max_hours=1,
        device="cpu",
        threads=1,
        bundle_origin=["https://oklo.org"],
    )
    with monkeypatch.context() as patch:
        patch.setattr(review.subprocess, "Popen", Replay)
        review.review(args)
    assert len(launched) == 1
    receipt = directory / "receipt.json"
    assert coordinator[2]("accept", receipt).returncode == 0
    assert coordinator[1]("SELECT state FROM tasks")[0][0] == "pending"
    assert coordinator[1]("SELECT status,verified FROM results")[0] == ("rejected", 0)
    assert coordinator[1]("SELECT status FROM workers")[0][0] == "pending"
