import {trialOrbit, trialPositionAt} from './orbit-model.mjs?v=3';

const radians = Math.PI / 180;
const obliquity = 23.439291111 * radians;

function direction(orbit, epoch, date, astronomy) {
  const time = astronomy.MakeTime(date);
  const [x, y, z] = trialPositionAt(orbit, time.tt - epoch.tt);
  const earth = astronomy.BaryState('Earth', time);
  const dx = x - earth.x;
  const dy = y * Math.cos(obliquity) - z * Math.sin(obliquity) - earth.y;
  const dz = y * Math.sin(obliquity) + z * Math.cos(obliquity) - earth.z;
  const ra = (Math.atan2(dy, dx) / radians + 360) % 360;
  const dec = Math.atan2(dz, Math.hypot(dx, dy)) / radians;
  return {ra, dec};
}

export function predict(data, date, astronomy) {
  const epoch = astronomy.MakeTime(new Date(data.epoch + 'T00:00:00Z'));
  const orbit = trialOrbit(data.position, data.velocity);
  const at = date => direction(orbit, epoch, date, astronomy);
  const {ra, dec} = at(date);
  return {ra, dec, constellation: astronomy.Constellation(ra / 15, dec).name, at};
}

// Project to percent coordinates in the survey's north-up, east-left TAN image.
export function tangentPoint(position, center, fieldMinutes) {
  const delta = (position.ra - center.ra) * radians;
  const dec = position.dec*radians, dec0 = center.dec*radians;
  const along = Math.sin(dec0)*Math.sin(dec) + Math.cos(dec0)*Math.cos(dec)*Math.cos(delta);
  if (along <= 0) return null;
  const scale = 50 / (Math.tan(fieldMinutes*radians/120) * along);
  return [50 - scale*Math.cos(dec)*Math.sin(delta),
          50 - scale*(Math.cos(dec0)*Math.sin(dec) - Math.sin(dec0)*Math.cos(dec)*Math.cos(delta))];
}

export function skyTrack(prediction, date, fieldMinutes) {
  const commands = [];
  let connected = false;
  for (let day=-30; day<=30; day++) {
    const position = prediction.at(new Date(date.getTime() + day*86400000));
    const point = tangentPoint(position, prediction, fieldMinutes);
    if (point) commands.push((connected ? 'L' : 'M') + point.map(value=>value.toFixed(4)).join(' '));
    connected = Boolean(point);
  }
  return commands.join(' ');
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
