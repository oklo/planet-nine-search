<?php
declare(strict_types=1);
if (PHP_SAPI !== "cli") {
    http_response_code(404);
    exit();
}

function setting(PDO $db, string $key, string $value): void
{
    query($db, "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [$key, $value]);
}

function review_key(): string
{
    $path = getenv("P9_REVIEW_KEY") ?: "";
    if (!is_file($path) || (fileperms($path) & 0077)) {
        throw new RuntimeException("Private P9_REVIEW_KEY file required");
    }
    $key = hex2bin(trim(file_get_contents($path)));
    if ($key === false || strlen($key) !== 32) {
        throw new RuntimeException("Invalid review key");
    }
    return $key;
}

function review_build(array $report): string
{
    $source = $report["source_sha256"] ?? "";
    $launch = $report["launch"] ?? [];
    if (!is_string($source) || !preg_match('/^[a-f0-9]{64}$/D', $source) ||
        ($launch["source_sha256"] ?? "") !== $source ||
        !is_string($report["device"] ?? null) || !is_array($report["dependencies"] ?? null)) {
        throw new RuntimeException("Missing release or device identity");
    }
    $dependencies = $report["dependencies"];
    ksort($dependencies);
    return hash("sha256", json_encode([$source, $report["device"], $dependencies,
        $launch["platform"] ?? "", $launch["python"] ?? ""], JSON_THROW_ON_ERROR));
}

function review_choice(bool $local, int $passes, bool $candidate, int $draw): array
{
    if ($draw < 0 || $draw > 9) {
        throw new RuntimeException("Invalid audit draw");
    }
    if ($candidate) {
        return ["full_replay", "candidate"];
    }
    if (!$local && $passes < 3) {
        return ["full_replay", "probation"];
    }
    if ($draw === 0) {
        return ["full_replay", "random_audit"];
    }
    return $local ? ["trusted_local", "trusted_local"] : ["statistical", "established_worker"];
}

function review_plan(PDO $db, string $task): string
{
    $row = query($db, "SELECT r.*,t.spec,t.expires FROM results r JOIN tasks t ON t.id=r.task_id JOIN workers w ON w.id=r.worker_id WHERE r.task_id=? AND r.status='pending' AND t.state='submitted' AND w.status='approved'", [$task])->fetch(PDO::FETCH_ASSOC);
    if (!$row || $row["expires"] <= time()) {
        throw new RuntimeException("Current approved submission required");
    }
    $name = "review_plan:" . $row["id"];
    $existing = query($db, "SELECT value FROM settings WHERE key=?", [$name])->fetchColumn();
    if ($existing !== false) {
        return $existing;
    }
    $report = json_decode($row["result"], true, 32, JSON_THROW_ON_ERROR);
    $build = review_build($report);
    $passes = (int) query($db, "SELECT value FROM settings WHERE key=?", ["audits:" . $row["worker_id"] . ":" . $build])->fetchColumn();
    $local = query($db, "SELECT value FROM settings WHERE key=?", ["trusted_local:" . $row["worker_id"]])->fetchColumn() === "1";
    $candidate = ($report["state"] ?? "") === "needs_followup" || !empty($report["candidate_count"]) || !empty($report["candidates"]);
    [$mode, $reason] = review_choice($local, $passes, $candidate, random_int(0, 9));
    $plan = ["contract" => "review-plan-v1", "task_id" => $task,
        "worker_id" => $row["worker_id"], "submission_digest" => $row["digest"],
        "spec_sha256" => hash("sha256", $row["spec"]), "source_sha256" => $report["source_sha256"],
        "build" => $build, "mode" => $mode, "reason" => $reason,
        "audit_probability" => 0.1, "passed_audits" => $passes, "created" => time()];
    $raw = json_encode($plan, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR);
    $signed = json_encode(["payload" => base64_encode($raw),
        "signature" => hash_hmac("sha256", $raw, review_key())], JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR);
    setting($db, $name, $signed);
    event($db, "review-plan", $row["worker_id"], $task, $mode . ":" . $reason);
    return $signed;
}

function check_review_plan(PDO $db, array $result, array $receipt): ?array
{
    $raw = query($db, "SELECT value FROM settings WHERE key=?", ["review_plan:" . $result["id"]])->fetchColumn();
    if (($receipt["contract"] ?? "") === "trusted-full-replay-v1" && $raw === false) {
        return null; // Preserve acceptance of a replay started before this policy.
    }
    if (($receipt["contract"] ?? "") !== "statistical-review-v1" || $raw === false) {
        throw new RuntimeException("Master-issued review plan required");
    }
    $signed = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
    $plan = json_decode(base64_decode($signed["payload"], true), true, 32, JSON_THROW_ON_ERROR);
    $method = $receipt["review_method"] ?? "";
    if (!hash_equals($signed["signature"], $receipt["plan_signature"] ?? "") ||
        !in_array($method, ["full_replay", $plan["mode"]], true) ||
        !in_array($method, ["full_replay", "trusted_local", "statistical"], true) ||
        ($receipt["sensitivity_credit"] ?? null) !== 0 ||
        ($receipt["audit_probability"] ?? null) !== 0.1 ||
        ($receipt["full_replay_verified"] ?? null) !== ($method === "full_replay" && $receipt["disposition"] === "accepted")) {
        throw new RuntimeException("Receipt does not satisfy the assigned review");
    }
    if ($method === "trusted_local" && query($db, "SELECT value FROM settings WHERE key=?", ["trusted_local:" . $result["worker_id"]])->fetchColumn() !== "1") {
        throw new RuntimeException("Local trust was revoked");
    }
    return $plan;
}

function record_audit(PDO $db, array $result, array $receipt, ?array $plan): void
{
    if ($plan === null) {
        return;
    }
    $key = "audits:" . $result["worker_id"] . ":" . $plan["build"];
    if ($receipt["disposition"] === "accepted") {
        if ($receipt["review_method"] === "full_replay") {
            $count = (int) query($db, "SELECT value FROM settings WHERE key=?", [$key])->fetchColumn();
            setting($db, $key, (string) ($count + 1));
        }
        event($db, "validation", $result["worker_id"], $result["task_id"], $receipt["review_method"]);
        return;
    }
    setting($db, $key, "0");
    setting($db, "trusted_local:" . $result["worker_id"], "0");
    // Withdraw unchecked reports for this build until an operator reassesses them.
    foreach (query($db, "SELECT id,task_id,receipt FROM results WHERE worker_id=? AND status='accepted'", [$result["worker_id"]])->fetchAll(PDO::FETCH_ASSOC) as $old) {
        $signed = json_decode($old["receipt"], true);
        $r = json_decode(base64_decode($signed["payload"] ?? "", true), true);
        if (($r["contract"] ?? "") !== "statistical-review-v1" || ($r["review_method"] ?? "") === "full_replay") {
            continue;
        }
        $prior = query($db, "SELECT value FROM settings WHERE key=?", ["review_plan:" . $old["id"]])->fetchColumn();
        $prior = json_decode($prior ?: "{}", true);
        $prior = json_decode(base64_decode($prior["payload"] ?? "", true), true);
        if (($prior["build"] ?? "") === $plan["build"]) {
            query($db, "UPDATE results SET status='audit_hold',verified=0 WHERE id=?", [$old["id"]]);
            query($db, "UPDATE tasks SET state='audit_hold',last_error='Related worker audit failed' WHERE id=?", [$old["task_id"]]);
            event($db, "audit-hold", $result["worker_id"], $old["task_id"], "Reassessment required");
        }
    }
}
