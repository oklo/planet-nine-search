<?php
declare(strict_types=1);

const PROTOCOL = "tess-gaia-hough-v2";
const LEASE_SECONDS = 1800;
const MAX_LEASE_SECONDS = 21600;
const REVIEW_SECONDS = 86400;

function database_path(): string
{
    return getenv("P9_DATABASE") ?:
        dirname(__DIR__, 2) . "/.planet-nine/queue.sqlite";
}

function database(): PDO
{
    if (!is_file(database_path())) {
        throw new RuntimeException("Coordinator is not initialized");
    }
    $db = new PDO("sqlite:" . database_path(), null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    ]);
    $db->exec("PRAGMA busy_timeout=10000; PRAGMA foreign_keys=ON");
    if (
        query(
            $db,
            "SELECT value FROM settings WHERE key='schema'",
        )->fetchColumn() !== "2"
    ) {
        throw new RuntimeException("Run the coordinator migration");
    }
    return $db;
}

function query(PDO $db, string $sql, array $args = []): PDOStatement
{
    $statement = $db->prepare($sql);
    $statement->execute($args);
    return $statement;
}

function claim_limit(PDO $db, string $worker): int
{
    $value = query($db, "SELECT value FROM settings WHERE key=?", [
        "claim_limit:" . $worker,
    ])->fetchColumn();
    if ($value === false) {
        return 8;
    }
    $limit = filter_var($value, FILTER_VALIDATE_INT, [
        "options" => ["min_range" => 1, "max_range" => 96],
    ]);
    if ($limit === false) {
        throw new RuntimeException("Configured daily claim limit must be 1..96");
    }
    return $limit;
}

function transaction(PDO $db, callable $action): mixed
{
    $db->exec("BEGIN IMMEDIATE");
    try {
        $value = $action();
        $db->exec("COMMIT");
        return $value;
    } catch (Throwable $error) {
        $db->exec("ROLLBACK");
        throw $error;
    }
}

function abort_request(int $status, string $message): never
{
    throw new InvalidArgumentException($message, $status);
}

function secret_hash(string $token): string
{
    if (!preg_match('/^[a-f0-9]{64}$/D', $token)) {
        abort_request(400, "Expected a 32-byte hexadecimal token");
    }
    return hash("sha256", $token);
}

function event(
    PDO $db,
    string $action,
    ?string $worker = null,
    ?string $task = null,
    string $detail = "",
): void {
    query(
        $db,
        "INSERT INTO events(time,action,worker_id,task_id,detail) VALUES(?,?,?,?,?)",
        [time(), $action, $worker, $task, substr($detail, 0, 500)],
    );
}

function requeue(
    PDO $db,
    string $task,
    string $reason,
    string $result_state = "rejected",
): void {
    query(
        $db,
        "UPDATE results SET status=? WHERE task_id=? AND status='pending'",
        [$result_state, $task],
    );
    query(
        $db,
        "UPDATE tasks SET state='pending',worker_id=NULL,lease_hash=NULL,expires=NULL,started=NULL,last_error=? WHERE id=?",
        [$reason, $task],
    );
    event($db, "requeue", null, $task, $reason);
}

function expire_work(PDO $db): void
{
    foreach (
        query(
            $db,
            "SELECT id,worker_id,state FROM tasks WHERE state IN ('leased','submitted') AND expires<=?",
            [time()],
        )->fetchAll(PDO::FETCH_ASSOC)
        as $job
    ) {
        if ($job["state"] === "submitted") {
            query(
                $db,
                "UPDATE workers SET status='pending' WHERE id=? AND status='approved'",
                [$job["worker_id"]],
            );
        } else {
            query(
                $db,
                "UPDATE workers SET strikes=strikes+1,status=CASE WHEN strikes>=2 THEN 'pending' ELSE status END WHERE id=?",
                [$job["worker_id"]],
            );
        }
        requeue($db, $job["id"], $job["state"] . " timeout", "expired");
    }
}

function authenticate(PDO $db): array
{
    $header = $_SERVER["HTTP_AUTHORIZATION"] ?? "";
    if (!preg_match('/^Bearer ([a-f0-9]{64})$/D', $header, $match)) {
        abort_request(401, "Worker token required");
    }
    $worker = query($db, "SELECT * FROM workers WHERE token_hash=?", [
        secret_hash($match[1]),
    ])->fetch(PDO::FETCH_ASSOC);
    if (!$worker) {
        abort_request(401, "Unknown worker");
    }
    if ($worker["status"] !== "approved") {
        abort_request(403, "Worker needs operator approval");
    }
    return $worker;
}

function active_lease(PDO $db, array $worker, array $body): array
{
    $job = query($db, "SELECT * FROM tasks WHERE id=?", [
        $body["task_id"] ?? "",
    ])->fetch(PDO::FETCH_ASSOC);
    $hash = secret_hash($body["lease_token"] ?? "");
    if (
        !$job ||
        $job["worker_id"] !== $worker["id"] ||
        !hash_equals($job["lease_hash"] ?? "", $hash)
    ) {
        abort_request(409, "Lease does not belong to this worker");
    }
    return $job;
}

function renew(PDO $db, array $job): int
{
    $expires = min(
        time() + LEASE_SECONDS,
        (int) $job["started"] + MAX_LEASE_SECONDS,
    );
    if ($expires <= time()) {
        abort_request(409, "Absolute lease lifetime exceeded");
    }
    query($db, "UPDATE tasks SET expires=? WHERE id=?", [$expires, $job["id"]]);
    return $expires - time();
}
