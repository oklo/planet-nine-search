import assert from 'node:assert/strict';
import test from 'node:test';
import astronomy from '../coordinator/coordination/astronomy-engine-2.1.19.min.js';
import {predict, findingChart, sexagesimal, skyTrack, tangentPoint} from '../coordinator/coordination/finder-model.mjs';

// Archived trial N52304246/4. Reference directions use the search propagator
// and Astropy's Earth barycentric ephemeris, independently of the browser code.
const data = {"epoch": "2026-11-27", "position": [478.2768815313845, -336.4461778902296, -201.7528431614706], "velocity": [0.00024290388719785697, 0.00015020205652777118, 0.0003314682761615202]};
const reference = [["2019-09-14T06:15:34", 334.4062608477645, -31.18083857430421, "Pisces Austrinus"], ["2026-11-27T00:00:00", 334.37127725588397, -31.066253715379144, "Pisces Austrinus"], ["2027-05-11T08:48:17", 334.5721278531251, -30.99239544659298, "Pisces Austrinus"], ["2032-11-27T12:54:17", 334.3972413855704, -30.97962640537267, "Pisces Austrinus"]];
test('geocentric direction and constellation follow the requested UTC time', () => {
  for (const [iso, ra, dec, name] of reference) {
    const point = predict(data, new Date(iso+'Z'), astronomy);
    const delta = ((point.ra - ra + 540) % 360 - 180) * Math.cos(dec*Math.PI/180);
    assert.ok(3600*Math.hypot(delta,point.dec-dec) < 0.05);
    assert.equal(point.constellation,name);
  }
});
test('finder boxes use the tangent-plane plate scale and the central pixel', () => {
  for (const side of [1,2,5,10,20]) {
    const chart = findingChart(359.99,-82,side);
    const params = new URL(chart.urls[0]).searchParams;
    const wcs = JSON.parse(params.get('wcs'));
    assert.equal(params.get('hips'),'CDS/P/DSS2/color');
    assert.deepEqual([wcs.CRVAL1,wcs.CRVAL2],[359.99,-82]);
    assert.equal(wcs.CRPIX1,(wcs.NAXIS1+1)/2);
    assert.equal(wcs.CDELT1,-wcs.CDELT2);
    const half = wcs.CDELT2*Math.PI/180*wcs.NAXIS2*chart.boxPercent/200;
    assert.ok(Math.abs(2*Math.atan(half)*180/Math.PI*60 - side) < 1e-9);
  }
});
test('coordinate rounding carries across zero hours and minutes', () => {
  assert.equal(sexagesimal(359.99999,true),'00h 00m 00s');
  assert.equal(sexagesimal(-10.99999,false),'−11° 00′ 00″');
});

test('apparent sky track projects the two month endpoints to their Astropy WCS positions', () => {
  const date = new Date('2027-05-11T08:48:17Z');
  const prediction = predict(data,date,astronomy);
  const points = [...skyTrack(prediction,date,15).matchAll(/[ML]([-0-9.]+) ([-0-9.]+)/g)]
    .map(match => [Number(match[1]),Number(match[2])]);
  assert.equal(points.length,61);
  // FITS pixel coordinates increase upward; screen Y increases downward.
  for (const [i,expected] of [[0,[58.3422429,100-52.3596765]],[30,[50,50]],[60,[50.2792406,100-43.7995263]]]) {
    assert.ok(Math.hypot(...points[i].map((v,j)=>v-expected[j])) < .002);
  }
});
test('finder projection is continuous across zero RA and clips the rear hemisphere', () => {
  const center={ra:359.99,dec:72};
  const east=tangentPoint({ra:.01,dec:72},center,15);
  assert.ok(east[0]<50 && east[0]>45);
  assert.equal(tangentPoint({ra:179.99,dec:-72},center,15),null);
});
