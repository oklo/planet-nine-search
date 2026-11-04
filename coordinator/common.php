<?php
declare(strict_types=1);

const PROTOCOL = 'tess-gaia-hough-v1';
const LEASE_SECONDS = 1800;

function database(): PDO {
    $path = getenv('P9_DATABASE') ?: dirname(__DIR__, 2) . '/.planet-nine/queue.sqlite';
    if (!is_file($path)) {
        throw new RuntimeException('Coordinator is not initialized');
    }
    $db = new PDO('sqlite:' . $path, null, null, [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
    $db->exec('PRAGMA busy_timeout=10000; PRAGMA foreign_keys=ON');
    return $db;
}

function query(PDO $db, string $sql, array $args = []): PDOStatement {
    $statement = $db->prepare($sql);
    $statement->execute($args);
    return $statement;
}

function transaction(PDO $db, callable $action): mixed {
    $db->exec('BEGIN IMMEDIATE');
    try {
        $value = $action();
        $db->exec('COMMIT');
        return $value;
    } catch (Throwable $error) {
        $db->exec('ROLLBACK');
        throw $error;
    }
}

function abort_request(int $status, string $message): never {
    throw new InvalidArgumentException($message, $status);
}

function secret_hash(string $token): string {
    if (!preg_match('/^[a-f0-9]{64}$/D', $token)) {
        abort_request(400, 'Expected a 32-byte hexadecimal token');
    }
    return hash('sha256', $token);
}

function authenticate(PDO $db): array {
    $header = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
    if (!preg_match('/^Bearer ([a-f0-9]{64})$/D', $header, $match)) {
        abort_request(401, 'Worker token required');
    }
    $worker = query($db, 'SELECT * FROM workers WHERE token_hash=?',
        [secret_hash($match[1])])->fetch(PDO::FETCH_ASSOC);
    if (!$worker) abort_request(401, 'Unknown worker');
    return $worker;
}

function active_lease(PDO $db, array $worker, array $body): array {
    $job = query($db, 'SELECT * FROM tasks WHERE id=?', [$body['task_id'] ?? ''])->fetch(PDO::FETCH_ASSOC);
    $hash = secret_hash($body['lease_token'] ?? '');
    if (!$job || $job['worker_id'] !== $worker['id'] || !hash_equals($job['lease_hash'] ?? '', $hash)) {
        abort_request(409, 'Lease does not belong to this worker');
    }
    return $job;
}
