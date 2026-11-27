import {predict, findingChart, sexagesimal} from './finder-model.mjs?v=1';

function surveyImage(url, signal) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.alt = 'Digitized Sky Survey image centered on the trial prediction';
    img.referrerPolicy = 'no-referrer';
    function finish(error) {
      clearTimeout(timer);
      img.onload = img.onerror = null;
      signal.removeEventListener('abort', cancel);
      if (error) {
        img.removeAttribute('src');
        reject(error);
      } else resolve(img);
    }
    const cancel = () => finish(new Error('Request canceled.'));
    const timer = setTimeout(() => finish(new Error('Survey image timed out.')), 15000);
    img.onload = () => img.naturalWidth === 601 && img.naturalHeight === 601
      ? finish() : finish(new Error('Unexpected survey image dimensions.'));
    img.onerror = () => finish(new Error('Survey image unavailable.'));
    signal.addEventListener('abort', cancel, {once:true});
    img.src = url;
  });
}

function mount(root) {
  const form = root.querySelector('form');
  const coordinates = root.querySelector('.finder-coordinates');
  const status = root.querySelector('.finder-status');
  const result = root.querySelector('figure');
  const frame = result.querySelector('.finder-frame');
  const marker = result.querySelector('.finder-marker');
  const cross = result.querySelector('.finder-cross');
  const planet = result.querySelector('.finder-planet');
  const point = result.querySelector('.finder-source');
  const data = JSON.parse(root.closest('.path-orbit-panel').querySelector('.orbit-data').textContent);
  let pending;
  function showMarker(show) {
    marker.setAttribute('aria-pressed', String(show));
    cross.hidden = !show;
  }
  function showPlanet(show) {
    planet.setAttribute('aria-pressed', String(show));
    point.hidden = !show;
  }
  planet.addEventListener('click', () => showPlanet(point.hidden));
  marker.addEventListener('click', () => showMarker(cross.hidden));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    pending?.abort();
    pending = new AbortController();
    const signal = pending.signal;
    const date = new Date(form.elements.utc.value + 'Z');
    const width = Number(form.elements.width.value);
    result.hidden = true;
    coordinates.textContent = '';
    status.textContent = 'Calculating position…';
    try {
      if (!Number.isFinite(date.getTime())) throw new Error('Enter a valid UTC observation time.');
      await import('./astronomy-engine-2.1.19.min.js');
      if (signal.aborted) return;
      const {ra, dec, constellation} = predict(data, date, window.Astronomy);
      const epoch = date.toISOString().slice(0,19).replace('T',' ') + ' UTC';
      coordinates.textContent = epoch + ' · RA ' + sexagesimal(ra,true) + ' · Dec ' +
        sexagesimal(dec,false) + ' · ' + constellation + ' (' + ra.toFixed(5) + '°, ' + dec.toFixed(5) + '°)';
      status.textContent = 'Loading survey image…';
      const chart = findingChart(ra, dec, width);
      let image;
      for (const url of chart.urls) {
        try { image = await surveyImage(url, signal); break; }
        catch { if (signal.aborted) return; }
      }
      if (signal.aborted) return;
      if (!image) throw new Error('The survey image is unavailable. Use Find on sky to retry.');
      frame.querySelector('img').replaceWith(image);
      frame.style.setProperty('--box-side',chart.boxPercent+'%');
      result.querySelector('.finder-scale').textContent = width+'′ search box · '+chart.fieldMinutes+'′ field';
      showMarker(true);
      showPlanet(false);
      status.textContent = '';
      result.hidden = false;
    } catch (error) {
      if (!signal.aborted) status.textContent = error instanceof Error ? error.message : 'Unable to calculate this observation.';
    }
  });
}
document.querySelectorAll('.sky-finder').forEach(mount);
