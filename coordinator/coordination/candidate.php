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
$orbits = is_file("$dir/orbits.json") ? json_decode(file_get_contents("$dir/orbits.json"), true) : [];
$notes = json_decode(file_get_contents(__DIR__ . '/visual-review.json'), true) ?? [];
$featured = [];
$public = [];
$pathEstimates = [];
foreach ($snapshot['fields'] as $field) {
    $name = $field['code'];
    if (!isset($active[$name])) continue;
    $record = $orbits[$name] ?? [];
    $ids = $active[$name];
    sort($ids, SORT_NUMERIC);
    if (($record['task_id'] ?? null) !== ($field['task_id'] ?? null)
        || ($record['reviewed_paths'] ?? []) !== $ids) continue;
    $pathEstimates[$name] = $record['rows'] ?? [];
    $selected = array_values(array_intersect($ids, $record['public_paths'] ?? []));
    $review = $notes[$name] ?? null;
    if ($review && $review['task_id'] === $record['task_id']) {
        foreach ($review['paths'] as $n => $note) {
            if (in_array($n, $ids, true) && is_file("$dir/$name-$n.png")) {
                $featured[$name][$n] = $note;
                if (!in_array($n, $selected, true)) $selected[] = $n;
            }
        }
    }
    if ($selected) $public[$name] = $selected;
}
if (isset($_GET['list'])) {
    $rows = [];
    foreach (array_replace(array_intersect_key($notes, $featured), $public) as $name => $numbers) {
        foreach ($numbers as $n) {
            $rows[] = ['field' => $name, 'number' => $n, 'orbit' => $pathEstimates[$name][$n] ?? null,
                'title' => $featured[$name][$n]['title'] ?? null];
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
$ids = array_unique(array_merge(array_keys($featured[$code] ?? []), $active[$code] ?? []));
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
function estimate($value, int $digits, string $unit = ''): string {
    if ((!is_int($value) && !is_float($value)) || !is_finite($value)) return '—';
    return number_format($value, $digits, '.', '') . $unit;
}
?>
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title><?= $code ?> · planet nine</title>
<link rel="stylesheet" href="/wp-content/themes/oklo/style.css"><link rel="stylesheet" href="map.css?v=32"></head>
<body class="evidence-page"><header class="site-header"><div class="band"><h1 class="site-title"><a href="./">planet nine</a></h1><p class="site-description">TESS / search coordination</p></div></header>
<main class="site-main"><p><a href="./">← Search atlas</a></p><h2><?= $code ?></h2>
<?php if (!$ids): ?><p>No candidates await follow-up in this field.</p><?php endif; ?>
<?php foreach ($ids as $id): $id = (int)$id; $note = $featured[$code][$id] ?? null; ?>
<section id="candidate-<?= $id ?>">
<?php if ($note): ?>
<h3>Path <?= $id ?> · <?= htmlspecialchars($note['title']) ?></h3>
<p><?= htmlspecialchars($note['text']) ?></p>
<?php else: ?><h3><?= in_array($id, $public[$code] ?? [], true) ? 'Candidate path' : 'Diagnostic path' ?> <?= $id ?></h3><?php endif; ?>
<?php if (is_file("$dir/$code-$id.png")): ?>
<figure class="candidate-figure"><a href="?field=<?= $code ?>&amp;image=<?= $id ?>"><img src="?field=<?= $code ?>&amp;image=<?= $id ?>" alt="TESS sector measurements for <?= $code ?> candidate <?= $id ?>"></a></figure>
<?php else: ?><p>Sector figures are being prepared.</p><?php endif; ?>
<?php $orbit = $pathEstimates[$code][$id] ?? null; if ($orbit):
    $radius = $orbit['radius'] ?? null;
    $mass = is_numeric($radius) && $radius >= 1 && $radius <= 6.61 ? 3 * $radius : null;
?>
<div class="path-estimates">
  <div><h4>Trial orbit</h4><dl>
    <dt>Observer distance</dt><dd><?= estimate($orbit['distance'] ?? null, 0, ' au') ?></dd>
    <dt>Semimajor axis</dt><dd><?= estimate($orbit['a'] ?? null, 0, ' au') ?></dd>
    <dt>Eccentricity</dt><dd><?= estimate($orbit['e'] ?? null, 2) ?></dd>
    <dt>Inclination</dt><dd><?= estimate($orbit['i'] ?? null, 1, '°') ?></dd>
  </dl></div>
  <div><h4>Physical estimates</h4><dl>
    <dt>TESS magnitude</dt><dd><?= estimate($orbit['T'] ?? null, 1) ?></dd>
    <dt>Radius</dt><dd><?= estimate($radius, 1, ' Earth radii') ?></dd>
    <dt>Mass</dt><dd><?= estimate($mass, 1, ' Earth masses') ?></dd>
  </dl></div>
</div>
<p class="estimate-note">The trial orbit assumes zero radial velocity. The radius estimate assumes a geometric albedo of 0.4 and V − T = 0.5. The mass estimate uses 3 Earth masses per Earth radius over the range 1–6.61 Earth radii.</p>
<?php else: ?><p>Orbit and physical estimates are unavailable for this path.</p><?php endif; ?>
<?php if ($note): ?><p>Do these images persuade you that TESS saw the same moving object?</p><?php endif; ?>
<form method="post" class="candidate-votes" action="?field=<?= $code ?>">
<input type="hidden" name="candidate" value="<?= $id ?>">
<?php foreach (['yes' => 'Compelling', 'no' => 'Not compelling'] as $key => $label): ?>
<button name="choice" value="<?= $key ?>"><?= $label ?> <span><?= $votes[$id][$key] ?? 0 ?></span></button>
<?php endforeach; ?></form></section>
<?php endforeach; ?></main></body></html>
