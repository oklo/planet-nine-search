import {predict, findingChart, sexagesimal, skyTrack} from './finder-model.mjs?v=4';

function drawTrail(layer, points) {
  const paths = document.createDocumentFragment();
  for (let i=1; i<points.length; i++) {
    const from=points[i-1], to=points[i];
    if (!from || !to) continue;
    const segment=document.createElementNS('http://www.w3.org/2000/svg','path');
    segment.setAttribute('d','M'+from.map(v=>v.toFixed(4)).join(' ')+' L'+to.map(v=>v.toFixed(4)).join(' '));
    segment.setAttribute('vector-effect','non-scaling-stroke');
    // Brightness follows time even when annual parallax makes the path turn back.
    const progress=(i-.5)/(points.length-1);
    segment.setAttribute('stroke-opacity',(0.82*progress**1.5).toFixed(4));
    paths.append(segment);
  }
  layer.replaceChildren(paths);
}

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

const nearby = new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (entry.isIntersecting) entry.target.querySelector('form').requestSubmit();
  }
}, {rootMargin:'300px'});

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
  const pathButton = result.querySelector('.finder-path');
  const trackLayer = frame.querySelector('.finder-track');
  for (const [button,overlay] of [[marker,cross],[planet,point],[pathButton,trackLayer]]) {
    button.addEventListener('click', () => {
      const show = button.getAttribute('aria-pressed') !== 'true';
      button.setAttribute('aria-pressed', String(show));
      overlay.toggleAttribute('hidden', !show);
    });
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    nearby.unobserve(root);
    pending?.abort();
    pending = new AbortController();
    const signal = pending.signal;
    const date = new Date(form.elements.utc.value + 'Z');
    const width = Number(form.elements.width.value);
    result.hidden = false;
    result.setAttribute('aria-busy', 'true');
    coordinates.textContent = '';
    status.textContent = 'Calculating position…';
    try {
      if (!Number.isFinite(date.getTime())) throw new Error('Enter a valid UTC observation time.');
      await import('./astronomy-engine-2.1.19.min.js');
      if (signal.aborted) return;
      const prediction = predict(data, date, window.Astronomy);
      const {ra, dec, constellation} = prediction;
      const epoch = document.createElement('time');
      epoch.dateTime = date.toISOString();
      epoch.textContent = epoch.dateTime.slice(0,19).replace('T',' ') + ' UTC';
      const position = document.createElement('span');
      position.className = 'finder-position';
      for (const [label, value, hours] of [['RA', ra, true], ['Dec', dec, false]]) {
        const part = document.createElement('span');
        part.textContent = label + ' ' + sexagesimal(value,hours) + ' (' + value.toFixed(5) + '°)';
        position.append(part);
      }
      coordinates.replaceChildren(epoch, ' · ' + constellation, position);
      status.textContent = 'Loading survey image…';
      const chart = findingChart(ra, dec, width);
      const track = skyTrack(prediction, date, chart.fieldMinutes);
      let image;
      for (const url of chart.urls) {
        try { image = await surveyImage(url, signal); break; }
        catch { if (signal.aborted) return; }
      }
      if (signal.aborted) return;
      if (!image) throw new Error('The survey image is unavailable. Use Find on sky to retry.');
      frame.querySelector('img').replaceWith(image);
      drawTrail(trackLayer,track);
      frame.style.setProperty('--box-side',chart.boxPercent+'%');
      result.querySelector('.finder-scale').textContent = width+'′ search box · '+chart.fieldMinutes+'′ field';
      status.textContent = '';
      result.setAttribute('aria-busy', 'false');
    } catch (error) {
      if (signal.aborted) return;
      result.hidden = true;
      result.setAttribute('aria-busy', 'false');
      status.textContent = error instanceof Error ? error.message : 'Unable to calculate this observation.';
    }
  });
  result.hidden = false;
  if (root.closest('section')?.matches(':target')) form.requestSubmit();
  else nearby.observe(root);
}
document.querySelectorAll('.sky-finder').forEach(mount);
