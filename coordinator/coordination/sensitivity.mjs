// Display strength from completed recovery tests. This does not alter the ledger.
export function recoverySummary(field) {
  if (field.state !== 'complete') return null;
  let recovered = 0, assessed = 0, declared = 0;
  if (field.injections?.length && field.injections.every(row => Number.isInteger(row.reference_index))) {
    const seen = new Set();
    for (const row of field.injections) {
      if (!Number.isInteger(row.reference_index) || seen.has(row.reference_index)) return null;
      seen.add(row.reference_index);
      declared++;
      if (typeof row.recovered === 'boolean') {
        assessed++;
        recovered += Number(row.recovered);
      }
    }
  } else {
    for (const row of field.depth_samples || []) {
      if (!Number.isInteger(row.tested) || !Number.isInteger(row.recovered) ||
          row.tested < 0 || row.recovered < 0 || row.recovered > row.tested) return null;
      declared += row.tested;
      recovered += row.recovered;
    }
    assessed = declared;
  }
  if (!declared) return null;
  // One-sigma Wilson lower score tempers sparse successes. Unassessed trials
  // contribute no demonstrated recovery; this is a display index, not a
  // calibrated exclusion probability for the whole footprint.
  const p = recovered / declared;
  const strength = Math.max(0, (p + .5 / declared -
    Math.sqrt(p * (1 - p) / declared + .25 / declared ** 2)) / (1 + 1 / declared));
  return {recovered, assessed, declared, strength};
}

export function testLabel(field) {
  const samples = (field.depth_samples || []).filter(s => s.tested > 0 && Number.isFinite(s.magnitude));
  if (!samples.length) {
    if (field.state === 'queued' || field.state === 'preflight_failed') return 'Search not run';
    if (field.state === 'needs_followup' || field.state === 'calibration_pending') return 'Recovery tests pending';
    if (field.state === 'unsearchable') return 'No usable images';
    if (field.injections?.some(s => s.assessed === false)) return 'No test planet assessed';
    return field.depth_label && !/no injections recovered/i.test(field.depth_label) ? field.depth_label : 'No test brightness recorded';
  }
  const n = samples.reduce((v, s) => v + s.tested, 0);
  const found = samples.reduce((v, s) => v + s.recovered, 0);
  const bands = [...new Set(samples.map(s => s.band))];
  const range = bands.map(b => {
    const mags = samples.filter(s => s.band === b).map(s => s.magnitude);
    const lo = Math.min(...mags), hi = Math.max(...mags);
    return b + ' ' + lo.toFixed(2) + (lo === hi ? '' : '–' + hi.toFixed(2));
  }).join(', ');
  return n === 1 ? `1 test planet · ${range} · ${found ? 'found' : 'missed'}` : `${found}/${n} test planets found · ${range}`;
}

// November 4 ledger recoveries whose early map entries omit the orbit ID.
const historicalOrbitIds = [5652, 10938, 25264, 36205, 36600, 42627, 43449,
  49218, 49907, 57898, 67130, 68175, 97468];

export function recoveredOrbitCount(fields) {
  const ids = new Set(historicalOrbitIds);
  for (const field of fields) {
    if (field.state !== 'complete') continue;
    for (const trial of field.injections || []) {
      if (trial.recovered === true && trial.assessed !== false && Number.isInteger(trial.reference_index)) {
        ids.add(trial.reference_index);
      }
    }
  }
  return ids.size;
}
