<?php
declare(strict_types=1);
if (PHP_SAPI !== 'cli') { http_response_code(404); exit; }
require __DIR__ . '/common.php';
$action = $argv[1] ?? '';
if ($action === 'init') {
    $path = getenv('P9_DATABASE') ?: dirname(__DIR__, 2) . '/.planet-nine/queue.sqlite';
    if (is_file($path)) throw new RuntimeException('Database already exists');
    if (!is_dir(dirname($path))) mkdir(dirname($path), 0700, true);
    umask(0077);
    $db = new PDO('sqlite:' . $path);
    $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    $db->exec("PRAGMA journal_mode=WAL;
        CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE workers(id TEXT PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,address_hash TEXT NOT NULL,created INTEGER NOT NULL);
        CREATE TABLE tasks(id TEXT PRIMARY KEY,spec TEXT NOT NULL,priority REAL NOT NULL,required_bytes INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'pending',worker_id TEXT REFERENCES workers(id),lease_hash TEXT,expires INTEGER,attempts INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,last_error TEXT);
        CREATE UNIQUE INDEX one_active_claim ON tasks(worker_id) WHERE state='leased';
        CREATE INDEX pending_tasks ON tasks(state,priority);
        CREATE TABLE results(task_id TEXT PRIMARY KEY REFERENCES tasks(id),worker_id TEXT REFERENCES workers(id),submitted INTEGER NOT NULL,digest TEXT NOT NULL,result TEXT NOT NULL,verified INTEGER NOT NULL DEFAULT 0);");
    query($db, 'INSERT INTO settings VALUES(?,?)', ['ip_salt', bin2hex(random_bytes(32))]);
    echo "Initialized\n";
} elseif ($action === 'add') {
    $raw = trim(file_get_contents($argv[2]));
    $spec = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
    if (($spec['protocol'] ?? '') !== PROTOCOL || !preg_match('/^[a-f0-9]{64}$/D', $spec['task_id'] ?? '')
        || !is_int($spec['required_bytes'] ?? null) || $spec['required_bytes'] <= 0) throw new RuntimeException('Invalid task');
    query(database(), 'INSERT INTO tasks(id,spec,priority,required_bytes) VALUES(?,?,?,?)',
        [$spec['task_id'], $raw, (float)($argv[3] ?? 0), $spec['required_bytes']]);
    echo "Queued {$spec['task_id']}\n";
} elseif ($action === 'reserve') {
    $worker = $argv[2] ?? '';
    $count = filter_var($argv[3] ?? '', FILTER_VALIDATE_INT, ['options' => ['min_range' => 0, 'max_range' => 10000]]);
    if (!preg_match('/^[a-f0-9]{32}$/D', $worker) || $count === false) throw new RuntimeException('Worker ID and reserve 0..10000 required');
    $db = database();
    if (!query($db, 'SELECT id FROM workers WHERE id=?', [$worker])->fetchColumn()) throw new RuntimeException('Unknown worker');
    query($db, 'INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ['reserve:' . $worker, (string)$count]);
    echo "Worker will leave {$count} fields ready for others\n";
} elseif ($action === 'results') {
    foreach (query(database(), 'SELECT task_id,result,verified FROM results') as $row) {
        echo json_encode(['task_id' => $row['task_id'], 'verified' => (bool)$row['verified'],
            'result' => json_decode($row['result'], true)], JSON_UNESCAPED_SLASHES), "\n";
    }
} else {
    fwrite(STDERR, "Usage: php admin.php init | add task.json [priority] | reserve worker_id count | results\n"); exit(2);
}
