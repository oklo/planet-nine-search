<?php
declare(strict_types=1);
require __DIR__ . "/common.php";
header("Content-Type: application/json");
header("Cache-Control: no-store");
header("X-Content-Type-Options: nosniff");

function dispatch(PDO $db, string $action, array $body): array
{
    if ($action === "calibration") {
        $task = $_GET["task"] ?? "";
        if (!is_string($task) || !preg_match('/^[a-f0-9]{64}$/D', $task)) {
            abort_request(400, "Invalid task");
        }
        $raw = query($db, "SELECT result FROM results WHERE task_id=? AND status='accepted' AND verified=1 ORDER BY id DESC LIMIT 1", [$task])->fetchColumn();
        if ($raw === false) {
            abort_request(404, "No reviewed result");
        }
        $report = json_decode($raw, true, 64, JSON_THROW_ON_ERROR);
        $tests = [];
        foreach ($report["injections"] ?? [] as $row) {
            $tests[] = ["label" => $row["label"], "recovered" => $row["recovered"] ?? null, "score" => $row["best_score"] ?? null];
        }
        header("Cache-Control: public, max-age=60");
        return ["cutoff" => $report["control_maximum"] ?? null, "tests" => $tests];
    }
    // Expiry survives a subsequent rejected request.
    transaction($db, fn() => expire_work($db));
    if ($action === "status") {
        $counts = array_fill_keys(["pending", "leased", "submitted", "reviewed", "withdrawn", "legacy"], 0);
        foreach (query($db, "SELECT state,COUNT(*) FROM tasks GROUP BY state")->fetchAll(PDO::FETCH_KEY_PAIR) as $state => $count) {
            $counts[$state] = (int) $count;
        }
        $queues = ["pending" => [], "leased" => [], "submitted" => []];
        foreach (query($db, "SELECT id,spec,state,started FROM tasks WHERE state IN ('pending','leased','submitted') ORDER BY priority DESC,started,id") as $row) {
            $task = json_decode($row["spec"], true, 32, JSON_THROW_ON_ERROR);
            [$minimum, $maximum] = $task["policy"]["distance_au"] ?? [200, 1500];
            $label = preg_replace('/\s+\d+\s*[-–—]\s*\d+\s*au\s*$/iu', '', $task["label"] ?? $row["id"]);
            $field = ["task_id" => $row["id"], "label" => "$label {$minimum}–{$maximum} au"];
            if ($row["state"] === "leased") {
                $field["started"] = (int) $row["started"];
            }
            $queues[$row["state"]][] = $field;
        }
        return [
            "protocol" => PROTOCOL,
            "tasks" => $counts,
            "active_leases" => $queues["leased"],
            "queue_fields" => $queues,
            "expired_leases" => 0,
            "registration" => "automatic; consistency checks on submissions",
            "result_policy" => "Results receive consistency checks; candidate flags are retained for scientific follow-up.",
        ];
    }
    if ($action === "register") {
        $name = trim($body["name"] ?? "");
        $hash = secret_hash($body["token"] ?? "");
        if (!preg_match('/^[a-zA-Z0-9 _.-]{1,64}$/D', $name)) {
            abort_request(400, "Invalid worker name");
        }
        return transaction($db, function () use ($db, $name, $hash) {
            $existing = query(
                $db,
                "SELECT id,status FROM workers WHERE token_hash=?",
                [$hash],
            )->fetch(PDO::FETCH_ASSOC);
            if ($existing) {
                return [
                    "worker_id" => $existing["id"],
                    "approval" => $existing["status"],
                ];
            }
            $salt = query(
                $db,
                "SELECT value FROM settings WHERE key='ip_salt'",
            )->fetchColumn();
            $address = hash_hmac(
                "sha256",
                $_SERVER["REMOTE_ADDR"] ?? "",
                $salt,
            );
            $id = bin2hex(random_bytes(16));
            query(
                $db,
                "INSERT INTO workers(id,name,token_hash,address_hash,created,status) VALUES(?,?,?,?,?,?)",
                [$id, $name, $hash, $address, time(), "approved"],
            );
            event($db, "register", $id);
            return ["worker_id" => $id, "approval" => "approved"];
        });
    }
    return transaction($db, function () use ($db, $action, $body) {
        $worker = authenticate($db);
        $now = time();
        if ($action === "claim") {
            if (($body["protocol"] ?? "") !== PROTOCOL) {
                abort_request(400, "Unsupported search protocol");
            }
            $hash = secret_hash($body["lease_token"] ?? "");
            $budget = $body["available_bytes"] ?? 0;
            if (!is_int($budget) || $budget < 1) {
                abort_request(400, "Positive available_bytes required");
            }
            $job = query(
                $db,
                "SELECT * FROM tasks WHERE state='leased' AND worker_id=?",
                [$worker["id"]],
            )->fetch(PDO::FETCH_ASSOC);
            if ($job && !hash_equals($job["lease_hash"], $hash)) {
                abort_request(409, "Worker already has an active claim");
            }
            if (!$job) {
                if ((int) query($db, "SELECT COUNT(*) FROM tasks WHERE state='pending'")->fetchColumn() === 0) {
                    return ["task" => null, "retry_after" => 300, "reason" => "Waiting for coordinator replenishment"];
                }
                $job = query(
                    $db,
                    "SELECT * FROM tasks WHERE state='pending' AND required_bytes<=?
                         AND (CAST(? AS INTEGER)=1 OR json_type(spec,'$.policy.distance_au') IS NULL) AND NOT EXISTS
                         (SELECT 1 FROM events e WHERE e.worker_id=? AND e.task_id=tasks.id AND e.action='field_data_failure'
                          AND e.id>COALESCE((SELECT MAX(id) FROM events WHERE task_id=tasks.id AND action='operator_retry'),0))
                         ORDER BY priority DESC,id LIMIT 1",
                    [$budget, ($body["field_distance_range"] ?? false) === true ? 1 : 0, $worker["id"]],
                )->fetch(PDO::FETCH_ASSOC);
                if (!$job) {
                    $fits = query($db, "SELECT 1 FROM tasks WHERE state='pending' AND required_bytes<=? LIMIT 1", [$budget])->fetchColumn();
                    $reason = $fits ? "No ready field is eligible for this worker" : "No ready field fits this worker's disk allocation";
                    return ["task" => null, "retry_after" => 300, "reason" => $reason];
                }
                query(
                    $db,
                    "UPDATE tasks SET state='leased',worker_id=?,lease_hash=?,expires=?,started=?,attempts=attempts+1 WHERE id=?",
                    [
                        $worker["id"],
                        $hash,
                        $now + LEASE_SECONDS,
                        $now,
                        $job["id"],
                    ],
                );
                event($db, "claim", $worker["id"], $job["id"]);
                $seconds = LEASE_SECONDS;
            } else {
                $seconds = renew($db, $job);
            }
            return [
                "task_id" => $job["id"],
                "task" => json_decode(
                    $job["spec"],
                    true,
                    32,
                    JSON_THROW_ON_ERROR,
                ),
                "spec_sha256" => hash("sha256", $job["spec"]),
                "lease_seconds" => $seconds,
            ];
        }
        $job = active_lease($db, $worker, $body);
        $encoded = json_encode(
            $body["result"] ?? null,
            JSON_THROW_ON_ERROR |
                JSON_UNESCAPED_SLASHES |
                JSON_PRESERVE_ZERO_FRACTION,
        );
        if ($action === "submit" && $job["state"] === "submitted") {
            $saved = query(
                $db,
                "SELECT digest FROM results WHERE task_id=? AND status='pending'",
                [$job["id"]],
            )->fetchColumn();
            if (!$saved || !hash_equals($saved, hash("sha256", $encoded))) {
                abort_request(409, "Conflicting result");
            }
            return ["accepted" => true, "verified" => false];
        }
        if ($job["state"] !== "leased" || (int) $job["expires"] <= $now) {
            abort_request(409, "Lease expired or released");
        }
        if ($action === "heartbeat") {
            return ["lease_seconds" => renew($db, $job)];
        }
        if ($action === "release") {
            $reason = substr(
                (string) ($body["reason"] ?? "worker released"),
                0,
                500,
            );
            if (($body["field_data_failure"] ?? false) === true && !empty($body["failed"])) {
                event($db, "field_data_failure", $worker["id"], $job["id"], $reason);
                $count = (int) query($db, "SELECT COUNT(DISTINCT task_id) FROM events
                    WHERE worker_id=? AND action='field_data_failure' AND id>COALESCE(
                    (SELECT MAX(id) FROM events WHERE worker_id=? AND action IN ('submit','approve')),0)",
                    [$worker["id"], $worker["id"]])->fetchColumn();
                if ($count >= 3) {
                    query($db, "UPDATE workers SET status='pending' WHERE id=?", [$worker["id"]]);
                }
                requeue($db, $job["id"], $reason);
                return ["released" => true, "quarantined_for_worker" => true, "worker_held" => $count >= 3];
            }
            if (!empty($body["failed"])) {
                query($db, "UPDATE tasks SET failures=failures+1 WHERE id=?", [
                    $job["id"],
                ]);
                query(
                    $db,
                    "UPDATE workers SET strikes=strikes+1,status=CASE WHEN strikes>=2 THEN 'pending' ELSE status END WHERE id=?",
                    [$worker["id"]],
                );
            }
            requeue($db, $job["id"], $reason);
            return ["released" => true];
        }
        if ($action !== "submit") {
            abort_request(404, "Unknown action");
        }
        $result = $body["result"] ?? [];
        if (
            !is_array($result) ||
            ($result["task_id"] ?? "") !== $job["id"] ||
            ($result["protocol"] ?? "") !== PROTOCOL ||
            ($result["spec_sha256"] ?? "") !== hash("sha256", $job["spec"]) ||
            !in_array(
                $result["state"] ?? "",
                ["needs_followup", "searched", "quality_rejected"],
                true,
            )
        ) {
            abort_request(400, "Result identity, protocol, or state differs");
        }
        query(
            $db,
            "INSERT INTO results(task_id,worker_id,submitted,digest,result,status) VALUES(?,?,?,?,?,'pending')",
            [
                $job["id"],
                $worker["id"],
                $now,
                hash("sha256", $encoded),
                $encoded,
            ],
        );
        query($db, "UPDATE tasks SET state='submitted',expires=NULL WHERE id=?", [$job["id"]]);
        event($db, "submit", $worker["id"], $job["id"]);
        return ["accepted" => true, "verified" => false];
    });
}

try {
    $action = $_GET["action"] ?? "status";
    $method = $_SERVER["REQUEST_METHOD"] ?? "";
    if (
        (in_array($action, ["status", "calibration"], true) && $method !== "GET") ||
        (!in_array($action, ["status", "calibration"], true) && $method !== "POST")
    ) {
        abort_request(405, "Wrong HTTP method");
    }
    $input = file_get_contents("php://input", false, null, 0, 2000001);
    if (strlen($input) > 2000000) {
        abort_request(413, "Request too large");
    }
    $body =
        $input === "" ? [] : json_decode($input, true, 32, JSON_THROW_ON_ERROR);
    if (!is_array($body)) {
        abort_request(400, "JSON object required");
    }
    echo json_encode(
        dispatch(database(), $action, $body),
        JSON_THROW_ON_ERROR |
            JSON_UNESCAPED_SLASHES |
            JSON_PRESERVE_ZERO_FRACTION,
    );
} catch (InvalidArgumentException $error) {
    http_response_code($error->getCode() ?: 400);
    echo json_encode(["error" => $error->getMessage()]);
} catch (JsonException | TypeError $error) {
    http_response_code(400);
    echo json_encode(["error" => "Invalid JSON request"]);
} catch (Throwable $error) {
    error_log((string) $error);
    http_response_code(503);
    echo json_encode(["error" => "Coordinator unavailable"]);
}
