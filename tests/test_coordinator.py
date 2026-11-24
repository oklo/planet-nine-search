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

        def admin(*args, database_path=None, fence=""):
            return subprocess.run(
                [php, str(source / "admin.php"), *map(str, args)],
                env={
                    **environment,
                    "P9_DATABASE": str(database_path or database),
                    "P9_OPERATOR_FENCE": fence,
                },
                capture_output=True,
                text=True,
            )
    else:

        def admin(*args, database_path=None, fence=""):
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
                    "-e",
                    "P9_OPERATOR_FENCE=" + fence,
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
    assert response.json()["approval"] == "approved"
    if not approved:
        service[1]("UPDATE workers SET status='pending' WHERE id=?", [response.json()["worker_id"]])
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


def test_data_failure_quarantine_is_per_worker(coordinator):
    task = seed(coordinator)
    first = worker(coordinator)
    assert claim(coordinator, first).json()["task"] == task
    body = dict(task_id=task["task_id"], lease_token=first[1], failed=True, field_data_failure=True)
    reply = call(coordinator, "release", body, first[0]).json()
    assert reply["quarantined_for_worker"] and not reply["worker_held"]
    assert claim(coordinator, first).json()["task"] is None
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


def worker_id_for(service, pair):
    import hashlib

    return service[1](
        "SELECT id FROM workers WHERE token_hash=?", [hashlib.sha256(pair[0].encode()).hexdigest()]
    )[0][0]


def record_claims(service, pair, count):
    worker_id = worker_id_for(service, pair)
    for _ in range(count):
        service[1](
            "INSERT INTO events(time,action,worker_id,task_id,detail) VALUES(?,?,?,?,?)",
            [int(time.time()), "claim", worker_id, None, "test quota history"],
        )
    return worker_id


def test_claim_history_and_legacy_quotas_do_not_throttle_workers(coordinator):
    task = seed(coordinator)
    pair = worker(coordinator)
    identity = record_claims(coordinator, pair, 100)
    for key, value in [("claim_limit:", "1"), ("reserve:", "100")]:
        coordinator[1]("INSERT INTO settings VALUES(?,?)", [key + identity, value])
    assert claim(coordinator, pair).json()["task"] == task


def test_worker_continues_through_a_review_backlog(coordinator):
    for number in range(1, 13):
        seed(coordinator, number)
    pair = worker(coordinator)
    for _ in range(11):
        submission(coordinator, pair)
    assert claim(coordinator, pair).json()["task"] is not None
    assert coordinator[1]("SELECT COUNT(*),SUM(verified) FROM results")[0] == (11, 0)


def submission(service, pair):
    claimed = claim(service, pair).json()
    body = {
        "task_id": claimed["task_id"],
        "lease_token": pair[1],
        "result": {**claimed["task"], "spec_sha256": claimed["spec_sha256"], "state": "searched"},
    }
    assert call(service, "submit", body, pair[0]).status_code == 200
    return body


def test_explicit_holds_remain_but_submission_allows_the_next_field(coordinator):
    for i in range(5):
        seed(coordinator, i + 1)
    held = worker(coordinator, approved=False)
    assert claim(coordinator, held).status_code == 403
    participant = worker(coordinator)
    for _ in range(5):
        submission(coordinator, participant)
    assert coordinator[1]("SELECT COUNT(*) FROM tasks WHERE state='submitted'")[0][0] == 5
    assert coordinator[1]("SELECT SUM(verified) FROM results")[0][0] == 0


def test_review_delay_preserves_results_and_worker_access(coordinator):
    first = seed(coordinator)
    pair = worker(coordinator)
    submission(coordinator, pair)
    assert coordinator[1]("SELECT expires FROM tasks")[0][0] is None
    # Expiry values left by the old policy must also stop affecting submissions.
    coordinator[1]("UPDATE tasks SET expires=0")
    assert coordinator[2]("expire").returncode == 0
    assert coordinator[1]("SELECT state FROM tasks")[0][0] == "submitted"
    assert coordinator[1]("SELECT status,verified FROM results")[0] == ("pending", 0)
    second = seed(coordinator, 2)
    assert claim(coordinator, pair).json()["task"] == second
    assert first["task_id"] != second["task_id"]


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


def adaptive_submission(service, pair, number, *, source="a" * 64, candidate=False):
    seed(service, number)
    claimed = claim(service, pair).json()
    report = {
        **claimed["task"],
        "spec_sha256": claimed["spec_sha256"],
        "state": "needs_followup" if candidate else "searched",
        "candidate_count": int(candidate),
        "source_sha256": source,
        "device": "cpu",
        "dependencies": {"numpy": "2"},
        "launch": {"source_sha256": source, "platform": "test", "python": "3.12"},
    }
    response = call(
        service,
        "submit",
        {"task_id": report["task_id"], "lease_token": pair[1], "result": report},
        pair[0],
    )
    assert response.status_code == 200, response.text
    keypath = service[3] / "review.key"
    if not keypath.exists():
        keypath.write_text(secrets.token_hex(32))
        keypath.chmod(0o600)
    decision = service[2]("review-plan", report["task_id"])
    assert decision.returncode == 0, decision.stderr
    from p9search.review import verify_signed

    signed = json.loads(decision.stdout)
    plan = verify_signed(signed, bytes.fromhex(keypath.read_text()))
    return signed, plan


def adaptive_receipt(service, signed, plan, *, method=None, disposition="accepted"):
    from p9search.review import signed_receipt
    from p9search.storage import write_json

    method = method or plan["mode"]
    payload = {k: plan[k] for k in ("task_id", "worker_id", "spec_sha256", "submission_digest")}
    payload.update(
        contract="statistical-review-v1",
        disposition=disposition,
        review_method=method,
        plan_signature=signed["signature"],
        audit_probability=plan["audit_probability"],
        sensitivity_credit=0,
        full_replay_verified=method == "full_replay" and disposition == "accepted",
    )
    path = service[3] / "adaptive-receipt.json"
    write_json(
        path, signed_receipt(payload, bytes.fromhex((service[3] / "review.key").read_text()))
    )
    return service[2]("accept", path)


def test_new_and_established_workers_get_stable_consistency_plans(coordinator):
    pair = worker(coordinator)
    for n in range(1, 6):
        signed, plan = adaptive_submission(
            coordinator, pair, n, source=("a" if n < 5 else "b") * 64
        )
        assert plan["mode"] == "consistency"
        assert plan["audit_probability"] == 0
        assert json.loads(coordinator[2]("review-plan", plan["task_id"]).stdout) == signed
        assert adaptive_receipt(coordinator, signed, plan, method="statistical").returncode != 0
        response = adaptive_receipt(coordinator, signed, plan)
        assert response.returncode == 0, response.stderr


def test_candidate_flags_do_not_automatically_trigger_replication(coordinator):
    pair = worker(coordinator)
    identity = worker_id_for(coordinator, pair)
    assert call(coordinator, "trust-local", {"worker_id": identity}, pair[0]).status_code != 200
    signed, plan = adaptive_submission(coordinator, pair, 1, candidate=True)
    assert plan["mode"] == "consistency" and plan["audit_probability"] == 0
    assert adaptive_receipt(coordinator, signed, plan).returncode == 0
    assert coordinator[1]("SELECT status FROM workers")[0][0] == "approved"


def test_receipt_cannot_bind_another_plan_and_rotation_removes_local_trust(coordinator):
    pair = worker(coordinator)
    identity = worker_id_for(coordinator, pair)
    assert coordinator[2]("trust-local", identity, "on").returncode == 0
    signed, plan = adaptive_submission(coordinator, pair, 1)
    changed = {**signed, "signature": "0" * 64}
    assert adaptive_receipt(coordinator, changed, plan).returncode != 0
    token = coordinator[3] / "new-token"
    token.write_text(secrets.token_hex(32))
    assert coordinator[2]("rotate", identity, token).returncode == 0
    assert (
        coordinator[1]("SELECT value FROM settings WHERE key=?", ["trusted_local:" + identity])[0][
            0
        ]
        == "0"
    )


def test_failed_audit_holds_unchecked_history_but_preserves_full_replays(coordinator):
    from p9search.review import signed_receipt
    from p9search.storage import canonical

    pair = worker(coordinator)
    signed, plan = adaptive_submission(coordinator, pair, 1)
    key = bytes.fromhex((coordinator[3] / "review.key").read_text())
    for number, method in [(100, "statistical"), (101, "full_replay")]:
        task = seed(coordinator, number)
        coordinator[1]("UPDATE tasks SET state='reviewed' WHERE id=?", [task["task_id"]])
        receipt = signed_receipt(
            {"contract": "statistical-review-v1", "review_method": method}, key
        )
        coordinator[1](
            "INSERT INTO results(task_id,worker_id,submitted,digest,result,status,verified,receipt) VALUES(?,?,0,'old','{}','accepted',1,?)",
            [task["task_id"], plan["worker_id"], canonical(receipt)],
        )
        identity = coordinator[1]("SELECT id FROM results WHERE task_id=?", [task["task_id"]])[0][0]
        coordinator[1](
            "INSERT INTO settings VALUES(?,?)", [f"review_plan:{identity}", canonical(signed)]
        )
    assert adaptive_receipt(coordinator, signed, plan, disposition="disagrees").returncode == 0
    assert coordinator[1]("SELECT status,verified FROM results WHERE task_id=?", [f"{100:064x}"])[
        0
    ] == ("audit_hold", 0)
    assert coordinator[1]("SELECT status,verified FROM results WHERE task_id=?", [f"{101:064x}"])[
        0
    ] == ("accepted", 1)
    assert (
        coordinator[1]("SELECT state FROM tasks WHERE id=?", [f"{100:064x}"])[0][0] == "audit_hold"
    )


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


def test_review_backlog_preserves_one_active_lease_and_exports_oldest_first(coordinator):
    for number in range(1, 5):
        seed(coordinator, number)
    pair = worker(coordinator)
    first = submission(coordinator, pair)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(lambda _: claim(coordinator, pair), range(4)))
    assert all(r.status_code == 200 for r in claims)
    assert len({r.json()["task_id"] for r in claims}) == 1
    assert coordinator[1]("SELECT COUNT(*) FROM tasks WHERE state='leased'")[0][0] == 1
    second = submission(coordinator, pair)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        blocked = list(executor.map(lambda _: claim(coordinator, pair), range(4)))
    assert all(r.json()["task"] is not None for r in blocked)
    assert len({r.json()["task_id"] for r in blocked}) == 1
    assert coordinator[1]("SELECT COUNT(*),SUM(verified) FROM results")[0] == (2, 0)
    # Equal timestamps require a deterministic result-id tiebreaker.
    coordinator[1]("UPDATE results SET submitted=1234")
    exported = coordinator[2]("results")
    assert exported.returncode == 0, exported.stderr
    rows = [json.loads(line) for line in exported.stdout.splitlines()]
    assert [r["task_id"] for r in rows] == [first["task_id"], second["task_id"]]
    assert all(type(r["submitted"]) is int and type(r["result_id"]) is int for r in rows)
    assert rows[0]["result_id"] < rows[1]["result_id"]
    assert claim(coordinator, worker(coordinator)).json()["task"] is not None
    # Simulate the independent reviewer freeing one slot; no worker API accepts it.
    coordinator[1](
        "UPDATE results SET status='accepted',verified=1 WHERE task_id=?", [first["task_id"]]
    )
    assert claim(coordinator, pair).json()["task"] is not None


def test_empty_supply_is_explicit_and_same_worker_can_claim_after_replenishment(coordinator):
    pair = worker(coordinator)
    status = requests.get(coordinator[0], params={"action": "status"}, timeout=10)
    assert status.status_code == 200
    assert status.json()["tasks"]["pending"] == 0
    assert status.json()["tasks"]["leased"] == 0
    reply = claim(coordinator, pair)
    assert reply.json() == {
        "task": None,
        "retry_after": 300,
        "reason": "Waiting for coordinator replenishment",
    }
    task = seed(coordinator, 999)
    assert claim(coordinator, pair).json()["task_id"] == task["task_id"]
    assert coordinator[1]("SELECT COUNT(*) FROM workers")[0][0] == 1


def test_insufficient_worker_allocation_has_a_distinct_wait_reason(coordinator):
    seed(coordinator, 1000)
    assert claim(coordinator, worker(coordinator), budget=1).json() == {
        "task": None,
        "retry_after": 300,
        "reason": "No ready field fits this worker's disk allocation",
    }


@pytest.mark.parametrize("held_status", ["pending", "revoked"])
def test_registration_retry_does_not_release_an_existing_hold(coordinator, held_status):
    token = secrets.token_hex(32)
    body = {"name": "new participant", "token": token}
    first = call(coordinator, "register", body).json()
    assert first["approval"] == "approved"
    coordinator[1](
        "UPDATE workers SET status=?,strikes=3 WHERE id=?", [held_status, first["worker_id"]]
    )
    retry = call(coordinator, "register", body)
    assert retry.status_code == 200
    assert retry.json() == {"worker_id": first["worker_id"], "approval": held_status}
    assert claim(coordinator, (token, secrets.token_hex(32))).status_code == 403
    assert coordinator[1]("SELECT COUNT(*),strikes FROM workers")[0] == (1, 3)


def test_registration_is_automatic_and_idempotent_without_an_address_quota(coordinator):
    bodies = [{"name": f"participant {i}", "token": secrets.token_hex(32)} for i in range(12)]
    identities = []
    for body in bodies:
        response = call(coordinator, "register", body)
        assert response.status_code == 200
        assert response.json()["approval"] == "approved"
        identities.append(response.json()["worker_id"])
    assert call(coordinator, "register", bodies[0]).json()["worker_id"] == identities[0]
    assert coordinator[1]("SELECT COUNT(*) FROM workers")[0][0] == 12


def test_repeated_expired_leases_do_not_require_registration_again(coordinator):
    seed(coordinator)
    pair = worker(coordinator)
    for _ in range(4):
        assert claim(coordinator, pair).json()["task"] is not None
        coordinator[1]("UPDATE tasks SET expires=0")
    assert claim(coordinator, pair).json()["task"] is not None
    assert coordinator[1]("SELECT status,strikes FROM workers")[0] == ("approved", 4)


def test_operator_handover_checks_checkpoint_and_fences_prior_owner(coordinator):
    admin = coordinator[2]
    current = json.loads(admin("operator-status").stdout)["checkpoint"]
    one, two = secrets.token_hex(32), secrets.token_hex(32)
    args = [str(current["last_review"]), current["snapshot"] or "-"]
    assert admin("operator-claim", "travel", *args, fence=one).returncode == 0
    assert admin("operator-claim", "home", *args, fence=two).returncode
    assert admin("operator-release", fence=one).returncode == 0
    assert admin("operator-claim", "home", "999", args[1], fence=two).returncode
    assert admin("operator-claim", "home", *args, fence=two).returncode == 0
    assert admin("operator-renew", fence=one).returncode
    assert admin("operator-renew", fence=two).returncode == 0
    assert "Current operator lease required" in admin("review-plan", "missing", fence=one).stderr
