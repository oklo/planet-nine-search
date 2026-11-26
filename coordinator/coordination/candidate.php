<?php
declare(strict_types=1);
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');
$snapshot = json_decode(file_get_contents(__DIR__ . '/snapshot.json'), true);
if (!isset($snapshot['fields'])) {
    http_response_code(503);
    exit;
}
$dir = dirname(__DIR__, 3) . '/.planet-nine/candidate-figures';
$active = [];
foreach ($snapshot['fields'] as $field) {
    if ($field['state'] === 'needs_followup' && $field['candidate_count'] > 0) {
        $active[$field['code']] = $field['retained_candidate_flags'] ?? range(1, $field['candidate_count']);
    }
}
if (isset($_GET['list'])) {
    $orbits = is_file("$dir/orbits.json") ? json_decode(file_get_contents("$dir/orbits.json"), true) : [];
    $rows = [];
    foreach ($active as $name => $numbers) {
        foreach ($numbers as $n) {
            $rows[] = ['field' => $name, 'number' => $n, 'orbit' => $orbits[$name]['rows'][$n] ?? null];
        }
    }
    header('Content-Type: application/json');
    echo json_encode($rows);
    exit;
}
$code = $_GET['field'] ?? '';
if (!is_string($code) || !preg_match('/^N[0-9]{1,12}$/D', $code)) {
    http_response_code(404);
    exit;
}
$ids = $active[$code] ?? [];
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
$votes = [];
if ($ids) {
    $db = new PDO('sqlite:' . dirname($dir) . '/candidate-votes.sqlite', null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_TIMEOUT => 3
    ]);
    $db->exec('CREATE TABLE IF NOT EXISTS votes (field TEXT, candidate INTEGER, choice TEXT, total INTEGER NOT NULL, PRIMARY KEY(field,candidate,choice))');
    if ($_SERVER['REQUEST_METHOD'] === 'POST') {
        if (($_SERVER['HTTP_ORIGIN'] ?? '') !== 'https://oklo.org') {
            http_response_code(403);
            exit;
        }
        $id = filter_var($_POST['candidate'] ?? null, FILTER_VALIDATE_INT);
        $choice = $_POST['choice'] ?? '';
        if (!in_array($id, $ids, true) || !in_array($choice, ['yes','no'], true)) {
            http_response_code(400);
            exit;
        }
        $save = $db->prepare('INSERT INTO votes VALUES (?,?,?,1) ON CONFLICT(field,candidate,choice) DO UPDATE SET total=total+1');
        $save->execute([$code,$id,$choice]);
        header("Location: ?field=$code#candidate-$id", true, 303);
        exit;
    }
    $read = $db->prepare('SELECT candidate, choice, total FROM votes WHERE field=?');
    $read->execute([$code]);
    foreach ($read as $row) $votes[$row['candidate']][$row['choice']] = (int)$row['total'];
}
if (!$ids) http_response_code(410);
?>
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title><?= $code ?> · planet nine</title>
<link rel="stylesheet" href="/wp-content/themes/oklo/style.css"><link rel="stylesheet" href="map.css?v=25"></head>
<body class="evidence-page"><header class="site-header"><div class="band"><h1 class="site-title"><a href="./">planet nine</a></h1><p class="site-description">TESS / search coordination</p></div></header>
<main class="site-main"><p><a href="./">← Search atlas</a></p><h2><?= $code ?></h2>
<?php if (!$ids): ?><p>No candidates await follow-up in this field.</p><?php endif; ?>
<?php foreach ($ids as $id): $id = (int)$id; ?>
<section id="candidate-<?= $id ?>"><h3>Candidate <?= $id ?></h3>
<?php if (is_file("$dir/$code-$id.png")): ?>
<figure class="candidate-figure"><a href="?field=<?= $code ?>&amp;image=<?= $id ?>"><img src="?field=<?= $code ?>&amp;image=<?= $id ?>" alt="TESS sector measurements for <?= $code ?> candidate <?= $id ?>"></a></figure>
<?php else: ?><p>Sector figures are being prepared.</p><?php endif; ?>
<form method="post" class="candidate-votes" action="?field=<?= $code ?>">
<input type="hidden" name="candidate" value="<?= $id ?>">
<?php foreach (['yes' => 'Compelling', 'no' => 'Not compelling'] as $key => $label): ?>
<button name="choice" value="<?= $key ?>"><?= $label ?> <span><?= $votes[$id][$key] ?? 0 ?></span></button>
<?php endforeach; ?></form></section>
<?php endforeach; ?></main></body></html>
