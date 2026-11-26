import test from 'node:test';
import assert from 'node:assert/strict';
import {recoveredOrbitCount, recoverySummary} from '../coordinator/coordination/sensitivity.mjs';

const field = (success, total) => ({state: 'complete', injections: Array.from({length: total}, (_, i) => ({reference_index: i, recovered: i < success}))});
test('a completed search without recovery stays faint', () => {
  assert.equal(recoverySummary(field(0, 12)).strength, 0);
  assert.equal(recoverySummary({state: 'preflight_failed'}), null);
  assert.equal(recoverySummary({state: 'complete', depth_samples: []}), null);
});
test('more recoveries and more supporting tests deepen green', () => {
  assert.ok(recoverySummary(field(1, 4)).strength < recoverySummary(field(3, 4)).strength);
  assert.ok(recoverySummary(field(1, 1)).strength < recoverySummary(field(10, 10)).strength);
  assert.ok(recoverySummary(field(1, 1)).strength < 1);
});
test('duplicates and unassessed trials cannot increase recovery', () => {
  const f = field(1, 1), initial = recoverySummary(f).strength;
  f.injections.push({reference_index: 2, recovered: null});
  assert.equal(recoverySummary(f).assessed, 1);
  assert.ok(recoverySummary(f).strength < initial);
  f.injections.push(f.injections[0]);
  assert.equal(recoverySummary(f), null);
});
test('historical magnitude tests are retained', () => {
  const r = recoverySummary({state: 'complete', depth_samples: [{tested: 4, recovered: 2}, {tested: 4, recovered: 0}]});
  assert.equal(r.declared, 8);
  assert.equal(r.recovered, 2);
});

test('older native reports without prior IDs use their magnitude counts', () => {
  const f = {state: 'complete', injections: [{V: 19, recovered: true}], depth_samples: [{tested: 1, recovered: 1}]};
  assert.equal(recoverySummary(f).recovered, 1);
  assert.equal(recoverySummary(f).strength, .5);
});

test('map labels distinguish a measured miss from pending tests', async () => {
  const {testLabel} = await import('../coordinator/coordination/sensitivity.mjs');
  const miss = {band: 'V', magnitude: 19.97, tested: 1, recovered: 0};
  assert.equal(testLabel({state: 'complete', depth_samples: [miss]}), '1 test planet · V 19.97 · missed');
  assert.equal(testLabel({state: 'calibration_pending'}), 'Recovery tests pending');
  assert.equal(testLabel({state: 'preflight_failed'}), 'Search not run');
  assert.equal(testLabel({state: 'complete', injections: [{assessed: false}]}), 'No test planet assessed');
  assert.equal(testLabel({state: 'complete', depth_samples: [miss, {...miss, magnitude: 21, tested: 2}]}), '0/3 test planets found · V 19.97–21.00');
});


test('survey count adds distinct assessed recoveries to the historical ledger', () => {
  const baseline = recoveredOrbitCount([]);
  const trials = [5652, 100, 100].map(reference_index => ({reference_index, recovered: true}));
  const fields = [
    {state: 'complete', injections: [...trials, {reference_index: 101, recovered: true, assessed: false}]},
    {state: 'needs_followup', injections: [{reference_index: 102, recovered: true}]},
  ];
  assert.equal(recoveredOrbitCount(fields), baseline + 1);
});
