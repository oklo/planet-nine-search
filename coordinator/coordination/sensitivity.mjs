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
