<?php
declare(strict_types=1);

function operator_checkpoint(PDO $db): array
{
    $snapshot = __DIR__ . "/coordination/snapshot.json";
    return [
        "last_review" => (int) query($db, "SELECT COALESCE(MAX(id),0) FROM results WHERE verified=1")->fetchColumn(),
        "snapshot" => is_file($snapshot) ? hash_file("sha256", $snapshot) : null,
    ];
}

function operator_session(PDO $db, string $action, array $argv): void
{
    $saved = query($db, "SELECT value FROM settings WHERE key='operator_lease'")->fetchColumn();
    $lease = $saved === false ? null : json_decode($saved, true, 8, JSON_THROW_ON_ERROR);
    $now = time();
    if ($action === "operator-status") {
        echo json_encode(["owner"=>$lease["owner"] ?? null, "expires"=>$lease["expires"] ?? 0,
            "checkpoint"=>operator_checkpoint($db)], JSON_THROW_ON_ERROR), "\n";
        return;
    }
    $token = getenv("P9_OPERATOR_FENCE") ?: "";
    if ($action === "operator-claim") {
        $owner = $argv[2] ?? "";
        if (!preg_match('/^[A-Za-z0-9._-]{1,64}$/D', $owner) || !preg_match('/^[a-f0-9]{64}$/D', $token)) {
            throw new RuntimeException("Operator identity and private fence required");
        }
        if ($lease && $lease["expires"] > $now && !hash_equals($lease["digest"], hash("sha256", $token))) {
            throw new RuntimeException("Another operator holds the lease");
        }
        $head = operator_checkpoint($db);
        if ((string)$head["last_review"] !== ($argv[3] ?? "") || ($head["snapshot"] ?? "-") !== ($argv[4] ?? "")) {
            throw new RuntimeException("Operator checkpoint has changed");
        }
        $lease = ["owner"=>$owner, "digest"=>hash("sha256", $token), "expires"=>$now+900];
    } elseif ($lease === null && !str_starts_with($action, "operator-")) {
        return;
    } else {
        if (!$lease || $lease["expires"] <= $now || !hash_equals($lease["digest"], hash("sha256", $token))) {
            throw new RuntimeException("Current operator lease required");
        }
        if ($action === "operator-release") {
            $lease["expires"] = 0;
        } elseif ($action === "operator-renew") {
            $lease["expires"] = $now+900;
        } else {
            return;
        }
    }
    query($db, "INSERT OR REPLACE INTO settings(key,value) VALUES('operator_lease',?)",
        [json_encode($lease, JSON_THROW_ON_ERROR)]);
    echo json_encode(["owner"=>$lease["owner"], "expires"=>$lease["expires"]], JSON_THROW_ON_ERROR), "\n";
}
