import {recoveredOrbitCount, recoverySummary, testLabel} from './sensitivity.mjs?v=4';

'use strict';

const STATES = {
  complete: {label: 'Recovery measured', color: 'var(--field-complete)'},
  unsearchable: {label: 'Reviewed · no searchable data', color: 'var(--field-complete)'},
  needs_followup: {label: 'Field awaiting follow-up', color: 'var(--field-complete)', style: 'open'},
};
const $ = id => document.getElementById(id);
const metricDescriptions = {
  population: 'The share of the Brown–Batygin reference population recovered in injection tests.',
  searched: 'A typical field is a patch of sky 140 TESS pixels on a side, about 0.8° across. We usually search it in three or four TESS observing periods (sectors). Each field counts once after the search runs, even where neighboring fields overlap.',
};
const metricInfo = $('metric-info');
const topClose = $('candidate-top-close');
function updateCandidateClose() {
  topClose.hidden = true;
  if (metricInfo.open && metricInfo.querySelector('.candidate-list')) {
    topClose.hidden = metricInfo.scrollHeight <= metricInfo.clientHeight;
  }
}
new ResizeObserver(updateCandidateClose).observe($('metric-description'));
window.addEventListener('resize', updateCandidateClose);
for (const button of document.querySelectorAll('[data-metric]')) {
  button.addEventListener('click', () => {
    $('metric-title').textContent = button.textContent;
    const description = $('metric-description');
    const candidates = button.dataset.metric === 'flags';
    metricInfo.classList.toggle('has-candidates', candidates);
    if (candidates) {
      const list = document.createElement('div');
      description.replaceChildren(list);
      showCandidates(list);
    } else description.replaceChildren(metricDescriptions[button.dataset.metric]);
    topClose.hidden = true;
    metricInfo.showModal();
  });
}
async function showCandidates(container) {
  container.textContent = 'Loading candidates…';
  try {
    const response = await fetch('candidate.php?list=1', {cache: 'no-store'});
    if (!response.ok) throw new Error();
    const rows = await response.json();
    if (!rows.length) {
      container.textContent = 'There are currently no paths on display for visual review.';
      return;
    }
    const table = document.createElement('table');
    table.className = 'candidate-list';
    const headers = ['Field · path', 'T', 'R', 'M', 'Δ (au)', 'a (au)', 'e', 'i (°)'];
    const descriptions = ['Field and trial path number', 'TESS magnitude', 'Radius in Earth radii, assuming geometric albedo 0.4 and V − T = 0.5',
      'Estimated mass in Earth masses, using 3 Earth masses per Earth radius over the catalog range of 1–6.61 Earth radii', 'Observer distance', 'Semimajor axis', 'Eccentricity', 'Inclination'];
    const head = table.createTHead().insertRow();
    headers.forEach((title, index) => {
      const cell = textNode('th', '', head);
      cell.scope = 'col';
      cell.title = descriptions[index];
      cell.textContent = title;
      if (title === 'R' || title === 'M') textNode('sub', '⊕', cell);
    });
    const body = table.createTBody();
    for (const {field, number, orbit, title} of rows) {
      const row = body.insertRow();
      const cell = row.insertCell();
      textNode('a', field + ' · ' + number, cell).href =
        'candidate.php?field=' + encodeURIComponent(field) + '#candidate-' + number;
      if (title) textNode('small', title, cell);
      for (const [key, digits] of [['T', 1], ['radius', 1], ['mass', 1], ['distance', 0], ['a', 0], ['e', 2], ['i', 1]]) {
        const radius = orbit?.radius;
        const value = key === 'mass'
          ? (radius >= 1 && radius <= 6.61 ? 3 * radius : null)
          : orbit?.[key];
        row.insertCell().textContent = Number.isFinite(value) ? value.toFixed(digits) : '—';
      }
    }
    container.replaceChildren();
    textNode('p', 'These trial paths are open for visual review. Select a field and path to compare the TESS images and vote on whether you see a moving object.', container);
    container.append(table);
  } catch {
    container.textContent = 'Candidate list unavailable.';
  }
}

metricInfo.addEventListener('click', event => {
  if (event.target !== metricInfo) return;
  const box = metricInfo.getBoundingClientRect();
  if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) metricInfo.close();
});
const sky = $('sky');
const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const MAX_ZOOM = 512;
let data;
let view = {x: 0, y: 0, width: 1000, height: 500};
let selected = null;
let selectedSector = null;
const fieldNodes = new Map();
const pointers = new Map();
let gesture = null;

function svgNode(tag, attributes = {}, parent) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  parent?.append(node);
  return node;
}

function textNode(tag, text, parent, className = '') {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  parent.append(node);
  return node;
}

// RA increases leftward. Newton's iteration solves the Mollweide latitude.
function project(ra, dec) {
  const phi = dec * Math.PI / 180;
  let theta = phi;
  if (Math.abs(dec) < 89.999999) {
    for (let i = 0; i < 15; i++) {
      const step = (2 * theta + Math.sin(2 * theta) - Math.PI * Math.sin(phi)) /
        (2 + 2 * Math.cos(2 * theta));
      theta -= step;
      if (Math.abs(step) < 1e-12) break;
    }
  }
  return [500 + (180 - ra) / 360 * 1000 * Math.cos(theta), 250 - 250 * Math.sin(theta)];
}

function unproject(x, y) {
  if (((x - 500) / 500) ** 2 + ((y - 250) / 250) ** 2 > 1) return null;
  const theta = Math.asin(clamp((250 - y) / 250, -1, 1));
  const ra = 180 - (x - 500) / 1000 * 360 / Math.cos(theta);
  const dec = Math.asin(clamp((2 * theta + Math.sin(2 * theta)) / Math.PI, -1, 1)) * 180 / Math.PI;
  return [((ra % 360) + 360) % 360, dec];
}

function skyPath(points, close = false) {
  let previous;
  const path = points.map(point => {
    const command = !previous || Math.abs(previous[0] - point[0]) > 180 ? 'M' : 'L';
    previous = point;
    return command + project(...point).map(value => value.toFixed(5)).join(',');
  }).join(' ');
  return path + (close ? ' Z' : '');
}

function curve(points, attributes, parent) {
  return svgNode('path', {d: skyPath(points), fill: 'none', 'vector-effect': 'non-scaling-stroke', ...attributes}, parent);
}

function candidateLabel(field) {
  if (!Number.isInteger(field.candidate_count)) return 'Candidate count unavailable';
  return field.candidate_count === 0 ? 'No candidate traces' :
    `${field.candidate_count} trial path${field.candidate_count === 1 ? '' : 's'}`;
}

function candidatePage(field) {
  return field.state === 'needs_followup' && field.candidate_count > 0
    ? 'candidate.php?field=' + encodeURIComponent(field.code) : null;
}

function insidePolygon([x, y], polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, yi] = polygon[i], [xj, yj] = polygon[j];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function drawMap() {
  for (let ra = 0; ra <= 360; ra += 30) {
    curve(Array.from({length: 181}, (_, i) => [ra, i - 90]), {stroke: 'var(--map-grid)', 'stroke-width': .6}, $('graticule'));
  }
  for (let dec = -60; dec <= 60; dec += 30) {
    curve(Array.from({length: 361}, (_, ra) => [ra, dec]), {stroke: 'var(--map-grid)', 'stroke-width': .6}, $('graticule'));
  }
  curve(data.curves.galactic, {stroke: 'var(--map-galactic)', 'stroke-width': 1.1, 'stroke-dasharray': '5 4'}, $('reference-curves'));
  curve(data.curves.ecliptic, {stroke: 'var(--map-ecliptic)', 'stroke-width': 1.1}, $('reference-curves'));
  for (const field of data.fields) {
    const state = STATES[field.state];
    const group = svgNode('g', {'data-code': field.code}, $('fields'));
    svgNode('title', {}, group).textContent = `${field.code} · ${state.label} · ${[testLabel(field), candidateLabel(field)].filter(Boolean).join(' · ')}`;
    if (field.state === 'unsearchable') {
      group.style.opacity = 'var(--assessed-floor)';
      $('fields').prepend(group);
    } else if (field.state === 'complete') {
      const recovery = recoverySummary(field);
      group.style.opacity = `calc(var(--assessed-floor) + ${.595 * (recovery?.strength || 0)})`;
    }
    const followup = field.state === 'needs_followup';
    const paths = field.footprints.map(footprint => svgNode('path', {
      d: skyPath(footprint.ra_dec_deg, true), stroke: state.color, fill: followup ? 'none' : state.color,
      'pointer-events': 'all',
      'fill-opacity': field.state === 'complete' ? 1 : 'var(--field-fill)', 'stroke-width': 1.1, 'vector-effect': 'non-scaling-stroke',
      'data-sector': footprint.sector,
    }, group));
    const [x, y] = project(field.ra_deg, field.dec_deg);
    const shape = followup ? {x: -1, y: -1, width: 2, height: 2, fill: 'none'} :
      {cx: 0, cy: 0, r: 1, fill: state.style ? 'var(--map-surface)' : state.color};
    const dot = svgNode(followup ? 'rect' : 'circle', {...shape, stroke: state.color,
      'stroke-width': 1, 'pointer-events': 'all', 'vector-effect': 'non-scaling-stroke'}, group);
    const label = svgNode('g', {fill: state.color, 'data-code': field.code}, $('field-labels'));
    const codeText = svgNode('text', {'font-size': 12}, label);
    codeText.textContent = field.code;
    const details = svgNode('g', {class: 'field-depth'}, label);
    const depthText = svgNode('text', {'font-size': 11}, details);
    depthText.textContent = testLabel(field);
    const href = candidatePage(field);
    const parent = href ? svgNode('a', {href, 'aria-label': field.code + ' · ' + candidateLabel(field)}, details) : details;
    const countText = svgNode('text', {'font-size': 11}, parent);
    countText.textContent = candidateLabel(field);
    const labelWidth = Math.max(...[codeText, depthText, countText].map(n => n.getComputedTextLength())) + 12;
    const polygons = field.footprints.map(p => p.ra_dec_deg.map(point => project(...point)));
    fieldNodes.set(field.code, {group, paths, dot, label, codeText, details, depthText, countText, labelWidth, polygons, x, y, complete: field.state === 'complete'});
  }
  setView(view);
}

function setView(next) {
  const width = clamp(next.width, 1000 / MAX_ZOOM, 1000);
  const height = width / 2;
  view = {x: clamp(next.x, 0, 1000 - width), y: clamp(next.y, 0, 500 - height), width, height};
  sky.setAttribute('viewBox', `${view.x} ${view.y} ${width} ${height}`);
  const zoom = 1000 / width;
  // Keep labels and markers legible at any zoom, on both phones and desktops.
  const unit = width / Math.max(sky.getBoundingClientRect().width, 1);
  const occupied = [];
  const ordered = [...fieldNodes].sort(([a], [b]) => Number(b === selected) - Number(a === selected));
  for (const [code, nodes] of ordered) {
    const active = code === selected;
    const radius = (active ? 4 : zoom < 6 ? 2.5 : 2) * unit;
    nodes.dot.setAttribute('transform', `translate(${nodes.x} ${nodes.y}) scale(${radius})`);
    const detailed = [nodes.x - nodes.labelWidth / 2 * unit, nodes.y - 32 * unit,
      nodes.x + nodes.labelWidth / 2 * unit, nodes.y + 19 * unit];
    const corners = [[detailed[0], detailed[1]], [detailed[2], detailed[1]],
      [detailed[2], detailed[3]], [detailed[0], detailed[3]]];
    const collides = bounds => occupied.some(b => bounds[0] < b[2] && bounds[2] > b[0] && bounds[1] < b[3] && bounds[3] > b[1]);
    // Reveal the three-line label only when it fits inside a sector footprint.
    const showDetails = nodes.polygons.some(p => corners.every(c => insidePolygon(c, p))) && !collides(detailed);
    const bounds = showDetails ? detailed : [nodes.x + 6 * unit, nodes.y - 16 * unit, nodes.x + 40 * unit, nodes.y + 1 * unit];
    const overlap = collides(bounds);
    const visible = (active || (zoom >= 4 && !overlap)) && nodes.x > view.x && nodes.x < view.x + width && nodes.y > view.y && nodes.y < view.y + height;
    nodes.label.style.display = visible ? '' : 'none';
    nodes.details.style.display = showDetails ? '' : 'none';
    nodes.dot.style.display = visible && showDetails ? 'none' : '';
    if (visible) occupied.push(bounds);
    for (const [node, offset, size] of [[nodes.codeText, showDetails ? -18 : -5, 12], [nodes.depthText, -2, 11], [nodes.countText, 14, 11]]) {
      for (const [attr, value] of Object.entries({x: nodes.x + (showDetails ? 0 : 6 * unit),
        y: nodes.y + offset * unit, 'font-size': size * unit, 'stroke-width': 3 * unit,
        'text-anchor': showDetails ? 'middle' : 'start'})) node.setAttribute(attr, value);
    }
  }
  drawCoordinates(unit);
  $('zoom-level').textContent = `${zoom < 10 ? zoom.toFixed(1).replace('.0', '') : Math.round(zoom)}×`;
  $('zoom-in').disabled = zoom >= MAX_ZOOM - .1;
  $('zoom-out').disabled = zoom <= 1.001;
  $('reset-view').hidden = zoom <= 1;
  $('projection-label').hidden = zoom > 1;
}

function drawCoordinates(unit) {
  const layer = $('coordinates');
  layer.replaceChildren();
  const zoom = 1000 / view.width;
  const step = zoom < 3 ? 60 : zoom < 10 ? 10 : zoom < 35 ? 2 : .5;
  const center = unproject(view.x + view.width / 2, view.y + view.height / 2);
  if (!center) return;
  const decLabel = unproject(view.x + view.width / 2, view.y + view.height * .9)?.[1] ?? 0;
  for (let ra = 0; ra < 360; ra += step) {
    const [x, y] = project(ra, decLabel);
    if (x < view.x + 25 * unit || x > view.x + view.width - 25 * unit) continue;
    svgNode('text', {x, y, 'font-size': 10 * unit, 'stroke-width': 2.5 * unit, 'text-anchor': 'middle'}, layer).textContent = `${ra}°`;
  }
  if (zoom < 3) {
    for (const dec of [-60, -30, 30, 60]) {
      const [x, y] = project(180, dec);
      svgNode('text', {x: x + 5 * unit, y, 'font-size': 10 * unit, 'stroke-width': 2.5 * unit}, layer).textContent = `${dec > 0 ? '+' : '−'}${Math.abs(dec)}°`;
    }
  }
}

function zoomAt(factor, x = view.x + view.width / 2, y = view.y + view.height / 2) {
  const width = clamp(view.width / factor, 1000 / MAX_ZOOM, 1000);
  const ratio = width / view.width;
  setView({x: x - (x - view.x) * ratio, y: y - (y - view.y) * ratio, width});
}

function fitPoints(points, padding = 1.35) {
  const projected = points.map(point => project(...point));
  const xs = projected.map(p => p[0]), ys = projected.map(p => p[1]);
  const left = Math.min(...xs), right = Math.max(...xs), top = Math.min(...ys), bottom = Math.max(...ys);
  const width = clamp(Math.max(right - left, (bottom - top) * 2) * padding, 1000 / MAX_ZOOM, 1000);
  setView({x: (left + right - width) / 2, y: (top + bottom - width / 2) / 2, width});
}

function emphasizeField() {
  for (const [code, nodes] of fieldNodes) {
    const active = code === selected;
    for (const path of nodes.paths) {
      const sectorMatches = selectedSector === null || Number(path.dataset.sector) === selectedSector;
      path.style.opacity = active ? (sectorMatches ? 1 : .16) : (selected ? .35 : 1);
      path.setAttribute('stroke-width', active && sectorMatches ? 2 : 1.1);
      path.setAttribute('fill-opacity', nodes.complete ? 1 : (active && sectorMatches ? 'var(--field-fill-selected)' : 'var(--field-fill)'));
    }
    nodes.dot.style.opacity = selected && !active ? .45 : 1;
    if (active) $('fields').append(nodes.group);
  }
}

async function showTestScores(field, paragraph) {
  try {
    const response = await fetch('../api.php?action=calibration&task=' + encodeURIComponent(field.task_id));
    if (!response.ok) return;
    const result = await response.json();
    if (!paragraph.isConnected || !Number.isFinite(result.cutoff) || !field.injections?.length) return;
    const lookup = new Map(result.tests.map(t => [t.label, t]));
    if (field.injections.length !== lookup.size || !field.injections.every(t => {
      const row = lookup.get('prior ' + t.reference_index);
      return row && row.recovered === t.recovered;
    })) return;
    const scores = result.tests.map(t => t.score).filter(Number.isFinite);
    const best = scores.length ? ` Best test-planet score: ${Math.max(...scores).toFixed(2)}.` : '';
    paragraph.textContent = `A test planet had to beat ${result.cutoff.toFixed(2)}, the strongest false signal in the control searches.${best}`;
  } catch { /* The snapshot still contains the brightness and recovery counts. */ }
}

function selectField(code, zoom = true) {
  selected = code || null;
  selectedSector = null;
  $('clear-selection').hidden = !selected;
  emphasizeField();
  $('field-panel').hidden = !selected;
  const panel = $('field-details');
  panel.replaceChildren();
  if (!selected) {
    setView(view);
    return;
  }
  const field = data.fields.find(f => f.code === code);
  const state = STATES[field.state];
  textNode('h3', `${field.code} · ${field.name}`, panel, 'field-title');
  textNode('p', state.label, panel, 'field-state');
  textNode('p', `RA ${field.ra_deg.toFixed(3)}° · Dec ${field.dec_deg.toFixed(3)}°`, panel, 'field-meta');
  if (Array.isArray(field.distance_au)) textNode('p', `Orbital distance · ${field.distance_au.join('–')} au`, panel, 'field-meta');
  if (field.state !== 'complete' || !field.depth_samples?.length) textNode('p', field.result, panel);
  if (Number.isInteger(field.historical_flag_count)) {
    textNode('p', `${field.historical_flag_count} original flags · ${field.candidate_count} awaiting follow-up`, panel, 'small');
  }
  const summary = textNode('p', testLabel(field) + ' · ', panel, 'field-meta');
  if (candidatePage(field)) textNode('a', candidateLabel(field), summary).href = candidatePage(field);
  else summary.append(candidateLabel(field));
  const recovery = recoverySummary(field);
  if (recovery && recovery.assessed < recovery.declared) textNode('p', `${recovery.declared - recovery.assessed} additional test planets could not be assessed.`, panel, 'small');
  if (field.depth_samples?.some(s => s.recovered > 0)) {
    if (field.depth_label) textNode('p', field.depth_label, panel, 'small');
  } else if (field.depth_samples?.some(s => s.tested > 0)) {
    textNode('p', 'These tests did not establish a limiting magnitude.', panel, 'small');
  } else if (field.depth_note) textNode('p', field.depth_note, panel, 'small');
  textNode('p', 'Sector image footprints', panel, 'small');
  const sectors = textNode('div', '', panel, 'sector-list');
  for (const sector of [null, ...field.footprints.map(p => p.sector)]) {
    const button = textNode('button', sector === null ? 'All sectors' : `S${sector}`, sectors);
    button.type = 'button';
    button.setAttribute('aria-pressed', String(sector === null));
    button.addEventListener('click', () => {
      selectedSector = sector;
      for (const other of sectors.children) other.setAttribute('aria-pressed', String(other === button));
      emphasizeField();
    });
  }
  if (field.depth_samples?.length) {
    const table = textNode('table', '', panel, 'injections');
    const caption = textNode('caption', 'Injection recovery by magnitude', table);
    caption.className = 'screen-reader-text';
    const header = textNode('tr', '', textNode('thead', '', table));
    textNode('th', 'Magnitude', header).scope = 'col';
    textNode('th', 'Recovered / tested', header).scope = 'col';
    const body = textNode('tbody', '', table);
    for (const injection of field.depth_samples) {
      const row = textNode('tr', '', body);
      textNode('td', `${injection.band} ${injection.magnitude.toFixed(2)}`, row);
      textNode('td', `${injection.recovered} / ${injection.tested}`, row);
    }
    const explanation = textNode('p', 'A test planet counts as found when its path scores higher than the strongest false signal in the control searches.', panel, 'small');
    if (field.task_id && field.review_method !== 'trusted_local' && field.state === 'complete') {
      showTestScores(field, explanation);
    }

  }
  if (zoom) fitPoints(field.footprints.flatMap(p => p.ra_dec_deg), 1.65);
  else setView(view);
}

function mapPoint(event) {
  const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(sky.getScreenCTM().inverse());
  return [point.x, point.y];
}

function resetGesture() {
  const points = [...pointers.values()];
  if (!points.length) { gesture = null; return; }
  const mid = points.length === 1 ? points[0] : {x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2};
  gesture = {start: mid, view: {...view}, distance: points.length > 1 ? Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y) : 0,
    anchor: mapPoint({clientX: mid.x, clientY: mid.y}), moved: points.length > 1,
    code: points[0].code};
}

sky.addEventListener('pointerdown', event => {
  if (event.button !== 0 || event.target.closest('a')) return;
  sky.focus({preventScroll: true});
  pointers.set(event.pointerId, {x: event.clientX, y: event.clientY, code: event.target.closest('[data-code]')?.dataset.code});
  sky.setPointerCapture(event.pointerId);
  resetGesture();
});
sky.addEventListener('pointermove', event => {
  const world = mapPoint(event);
  const coord = unproject(...world);
  $('cursor-coordinates').textContent = coord ? `RA ${coord[0].toFixed(2)}° · Dec ${coord[1].toFixed(2)}°` : '';
  if (!pointers.has(event.pointerId) || !gesture) return;
  pointers.set(event.pointerId, {...pointers.get(event.pointerId), x: event.clientX, y: event.clientY});
  const points = [...pointers.values()];
  const mid = points.length === 1 ? points[0] : {x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2};
  if (Math.hypot(mid.x - gesture.start.x, mid.y - gesture.start.y) > 4) gesture.moved = true;
  if (!gesture.moved) return;
  sky.classList.add('dragging');
  const rect = sky.getBoundingClientRect();
  const distance = points.length > 1 ? Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y) : 0;
  const width = clamp(gesture.view.width * (gesture.distance ? gesture.distance / Math.max(distance, 1) : 1), 1000 / MAX_ZOOM, 1000);
  setView({x: gesture.anchor[0] - (mid.x - rect.left) / rect.width * width,
    y: gesture.anchor[1] - (mid.y - rect.top) / rect.height * width / 2, width});
});
function endPointer(event) {
  if (!pointers.has(event.pointerId)) return;
  const click = event.type === 'pointerup' && pointers.size === 1 && gesture && !gesture.moved;
  const code = gesture?.code;
  pointers.delete(event.pointerId);
  sky.classList.remove('dragging');
  if (click) {
    if (code && data) selectField(code);
    else zoomAt(2.5, ...mapPoint(event));
  }
  const wasMoved = gesture?.moved;
  resetGesture();
  if (gesture && wasMoved) gesture.moved = true;
}
sky.addEventListener('pointerup', endPointer);
sky.addEventListener('pointercancel', endPointer);
sky.addEventListener('pointerleave', () => { $('cursor-coordinates').textContent = ''; });
sky.addEventListener('wheel', event => {
  if (document.activeElement !== sky && !event.ctrlKey) return;
  event.preventDefault();
  zoomAt(Math.exp(clamp(-event.deltaY * .004, -.5, .5)), ...mapPoint(event));
}, {passive: false});
sky.addEventListener('keydown', event => {
  const shifts = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1]};
  if (shifts[event.key]) {
    const [x, y] = shifts[event.key];
    setView({...view, x: view.x + x * view.width * .15, y: view.y + y * view.height * .15});
  } else if (event.key === '+' || event.key === '=') zoomAt(1.7);
  else if (event.key === '-') zoomAt(1 / 1.7);
  else if (event.key === '0' || event.key === 'Home') setView({x: 0, y: 0, width: 1000});
  else return;
  event.preventDefault();
});
$('zoom-in').addEventListener('click', () => zoomAt(1.7));
$('zoom-out').addEventListener('click', () => zoomAt(1 / 1.7));
$('clear-selection').addEventListener('click', () => selectField(null, false));
$('reset-view').addEventListener('click', () => {
  setView({x: 0, y: 0, width: 1000});
  $('zoom-in').focus({preventScroll: true});
});
new ResizeObserver(() => setView(view)).observe(sky);

async function updateQueue() {
  try {
    const response = await fetch('../api.php?action=status', {cache: 'no-store', signal: AbortSignal.timeout(12000)});
    if (!response.ok) throw new Error('Queue unavailable');
    const status = await response.json();
    if (!status.tasks || typeof status.tasks !== 'object') throw new Error('Invalid queue status');
    const count = name => Number.isInteger(Number(status.tasks[name] ?? 0)) && Number(status.tasks[name] ?? 0) >= 0 ? Number(status.tasks[name] ?? 0) : 0;
    if (!status.queue_fields || !['pending', 'leased', 'submitted'].every(key => Array.isArray(status.queue_fields[key]))) throw new Error('Queue lists unavailable');
    const fieldsByTask = new Map((data?.fields || []).map(field => [field.task_id, field]));
    for (const [state, heading, listId, label] of [
      ['submitted', 'verification-heading', 'verification-fields', 'awaiting verification'],
      ['leased', 'leases-heading', 'active-leases', count('leased') === 1 ? 'active lease' : 'active leases'],
      ['pending', 'ready-heading', 'ready-fields', 'ready to claim'],
    ]) {
      $(heading).textContent = `${count(state)} ${label}`;
      const list = $(listId);
      const scrollTop = list.scrollTop;
      list.replaceChildren();
      for (const item of status.queue_fields[state]) {
        const field = fieldsByTask.get(item.task_id);
        const parts = [field?.code, item.label];
        if (state === 'leased' && item.started) parts.push('Leased ' + new Date(item.started * 1000).toLocaleString([], {year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}));
        textNode('li', parts.filter(Boolean).join(' · '), list);
      }
      if (!list.childElementCount) textNode('li', 'None', list, 'muted');
      list.scrollTop = scrollTop;
    }
    $('queue-lists').hidden = false;
    $('queue-status').hidden = true;
    if (data) displayRecovery(count('audit_hold') > 0);
  } catch {
    $('queue-lists').hidden = true;
    $('queue-status').hidden = false;
    $('queue-status').textContent = 'Live queue temporarily unavailable';
  }
}

function displayRecovery(held) {
  const count = recoveredOrbitCount(data.fields);
  const population = data.prior.population;
  const share = count / population;
  $('population-share').textContent = held ? 'Under review' : (100 * share).toFixed(2) + '%';
  if (held) {
    metricDescriptions.population = 'The recovery ledger is under review.';
  } else {
    const sentence = document.createElement('span');
    sentence.append(`Progress is measured against the ${population.toLocaleString('en-US')} possible versions of Planet Nine computed by `);
    textNode('a', 'Brown & Batygin', sentence).href = 'https://data.caltech.edu/records/8fjad-x7y61';
    sentence.append(' which have not been ruled out by searches to date.');
    metricDescriptions.population = sentence;
  }
}

async function initialize() {
  try {
    const response = await fetch('snapshot.json', {cache: 'no-store'});
    if (!response.ok) throw new Error('Snapshot unavailable');
    data = await response.json();
    if (data.schema_version !== 1 || !Array.isArray(data.fields)) throw new Error('Unsupported snapshot');
    const publicList = await fetch('candidate.php?list=1', {cache: 'no-store'});
    if (!publicList.ok) throw new Error('Candidate list unavailable');
    const paths = new Map();
    for (const row of await publicList.json()) {
      if (!paths.has(row.field)) paths.set(row.field, new Set());
      paths.get(row.field).add(row.number);
    }
    data.fields = data.fields.filter(field => Object.hasOwn(STATES, field.state) &&
      (field.state !== 'needs_followup' || paths.has(field.code)));
    for (const field of data.fields.filter(f => f.state === 'needs_followup')) {
      field.candidate_count = paths.get(field.code).size;
    }
    data.field_counts.needs_followup = data.fields.filter(f => f.state === 'needs_followup').length;
    data.physical_followup_flags = data.fields.reduce((n, f) => n + (f.state === 'needs_followup' ? f.candidate_count : 0), 0);
    $('snapshot-time').textContent = `${new Date(data.updated_utc).toLocaleString('en-GB', {day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC'})} UTC`;
    $('searched-count').textContent = data.searched_fields;
    $('followup-count').textContent = data.fields.filter(f => f.state === 'needs_followup' && f.candidate_count > 0).length;
    displayRecovery(false);
    for (const [state, info] of Object.entries(STATES)) {
      if (state === 'unsearchable' && !data.field_counts[state]) continue;
      const item = textNode('span', '', $('state-legend'));
      const swatch = textNode('i', '', item, state === 'complete' ? 'recovery-scale' : (info.style || ''));
      swatch.style.setProperty('--state-color', info.color);
      swatch.setAttribute('aria-hidden', 'true');
      if (state === 'unsearchable') swatch.style.opacity = 'var(--assessed-floor)';
      const count = data.field_counts[state] || 0;
      if (state === 'needs_followup') {
        const traces = data.physical_followup_flags;
        item.append(`${traces} trial path${traces === 1 ? '' : 's'} in ${count} ${count === 1 ? 'field' : 'fields'}`);
      } else {
        item.append(state === 'complete' ? info.label : `${info.label} (${count})`);
      }
    }
    drawMap();
  } catch (error) {
    $('load-error').hidden = false;
    $('snapshot-time').textContent = 'Search snapshot unavailable';
    console.error(error);
  }
  await updateQueue();
  setInterval(() => { if (!document.hidden) updateQueue(); }, 60000);
}
initialize();
