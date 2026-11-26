<?php
declare(strict_types=1);
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');
$code = $_GET['field'] ?? '';
if (!is_string($code) || !preg_match('/^N[0-9]{1,12}$/D', $code)) {
    http_response_code(404);
    exit;
}
$snapshot = json_decode(file_get_contents(__DIR__ . '/snapshot.json'), true);
if (!isset($snapshot['fields'])) {
    http_response_code(503);
    exit;
}
$ids = [];
foreach ($snapshot['fields'] as $field) {
    if ($field['code'] !== $code || $field['state'] !== 'needs_followup') continue;
    $count = (int)$field['candidate_count'];
    if ($count > 0) $ids = $field['retained_candidate_flags'] ?? range(1, $count);
    break;
}
$dir = dirname(__DIR__, 3) . '/.planet-nine/candidate-figures';
if (isset($_GET['image'])) {
    $id = filter_var($_GET['image'], FILTER_VALIDATE_INT);
    $path = "$dir/$code-$id.png";
    if (!in_array($id, $ids, true) || !is_file($path)) {
        http_response_code(404);
        exit;
    }
    header('Content-Type: image/png');
    readfile($path);
    exit;
}
if (!$ids) http_response_code(410);
?>
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title><?= $code ?> · planet nine</title>
<link rel="stylesheet" href="/wp-content/themes/oklo/style.css"><link rel="stylesheet" href="map.css?v=17"></head>
<body><header class="site-header"><div class="band"><h1 class="site-title"><a href="./">planet nine</a></h1><p class="site-description">TESS / search coordination</p></div></header>
<main class="site-main"><p><a href="./">← Search atlas</a></p><h2><?= $code ?></h2>
<?php if (!$ids): ?><p>No candidates await follow-up in this field.</p><?php endif; ?>
<?php foreach ($ids as $id): $id = (int)$id; ?>
<section><h3>Candidate <?= $id ?></h3>
<?php if (is_file("$dir/$code-$id.png")): ?>
<figure class="candidate-figure"><a href="?field=<?= $code ?>&amp;image=<?= $id ?>"><img src="?field=<?= $code ?>&amp;image=<?= $id ?>" alt="TESS sector measurements for <?= $code ?> candidate <?= $id ?>"></a></figure>
<?php else: ?><p>Sector figures are being prepared.</p><?php endif; ?></section>
<?php endforeach; ?></main></body></html>
