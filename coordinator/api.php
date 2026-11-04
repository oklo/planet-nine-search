<?php
declare(strict_types=1);
require __DIR__ . '/common.php';
header('Content-Type: application/json');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

function dispatch(PDO $db, string $action, array $body): array {
    if ($action === 'status') {
        $counts = query($db, 'SELECT state, COUNT(*) AS count FROM tasks GROUP BY state')->fetchAll(PDO::FETCH_KEY_PAIR);
        return ['protocol' => PROTOCOL, 'tasks' => $counts,
            'expired_leases' => (int) query($db, "SELECT COUNT(*) FROM tasks WHERE state='leased' AND expires<=?", [time()])->fetchColumn(),
            'result_policy' => 'Submissions are unverified; no automatic discovery or exclusion credit.'];
    }
    if ($action === 'register') {
        $name = trim($body['name'] ?? '');
        $hash = secret_hash($body['token'] ?? '');
        if (!preg_match('/^[a-zA-Z0-9 _.-]{1,64}$/D', $name)) abort_request(400, 'Invalid worker name');
        return transaction($db, function () use ($db, $name, $hash) {
            $existing = query($db, 'SELECT id FROM workers WHERE token_hash=?', [$hash])->fetchColumn();
            if ($existing) return ['worker_id' => $existing];
            $salt = query($db, "SELECT value FROM settings WHERE key='ip_salt'")->fetchColumn();
            $address = hash_hmac('sha256', $_SERVER['REMOTE_ADDR'] ?? '', $salt);
            $count = query($db, 'SELECT COUNT(*) FROM workers WHERE address_hash=? AND created>?',
                [$address, time() - 86400])->fetchColumn();
            if ($count >= 10) abort_request(429, 'Daily registration limit reached; reuse your credentials');
            $id = bin2hex(random_bytes(16));
            query($db, 'INSERT INTO workers VALUES(?,?,?,?,?)', [$id, $name, $hash, $address, time()]);
            return ['worker_id' => $id];
        });
    }
    $worker = authenticate($db);
    return transaction($db, function () use ($db, $action, $body, $worker) {
        $now = time();
        if ($action === 'claim') {
            if (($body['protocol'] ?? '') !== PROTOCOL) abort_request(400, 'Unsupported search protocol');
            $hash = secret_hash($body['lease_token'] ?? '');
            $budget = $body['available_bytes'] ?? 0;
            if (!is_int($budget) || $budget < 1) abort_request(400, 'Positive available_bytes required');
            query($db, "UPDATE tasks SET state='pending', worker_id=NULL, lease_hash=NULL, expires=NULL WHERE state='leased' AND expires<=?", [$now]);
            $job = query($db, "SELECT * FROM tasks WHERE state='leased' AND worker_id=?", [$worker['id']])->fetch(PDO::FETCH_ASSOC);
            if ($job && !hash_equals($job['lease_hash'], $hash)) abort_request(409, 'Worker already has an active claim');
            if (!$job) {
                $job = query($db, "SELECT * FROM tasks WHERE state='pending' AND required_bytes<=? ORDER BY priority DESC,id LIMIT 1", [$budget])->fetch(PDO::FETCH_ASSOC);
                if (!$job) return ['task' => null, 'retry_after' => 300];
                query($db, "UPDATE tasks SET state='leased',worker_id=?,lease_hash=?,expires=?,attempts=attempts+1 WHERE id=?",
                    [$worker['id'], $hash, $now + LEASE_SECONDS, $job['id']]);
            } else {
                query($db, 'UPDATE tasks SET expires=? WHERE id=?', [$now + LEASE_SECONDS, $job['id']]);
            }
            return ['task_id' => $job['id'], 'task' => json_decode($job['spec'], true, 32, JSON_THROW_ON_ERROR),
                'spec_sha256' => hash('sha256', $job['spec']), 'lease_seconds' => LEASE_SECONDS];
        }
        $job = active_lease($db, $worker, $body);
        if ($action === 'submit' && $job['state'] === 'submitted') {
            $result = json_encode($body['result'] ?? null, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
            $saved = query($db, 'SELECT digest FROM results WHERE task_id=?', [$job['id']])->fetchColumn();
            if (!hash_equals($saved, hash('sha256', $result))) abort_request(409, 'Conflicting result');
            return ['accepted' => true, 'verified' => false];
        }
        if ($job['state'] !== 'leased' || (int)$job['expires'] <= $now) abort_request(409, 'Lease expired or released');
        if ($action === 'heartbeat') {
            query($db, 'UPDATE tasks SET expires=? WHERE id=?', [$now + LEASE_SECONDS, $job['id']]);
            return ['lease_seconds' => LEASE_SECONDS];
        }
        if ($action === 'release') {
            $failures = (int)$job['failures'] + (!empty($body['failed']) ? 1 : 0);
            $state = $failures >= 3 ? 'failed' : 'pending';
            $reason = substr((string)($body['reason'] ?? 'worker released'), 0, 500);
            query($db, 'UPDATE tasks SET state=?,worker_id=NULL,lease_hash=NULL,expires=NULL,last_error=?,failures=? WHERE id=?', [$state, $reason, $failures, $job['id']]);
            return ['released' => true];
        }
        if ($action !== 'submit') abort_request(404, 'Unknown action');
        $result = $body['result'] ?? [];
        if (!is_array($result) || ($result['task_id'] ?? '') !== $job['id'] || ($result['protocol'] ?? '') !== PROTOCOL
            || ($result['spec_sha256'] ?? '') !== hash('sha256', $job['spec'])
            || !in_array($result['state'] ?? '', ['needs_followup', 'searched', 'quality_rejected'], true)) {
            abort_request(400, 'Result identity, protocol, or state differs');
        }
        $encoded = json_encode($result, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
        query($db, 'INSERT INTO results(task_id,worker_id,submitted,digest,result,verified) VALUES(?,?,?,?,?,0)',
            [$job['id'], $worker['id'], $now, hash('sha256', $encoded), $encoded]);
        query($db, "UPDATE tasks SET state='submitted' WHERE id=?", [$job['id']]);
        return ['accepted' => true, 'verified' => false];
    });
}

try {
    $action = $_GET['action'] ?? 'status';
    $method = $_SERVER['REQUEST_METHOD'] ?? '';
    if (($action === 'status' && $method !== 'GET') || ($action !== 'status' && $method !== 'POST')) abort_request(405, 'Wrong HTTP method');
    $input = file_get_contents('php://input', false, null, 0, 2000001);
    if (strlen($input) > 2000000) abort_request(413, 'Request too large');
    $body = $input === '' ? [] : json_decode($input, true, 32, JSON_THROW_ON_ERROR);
    if (!is_array($body)) abort_request(400, 'JSON object required');
    echo json_encode(dispatch(database(), $action, $body), JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
} catch (InvalidArgumentException $error) {
    http_response_code($error->getCode() ?: 400);
    echo json_encode(['error' => $error->getMessage()]);
} catch (JsonException | TypeError $error) {
    http_response_code(400); echo json_encode(['error' => 'Invalid JSON request']);
} catch (Throwable $error) {
    error_log((string)$error);
    http_response_code(503); echo json_encode(['error' => 'Coordinator unavailable']);
}
