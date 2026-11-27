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
$epoch = gmdate('Y-m-d');
$daily = [];
foreach ($snapshot['fields'] as $field) {
    $name = $field['code'];
    if (!isset($active[$name])) continue;
    $record = $orbits[$name] ?? [];
    $ids = $active[$name];
    sort($ids, SORT_NUMERIC);
    if (($record['task_id'] ?? null) !== ($field['task_id'] ?? null)
        || ($record['reviewed_paths'] ?? []) !== $ids) continue;
    $pathEstimates[$name] = $record['rows'] ?? [];
    $daily[$name] = $record['ephemeris'][$epoch] ?? [];
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
    $nightPath = "$dir/$code-$id-dark.png";
    if (($_GET['theme'] ?? '') === 'dark' && is_file($nightPath)) $path = $nightPath;
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
        if (!in_array($id, $ids, true) || !in_array($choice, ['yes','maybe','no'], true)) {
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
function sexagesimal(float $degrees, bool $hours): string {
    $seconds = (int)round(abs($degrees) * 3600 / ($hours ? 15 : 1));
    if ($hours) $seconds %= 86400;
    return ($hours ? '' : ($degrees < 0 ? '−' : '+'))
        . sprintf($hours ? '%02dh %02dm %02ds' : '%02d° %02d′ %02d″',
            intdiv($seconds, 3600), intdiv($seconds % 3600, 60), $seconds % 60);
}
?>
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title><?= $code ?> · planet nine</title>
<link rel="stylesheet" href="/wp-content/themes/oklo/style.css"><link rel="stylesheet" href="map.css?v=39">
<script type="module" src="orbit-view.mjs?v=4"></script>
<script type="module" src="sky-finder.mjs?v=3"></script></head>
<body class="evidence-page"><header class="site-header"><div class="band"><h1 class="site-title"><a href="./">planet nine</a></h1><p class="site-description">TESS / search coordination</p></div></header>
<main class="site-main"><p><a href="./">← Search atlas</a></p><h2><?= $code ?></h2>
<?php if (!$ids): ?><p>No candidates await follow-up in this field.</p><?php endif; ?>
<?php foreach ($ids as $id): $id = (int)$id; $note = $featured[$code][$id] ?? null; ?>
<section id="candidate-<?= $id ?>" class="path-evidence">
<?php if ($note): ?>
<h3>Path <?= $id ?> · <?= htmlspecialchars($note['title']) ?></h3>
<?php else: ?><h3><?= in_array($id, $public[$code] ?? [], true) ? 'Candidate path' : 'Diagnostic path' ?> <?= $id ?></h3><?php endif; ?>
<div class="path-columns"><div class="path-assets">
<?php if ($note): ?><p><?= htmlspecialchars($note['text']) ?></p><?php endif; ?>
<?php if (is_file("$dir/$code-$id.png")): ?>
<figure class="candidate-figure">
<?php [$figureWidth, $figureHeight] = getimagesize("$dir/$code-$id.png");
$themes = is_file("$dir/$code-$id-dark.png") ? ['light', 'dark'] : ['default'];
foreach ($themes as $theme):
    $src = "?field=$code&amp;image=$id" . ($theme === 'dark' ? '&amp;theme=dark' : ''); ?>
<a class="figure-<?= $theme ?>" href="<?= $src ?>"><img width="<?= (int)$figureWidth ?>" height="<?= (int)$figureHeight ?>" loading="lazy" src="<?= $src ?>" alt="TESS sector measurements for <?= $code ?> candidate <?= $id ?>"></a>
<?php endforeach; ?></figure>
<?php else: ?><p>Sector figures are being prepared.</p><?php endif; ?>
<?php if ($note): ?><p>Do these images persuade you that TESS saw the same moving object?</p><?php endif; ?>
<form method="post" class="candidate-votes" action="?field=<?= $code ?>">
<input type="hidden" name="candidate" value="<?= $id ?>">
<?php foreach (['yes' => 'Compelling', 'maybe' => 'Maybe', 'no' => 'Not compelling'] as $key => $label): ?>
<button name="choice" value="<?= $key ?>"><?= $label ?> <span><?= $votes[$id][$key] ?? 0 ?></span></button>
<?php endforeach; ?></form></div>
<div class="path-orbit-panel">
<?php $orbit = $pathEstimates[$code][$id] ?? null; if ($orbit):
    $radius = $orbit['radius'] ?? null;
    $mass = is_numeric($radius) && $radius >= 1 && $radius <= 6.61 ? 3 * $radius : null;
    $day = $daily[$code] ?? [];
    $sky = $day['paths'][$id] ?? null;
?>
<?php if ($sky): ?>
<div class="sky-finder">
<form class="finder-form" aria-label="Sky position at observation time">
<label>Observation time (UTC)<input type="datetime-local" name="utc" value="<?= $epoch ?>T00:00" step="1" required></label>
<label>Search box<select name="width"><option value="1">1′ square</option><option value="2">2′ square</option><option value="5" selected>5′ square</option><option value="10">10′ square</option><option value="20">20′ square</option></select></label>
<button type="submit">Find on sky</button>
</form>
<p class="finder-coordinates" aria-live="polite"></p>
<p class="finder-status" role="status"></p>
<figure class="finder-result" aria-busy="true" hidden>
<div class="finder-frame"><img alt="Digitized Sky Survey image centered on the trial prediction">
<svg class="finder-track" viewBox="0 0 100 100" aria-hidden="true" focusable="false"><path vector-effect="non-scaling-stroke"/></svg>
<span class="finder-box" aria-hidden="true"></span><span class="finder-source" aria-hidden="true"></span><span class="finder-cross" aria-hidden="true"></span>
<span class="finder-orientation" aria-hidden="true">N ↑ · E ←</span></div>
<div class="finder-controls"><button type="button" class="finder-planet" aria-pressed="true">Simulated planet</button><button type="button" class="finder-marker" aria-pressed="true">Predicted position</button><span class="finder-scale"></span></div>
<figcaption>Archival Digitized Sky Survey image. The amber line traces the predicted sky position for 30 days on either side of the observation. <a href="https://alasky.cds.unistra.fr/hips-image-services/hips2fits">CDS HiPS2FITS</a></figcaption>
</figure></div>
<div class="orbit-view" aria-busy="true">
<div class="orbit-plate"><div class="orbit-frame"><canvas tabindex="0" aria-label="Solar system and trial path <?= $id ?>. Arrow keys rotate, plus and minus zoom.">Interactive orbit diagram</canvas>
<span class="trial-key"><i aria-hidden="true"></i> Trial orbit · 10-year steps</span>
<div class="orbit-tools"><button type="button" data-zoom="in" disabled aria-label="Zoom in">+</button><button type="button" data-zoom="out" disabled aria-label="Zoom out">−</button><button type="button" data-center disabled title="Center the selected object or the orbit at this zoom">Center</button><button type="button" data-reset hidden disabled title="Fit the full orbit">Reset</button></div>
<output class="orbit-readout" aria-live="polite"></output></div>
<div class="orbit-layers">
<label><input type="checkbox" data-layer="planets" checked> Planets</label>
<label><input type="checkbox" data-layer="largest" checked> 100 largest KBOs</label>
<label><input type="checkbox" data-layer="detached" checked> Detached KBOs</label>
</div></div>
<p class="orbit-guide">Drag to rotate, scroll or pinch to zoom. Shift-drag or use two fingers to pan. Double-click or double-tap a spot to center it at this zoom. Select an object to identify it.</p>
<details class="orbit-sources"><summary>Comparison objects and sources</summary>
<p>The size ranking excludes centaurs and includes estimated diameters. The detached sample contains 204 objects classified in December 2023, including uncertain classifications. Eleven also appear in the size ranking.
<a href="https://web.gps.caltech.edu/~mbrown/dps.html">Brown size catalog</a> · <a href="https://github.com/katvolk/TNO-classifications">Volk &amp; Van Laerhoven classifications</a> (<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>) · <a href="https://ssd.jpl.nasa.gov/sb/orbits.html">JPL small-body elements</a> · <a href="https://ssd.jpl.nasa.gov/planets/approx_pos.html">JPL planet positions</a></p></details>
<script type="application/json" class="orbit-data"><?= json_encode(['jd' => $day['jd'], 'sun' => $day['sun'], 'position' => $sky['position'], 'velocity' => $sky['velocity'], 'epoch' => $epoch], JSON_HEX_TAG | JSON_THROW_ON_ERROR) ?></script>
</div>
<?php endif; ?>
<div class="path-estimates">
  <div><h4>Trial orbit</h4><dl>
    <dt>Observer distance</dt><dd><?= estimate($orbit['distance'] ?? null, 0, ' au') ?></dd>
    <dt>Semimajor axis</dt><dd><?= estimate($orbit['a'] ?? null, 0, ' au') ?></dd>
    <dt>Period</dt><dd><?= isset($orbit['period_years']) ? number_format($orbit['period_years']) . ' yr' : (($orbit['e'] ?? 0) >= 1 ? 'Open orbit' : '—') ?></dd>
    <dt>Eccentricity</dt><dd><?= estimate($orbit['e'] ?? null, 2) ?></dd>
    <dt>Inclination</dt><dd><?= estimate($orbit['i'] ?? null, 1, '°') ?></dd>
  </dl></div>
  <div><h4>Physical estimates</h4><dl>
    <dt>TESS magnitude</dt><dd><?= estimate($orbit['T'] ?? null, 1) ?></dd>
    <dt>Radius</dt><dd><?= estimate($radius, 1, ' Earth radii') ?></dd>
    <dt>Mass</dt><dd><?= estimate($mass, 1, ' Earth masses') ?></dd>
  </dl></div>
</div>
<?php if ($sky): ?>
<div class="path-sky"><h4>Predicted sky position</h4><dl>
<dt>Epoch (UTC)</dt><dd><time datetime="<?= $epoch ?>T00:00:00Z"><?= $epoch ?> 00:00</time></dd>
<dt>Right ascension</dt><dd><?= sexagesimal($sky['ra_deg'], true) ?></dd>
<dt>Declination</dt><dd><?= sexagesimal($sky['dec_deg'], false) ?></dd>
<dt>Constellation</dt><dd><?= htmlspecialchars($sky['constellation']) ?></dd></dl>
<p class="estimate-note">Geocentric ICRS direction predicted by the trial orbit.</p></div>
<?php else: ?><p>Sky position unavailable for <?= $epoch ?> UTC.</p><?php endif; ?>
<p class="estimate-note">The trial orbit assumes zero radial velocity. The radius estimate assumes a geometric albedo of 0.4 and V − T = 0.5. The mass estimate uses 3 Earth masses per Earth radius over the range 1–6.61 Earth radii.</p>
<?php else: ?><p>Orbit and physical estimates are unavailable for this path.</p><?php endif; ?>
</div></div></section>
<?php endforeach; ?></main></body></html>
