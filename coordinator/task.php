<?php
declare(strict_types=1);
if (PHP_SAPI !== "cli") {
    http_response_code(404);
    exit();
}

function keys_are(array $value, array $keys): bool
{
    $actual = array_keys($value);
    sort($actual);
    sort($keys);
    return $actual === $keys;
}
function number(mixed $value): bool
{
    return (is_int($value) || is_float($value)) && is_finite((float) $value);
}
function vector(mixed $value, int $length): bool
{
    return is_array($value) &&
        array_is_list($value) &&
        count($value) === $length &&
        count(array_filter($value, "number")) === $length;
}
function validate_spec(string $raw): array
{
    if (strlen($raw) > 1000000) {
        throw new RuntimeException("Task exceeds limit");
    }
    $s = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
    $valid =
        is_array($s) &&
        keys_are($s, [
            "protocol",
            "task_id",
            "label",
            "sectors",
            "reference_sector",
            "bundle_url",
            "bundle_sha256",
            "bundle_bytes",
            "required_bytes",
            "injections",
            "policy",
        ]);
    if (
        !$valid ||
        $s["protocol"] !== PROTOCOL ||
        !is_string($s["label"]) ||
        strlen($s["label"]) > 100 ||
        !preg_match('/^[a-f0-9]{64}$/D', $s["task_id"]) ||
        !preg_match('/^[a-f0-9]{64}$/D', $s["bundle_sha256"])
    ) {
        throw new RuntimeException("Invalid task identity");
    }
    // make_task.py emits compact canonical JSON. Removing its top-level identity
    // preserves numeric tokens exactly across Python and PHP.
    $without = preg_replace(
        '/,"task_id":"[a-f0-9]{64}"(?=,|})/',
        "",
        $raw,
        -1,
        $removed,
    );
    if ($removed !== 1 || hash("sha256", $without) !== $s["task_id"]) {
        throw new RuntimeException(
            "Task hash differs; use canonical make_task.py output",
        );
    }
    $sectors = $s["sectors"];
    if (
        !is_array($sectors) ||
        !array_is_list($sectors) ||
        count($sectors) < 3 ||
        count($sectors) > 4 ||
        count(array_unique($sectors)) !== count($sectors)
    ) {
        throw new RuntimeException("Invalid sectors");
    }
    foreach ($sectors as $v) {
        if (!is_int($v) || $v < 1 || $v > 9999) {
            throw new RuntimeException("Invalid sector");
        }
    }
    if (!in_array($s["reference_sector"], $sectors, true)) {
        throw new RuntimeException("Invalid reference");
    }
    if (
        !is_int($s["bundle_bytes"]) ||
        $s["bundle_bytes"] < 1 ||
        $s["bundle_bytes"] > 67108864 ||
        !is_int($s["required_bytes"]) ||
        $s["required_bytes"] < 134217728 ||
        $s["required_bytes"] > 34359738368
    ) {
        throw new RuntimeException("Invalid size");
    }
    $url = parse_url($s["bundle_url"]);
    if (
        !$url ||
        ($url["scheme"] ?? "") !== "https" ||
        empty($url["host"]) ||
        isset($url["user"]) ||
        isset($url["pass"]) ||
        isset($url["fragment"]) ||
        (isset($url["port"]) && $url["port"] !== 443)
    ) {
        throw new RuntimeException("Invalid bundle URL");
    }
    if (
        filter_var($url["host"], FILTER_VALIDATE_IP) &&
        !filter_var(
            $url["host"],
            FILTER_VALIDATE_IP,
            FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE,
        )
    ) {
        throw new RuntimeException("Nonpublic bundle URL");
    }
    if (
        !is_array($s["injections"]) ||
        !array_is_list($s["injections"]) ||
        count($s["injections"]) > 8
    ) {
        throw new RuntimeException("Invalid injections");
    }
    foreach ($s["injections"] as $i) {
        if (
            !is_array($i) ||
            !keys_are($i, ["parameters", "radial_velocity", "flux", "label"]) ||
            !vector($i["parameters"], 5) ||
            !number($i["radial_velocity"]) ||
            !number($i["flux"]) ||
            $i["flux"] <= 0 ||
            $i["flux"] > 1000 ||
            $i["parameters"][4] < 1 / 1500 ||
            $i["parameters"][4] > 1 / 200 ||
            !is_string($i["label"]) ||
            strlen($i["label"]) > 100
        ) {
            throw new RuntimeException("Invalid injection");
        }
    }
    $p = $s["policy"];
    if (
        !is_array($p) ||
        !keys_are($p, ["version", "empirical_star_sectors", "prior"]) ||
        $p["version"] !== PROTOCOL ||
        !is_array($p["empirical_star_sectors"]) ||
        !array_is_list($p["empirical_star_sectors"])
    ) {
        throw new RuntimeException("Invalid policy");
    }
    $empirical = $p["empirical_star_sectors"];
    if (count(array_unique($empirical)) !== count($empirical)) {
        throw new RuntimeException("Duplicate empirical sector");
    }
    foreach ($empirical as $sid) {
        if (!in_array($sid, $sectors, true)) {
            throw new RuntimeException("Invalid empirical sector");
        }
    }
    $prior = $p["prior"];
    if ($prior !== null) {
        if (
            !is_array($prior) ||
            !keys_are($prior, [
                "version",
                "population_sha256",
                "lower",
                "upper",
                "optical_radius_max_earth",
                "angular_step",
                "inverse_step",
                "models",
                "reference_ids",
                "sky_radius_deg",
                "epoch_jd",
                "reference_frame_center",
            ]) ||
            $prior["version"] !== "local-brown-prior-v1" ||
            $prior["population_sha256"] !==
                "050b68182ecbf7fd76f280d8cc43c0683d207499aba1488a68092b834420a422"
        ) {
            throw new RuntimeException("Invalid prior");
        }
        if (
            !vector($prior["lower"], 3) ||
            !vector($prior["upper"], 3) ||
            !vector($prior["reference_frame_center"], 2)
        ) {
            throw new RuntimeException("Invalid prior coordinates");
        }
        for ($i = 0; $i < 3; $i++) {
            if ($prior["lower"][$i] >= $prior["upper"][$i]) {
                throw new RuntimeException("Empty prior");
            }
        }
        foreach (
            [
                "optical_radius_max_earth",
                "angular_step",
                "inverse_step",
                "sky_radius_deg",
                "epoch_jd",
            ]
            as $k
        ) {
            if (!number($prior[$k]) || $prior[$k] <= 0) {
                throw new RuntimeException("Invalid prior scale");
            }
        }
        $ids = $prior["reference_ids"];
        if (
            !is_int($prior["models"]) ||
            $prior["models"] < 64 ||
            $prior["models"] > 21740 ||
            !is_array($ids) ||
            !array_is_list($ids) ||
            count($ids) !== $prior["models"] ||
            count(array_unique($ids)) !== count($ids)
        ) {
            throw new RuntimeException("Invalid prior sample");
        }
        foreach ($ids as $id) {
            if (!is_int($id) || $id < 0 || $id >= 100000) {
                throw new RuntimeException("Invalid reference ID");
            }
        }
    }
    return $s;
}
