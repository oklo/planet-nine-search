import {trialOrbit, trialPositionAt} from './orbit-model.mjs?v=2';

const radians = Math.PI / 180;
const obliquity = 23.439291111 * radians;

export function predict(data, date, astronomy) {
  const time = astronomy.MakeTime(date);
  const epoch = astronomy.MakeTime(new Date(data.epoch + 'T00:00:00Z'));
  const orbit = trialOrbit(data.position, data.velocity);
  const [x, y, z] = trialPositionAt(orbit, time.tt - epoch.tt);
  const earth = astronomy.BaryState('Earth', time);
  const dx = x - earth.x;
  const dy = y * Math.cos(obliquity) - z * Math.sin(obliquity) - earth.y;
  const dz = y * Math.sin(obliquity) + z * Math.cos(obliquity) - earth.z;
  const ra = (Math.atan2(dy, dx) / radians + 360) % 360;
  const dec = Math.atan2(dz, Math.hypot(dx, dy)) / radians;
  return {ra, dec, constellation: astronomy.Constellation(ra / 15, dec).name};
}

export function sexagesimal(degrees, hours) {
  let total = Math.round(Math.abs(degrees) * 3600 / (hours ? 15 : 1));
  if (hours) total %= 86400;
  const pad = n => String(n).padStart(2, '0');
  const first = pad(Math.floor(total / 3600));
  const min = pad(Math.floor(total / 60) % 60), sec = pad(total % 60);
  return hours ? first+'h '+min+'m '+sec+'s' : (degrees < 0 ? '−' : '+')+first+'° '+min+'′ '+sec+'″';
}

// Odd dimensions place the reference point at the center of one pixel.
// Explicit TAN WCS also fixes north up, east left and the scale of the overlay.
export function findingChart(ra, dec, boxMinutes) {
  const fieldMinutes = Math.max(15, boxMinutes * 3);
  const pixels = 601;
  const tangent = minutes => Math.tan(minutes / 120 * radians);
  const degreesPerPixel = 2 * tangent(fieldMinutes) / (pixels * radians);
  const wcs = {NAXIS:2,NAXIS1:pixels,NAXIS2:pixels,CTYPE1:'RA---TAN',CTYPE2:'DEC--TAN',
    RADESYS:'ICRS',CRVAL1:ra,CRVAL2:dec,CRPIX1:(pixels+1)/2,CRPIX2:(pixels+1)/2,
    CDELT1:-degreesPerPixel,CDELT2:degreesPerPixel,CUNIT1:'deg',CUNIT2:'deg'};
  const params = new URLSearchParams({hips:'CDS/P/DSS2/color',wcs:JSON.stringify(wcs),format:'jpg'});
  const urls = ['alasky','alaskybis'].map(host =>
    'https://'+host+'.cds.unistra.fr/hips-image-services/hips2fits?'+params);
  return {fieldMinutes, boxPercent:100*tangent(boxMinutes)/tangent(fieldMinutes), urls};
}
