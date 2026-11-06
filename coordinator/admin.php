<?php
declare(strict_types=1);
if (PHP_SAPI !== "cli") {
    http_response_code(404);
    exit();
}
require __DIR__ . "/common.php";
require __DIR__ . "/task.php";
umask(0077);
$action = $argv[1] ?? "";
$result_schema = "CREATE TABLE results(id INTEGER PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),worker_id TEXT NOT NULL REFERENCES workers(id),submitted INTEGER NOT NULL,digest TEXT NOT NULL,result TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',verified INTEGER NOT NULL DEFAULT 0,receipt TEXT);
    CREATE UNIQUE INDEX one_pending_result ON results(task_id) WHERE status='pending';
    CREATE INDEX worker_results ON results(worker_id,status);
    CREATE TABLE events(id INTEGER PRIMARY KEY,time INTEGER NOT NULL,action TEXT NOT NULL,worker_id TEXT,task_id TEXT,detail TEXT NOT NULL);
    CREATE INDEX worker_events ON events(worker_id,action,time);";
if ($action === "init") {
    $path = database_path();
    if (file_exists($path)) {
        throw new RuntimeException("Database already exists");
    }
    if (!is_dir(dirname($path))) {
        mkdir(dirname($path), 0700, true);
    }
    $db = new PDO("sqlite:" . $path, null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    ]);
    $db->exec(
        "PRAGMA journal_mode=WAL;
        CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE workers(id TEXT PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,address_hash TEXT NOT NULL,created INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',strikes INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE tasks(id TEXT PRIMARY KEY,spec TEXT NOT NULL,priority REAL NOT NULL,required_bytes INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'pending',worker_id TEXT REFERENCES workers(id),lease_hash TEXT,expires INTEGER,started INTEGER,attempts INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,last_error TEXT);
        CREATE UNIQUE INDEX one_active_claim ON tasks(worker_id) WHERE state='leased';
        CREATE INDEX pending_tasks ON tasks(state,priority);" . $result_schema,
    );
    query($db, "INSERT INTO settings VALUES(?,?)", [
        "ip_salt",
        bin2hex(random_bytes(32)),
    ]);
    query($db, "INSERT INTO settings VALUES(?,?)", ["schema", "2"]);
    echo "Initialized\n";
} elseif ($action === "migrate") {
    if (!is_file(database_path())) {
        throw new RuntimeException("Missing database");
    }
    $db = new PDO("sqlite:" . database_path(), null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    ]);
    if (
        query(
            $db,
            "SELECT value FROM settings WHERE key='schema'",
        )->fetchColumn() === "2"
    ) {
        echo "Already current\n";
        exit();
    }
    transaction($db, function () use ($db, $result_schema) {
        $db->exec(
            "ALTER TABLE workers ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
            ALTER TABLE workers ADD COLUMN strikes INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE tasks ADD COLUMN started INTEGER;
            ALTER TABLE results RENAME TO legacy_results;" . $result_schema,
        );
        $db->exec("INSERT INTO results(task_id,worker_id,submitted,digest,result,status,verified) SELECT task_id,worker_id,submitted,digest,result,'legacy',verified FROM legacy_results;
            DROP TABLE legacy_results;
            UPDATE tasks SET state='legacy',expires=NULL;
            INSERT INTO settings VALUES('schema','2');");
        event($db, "migrate");
    });
    echo "Migrated; v1 tasks retained as legacy. Reapprove workers and publish v2 tasks.\n";
} elseif ($action === "restore") {
    $source = $argv[2] ?? "";
    if (file_exists(database_path()) || !is_file($source)) {
        throw new RuntimeException(
            "Restore requires an existing backup and an unused destination",
        );
    }
    $backup = new PDO("sqlite:" . $source, null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    ]);
    if ($backup->query("PRAGMA integrity_check")->fetchColumn() !== "ok") {
        throw new RuntimeException("Backup integrity check failed");
    }
    if (!is_dir(dirname(database_path()))) {
        mkdir(dirname(database_path()), 0700, true);
    }
    if (!copy($source, database_path())) {
        throw new RuntimeException("Restore failed");
    }
    chmod(database_path(), 0600);
    echo "Restored\n";
} elseif ($action === "backup") {
    if (!is_file(database_path())) {
        throw new RuntimeException("Missing database");
    }
    $db = new PDO("sqlite:" . database_path(), null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    ]);
    $target = $argv[2] ?? "";
    if ($target === "" || file_exists($target)) {
        throw new RuntimeException("Unused private backup path required");
    }
    $db->exec("VACUUM INTO " . $db->quote($target));
    chmod($target, 0600);
    echo "Backed up\n";
} else {
    $db = database();
    transaction($db, function () use ($db, $action, $argv) {
        if ($action === "add") {
            $path = $argv[2] ?? "";
            if (!is_file($path) || filesize($path) > 1000000) {
                throw new RuntimeException("Invalid task file");
            }
            $raw = trim(file_get_contents($path));
            $spec = validate_spec($raw);
            $priority = filter_var($argv[3] ?? "0", FILTER_VALIDATE_FLOAT);
            if ($priority === false || !is_finite($priority)) {
                throw new RuntimeException("Finite priority required");
            }
            query(
                $db,
                "INSERT INTO tasks(id,spec,priority,required_bytes) VALUES(?,?,?,?)",
                [$spec["task_id"], $raw, $priority, $spec["required_bytes"]],
            );
            event($db, "add", null, $spec["task_id"]);
            echo "Queued {$spec["task_id"]}\n";
        } elseif (
            in_array($action, ["approve", "revoke", "rotate", "reserve", "claim-limit"], true)
        ) {
            $id = $argv[2] ?? "";
            $audit_detail = "";
            if (
                !query($db, "SELECT id FROM workers WHERE id=?", [
                    $id,
                ])->fetchColumn()
            ) {
                throw new RuntimeException("Unknown worker");
            }
            if ($action === "reserve") {
                $count = filter_var($argv[3] ?? "", FILTER_VALIDATE_INT, [
                    "options" => ["min_range" => 0, "max_range" => 10000],
                ]);
                if ($count === false) {
                    throw new RuntimeException("Reserve must be 0..10000");
                }
                query(
                    $db,
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ["reserve:" . $id, (string) $count],
                );
            } elseif ($action === "claim-limit") {
                $count = filter_var($argv[3] ?? "", FILTER_VALIDATE_INT, [
                    "options" => ["min_range" => 1, "max_range" => 96],
                ]);
                if ($count === false) {
                    throw new RuntimeException("Daily claim limit must be 1..96");
                }
                query(
                    $db,
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ["claim_limit:" . $id, (string) $count],
                );
                $audit_detail = (string) $count;
            } else {
                if ($action !== "approve") {
                    foreach (
                        query(
                            $db,
                            "SELECT id FROM tasks WHERE worker_id=? AND state IN ('leased','submitted')",
                            [$id],
                        )->fetchAll(PDO::FETCH_COLUMN)
                        as $task
                    ) {
                        requeue($db, $task, $action . " credentials");
                    }
                }
                if ($action === "rotate") {
                    $token = trim(file_get_contents($argv[3]));
                    query(
                        $db,
                        "UPDATE workers SET token_hash=?,status='pending' WHERE id=?",
                        [secret_hash($token), $id],
                    );
                } else {
                    query(
                        $db,
                        "UPDATE workers SET status=?,strikes=0 WHERE id=?",
                        [$action === "approve" ? "approved" : "revoked", $id],
                    );
                }
            }
            event($db, $action, $id, null, $audit_detail);
            echo $action === "claim-limit" ? "Set daily claim limit to {$audit_detail}\n" : "Updated worker\n";
        } elseif ($action === "requeue") {
            $id = $argv[2] ?? "";
            $reason = trim($argv[3] ?? "");
            $task = query($db, "SELECT spec FROM tasks WHERE id=?", [
                $id,
            ])->fetchColumn();
            if (
                !$task ||
                $reason === "" ||
                json_decode($task, true)["protocol"] !== PROTOCOL
            ) {
                throw new RuntimeException("Current task and reason required");
            }
            requeue($db, $id, $reason);
            echo "Requeued\n";
        } elseif ($action === "expire") {
            expire_work($db);
            echo "Expired stale work\n";
        } elseif ($action === "workers") {
            foreach (
                query($db, "SELECT id,name,status,strikes FROM workers")
                as $row
            ) {
                echo json_encode($row), "\n";
            }
        } elseif ($action === "results") {
            foreach (
                query(
                    $db,
                    "SELECT task_id,worker_id,digest,result FROM results WHERE status='pending'",
                    [],
                )->fetchAll(PDO::FETCH_ASSOC)
                as $row
            ) {
                echo json_encode(
                [
                    "task_id" => $row["task_id"],
                    "worker_id" => $row["worker_id"],
                    "submission_digest" => $row["digest"],
                    "result_json" => $row["result"],
                ],
                JSON_UNESCAPED_SLASHES,
            ),
                    "\n";
            }
        } elseif ($action === "accept") {
            $path = $argv[2] ?? "";
            $keypath = getenv("P9_REVIEW_KEY") ?: "";
            if (
                !is_file($path) ||
                filesize($path) > 2000000 ||
                !is_file($keypath)
            ) {
                throw new RuntimeException(
                    "Receipt and private P9_REVIEW_KEY file required",
                );
            }
            $key = hex2bin(trim(file_get_contents($keypath)));
            if ($key === false || strlen($key) !== 32) {
                throw new RuntimeException("Invalid review key");
            }
            $receipt = json_decode(
                file_get_contents($path),
                true,
                32,
                JSON_THROW_ON_ERROR,
            );
            $payload = base64_decode($receipt["payload"] ?? "", true);
            if (
                $payload === false ||
                !hash_equals(
                    hash_hmac("sha256", $payload, $key),
                    $receipt["signature"] ?? "",
                )
            ) {
                throw new RuntimeException("Untrusted review signature");
            }
            $r = json_decode($payload, true, 32, JSON_THROW_ON_ERROR);
            $job = query(
                $db,
                "SELECT * FROM tasks WHERE id=? AND state='submitted'",
                [$r["task_id"] ?? ""],
            )->fetch(PDO::FETCH_ASSOC);
            $result = query(
                $db,
                "SELECT * FROM results WHERE task_id=? AND status='pending'",
                [$r["task_id"] ?? ""],
            )->fetch(PDO::FETCH_ASSOC);
            if (
                !$job ||
                !$result ||
                $job["expires"] <= time() ||
                ($r["contract"] ?? "") !== "trusted-full-replay-v1" ||
                ($r["spec_sha256"] ?? "") !== hash("sha256", $job["spec"]) ||
                ($r["submission_digest"] ?? "") !== $result["digest"] ||
                ($r["worker_id"] ?? "") !== $result["worker_id"] ||
                !in_array(
                    $r["disposition"] ?? "",
                    ["accepted", "disagrees"],
                    true,
                )
            ) {
                throw new RuntimeException(
                    "Review does not bind a current submission",
                );
            }
            $accepted = $r["disposition"] === "accepted";
            query(
                $db,
                "UPDATE results SET status=?,verified=?,receipt=? WHERE id=?",
                [
                    $accepted ? "accepted" : "rejected",
                    $accepted ? 1 : 0,
                    file_get_contents($path),
                    $result["id"],
                ],
            );
            if ($accepted) {
                query(
                    $db,
                    "UPDATE tasks SET state='reviewed',lease_hash=NULL,expires=NULL WHERE id=?",
                    [$job["id"]],
                );
            } else {
                requeue($db, $job["id"], "Trusted replay disagrees");
                query($db, "UPDATE workers SET status='pending' WHERE id=?", [
                    $result["worker_id"],
                ]);
            }
            event(
                $db,
                "review",
                $result["worker_id"],
                $job["id"],
                $r["disposition"],
            );
            echo "Recorded trusted review; no automatic sensitivity credit\n";
        } else {
            throw new RuntimeException(
                "Usage: admin.php init|migrate|add|workers|approve|revoke|rotate|reserve|claim-limit|requeue|expire|results|accept|backup|restore",
            );
        }
    });
}
