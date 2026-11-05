'use strict';

const STATES = {
  complete: {label: 'Search + recovery complete', color: 'var(--field-complete)'},
  needs_followup: {label: 'Follow-up open', color: 'var(--field-followup)'},
  calibration_pending: {label: 'Flags cleared · calibration pending', color: 'var(--field-calibration)'},
  validation_failed: {label: 'Validation failed', color: 'var(--field-validation)'},
  preflight_failed: {label: 'Stopped before search', color: 'var(--field-preflight)'},
  deferred: {label: 'Deferred / incomplete', color: 'var(--field-deferred)', style: 'open'},
  queued: {label: 'Queued', color: 'var(--field-queued)', style: 'dashed'},
};
const $ = id => document.getElementById(id);
const metricDescriptions = {
  recoverable: 'A projection from two benchmark fields, assuming comparable fields remain to search and a 20% chance that planet nine exists. The full searchable sky has not yet been calibrated.',
  recovered: 'Sensitivity accumulated in completed searches, estimated by adding test planets to the data and checking which we recover. Weighted by the Brown–Batygin orbit sample and a 20% chance that planet nine exists.',
  searched: 'Fields where the moving-object search ran. Some still have flags to review or recovery tests to finish.',
  flags: 'Trial tracks that passed the search threshold and still need checking. Several tracks can come from one stellar residual.',
  fields: 'All fields shown here, including completed searches, queued fields, and fields stopped by quality checks. Overlapping sector footprints can belong to the same field.',
};
const metricInfo = $('metric-info');
for (const button of document.querySelectorAll('[data-metric]')) {
  button.addEventListener('click', () => {
    $('metric-title').textContent = button.textContent;
    $('metric-description').textContent = metricDescriptions[button.dataset.metric];
    metricInfo.showModal();
  });
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
  return Number.isInteger(field.candidate_count)
    ? `${field.candidate_count} flag${field.candidate_count === 1 ? '' : 's'}`
    : 'Flags: —';
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
    svgNode('title', {}, group).textContent = `${field.code} · ${state.label} · ${field.depth_label || 'Depth uncalibrated'} · ${candidateLabel(field)}`;
    const paths = field.footprints.map(footprint => svgNode('path', {
      d: skyPath(footprint.ra_dec_deg, true), stroke: state.color, fill: state.color,
      'fill-opacity': 'var(--field-fill)', 'stroke-width': 1.1, 'vector-effect': 'non-scaling-stroke',
      'stroke-dasharray': field.state === 'queued' ? '4 3' : 'none',
      'data-sector': footprint.sector,
    }, group));
    const [x, y] = project(field.ra_deg, field.dec_deg);
    const dot = svgNode('circle', {cx: x, cy: y, r: 3, fill: state.style ? 'var(--map-surface)' : state.color,
      stroke: state.color, 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke'}, group);
    const label = svgNode('g', {fill: state.color, 'data-code': field.code}, $('field-labels'));
    const codeText = svgNode('text', {'font-size': 12}, label);
    codeText.textContent = field.code;
    const details = svgNode('g', {class: 'field-depth'}, label);
    const depthText = svgNode('text', {'font-size': 11}, details);
    depthText.textContent = field.depth_label || 'Depth uncalibrated';
    const countText = svgNode('text', {'font-size': 11}, details);
    countText.textContent = candidateLabel(field);
    const labelWidth = Math.max(...[codeText, depthText, countText].map(n => n.getComputedTextLength())) + 12;
    const polygons = field.footprints.map(p => p.ra_dec_deg.map(point => project(...point)));
    fieldNodes.set(field.code, {group, paths, dot, label, codeText, details, depthText, countText, labelWidth, polygons, x, y});
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
    nodes.dot.setAttribute('r', (active ? 4 : zoom < 6 ? 2.5 : 2) * unit);
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
  for (const button of document.querySelectorAll('[data-region]')) button.setAttribute('aria-pressed', String(button.dataset.region === 'all' && zoom === 1));
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
      path.setAttribute('fill-opacity', active && sectorMatches ? 'var(--field-fill-selected)' : 'var(--field-fill)');
    }
    nodes.dot.style.opacity = selected && !active ? .45 : 1;
    if (active) $('fields').append(nodes.group);
  }
}

function selectField(code, zoom = true) {
  selected = code || null;
  selectedSector = null;
  $('field-select').value = code || '';
  $('clear-selection').hidden = !selected;
  emphasizeField();
  const panel = $('field-details');
  panel.replaceChildren();
  if (!selected) {
    textNode('p', 'Select a field on the map or use the field menu to inspect its sectors.', panel, 'muted');
    setView(view);
    return;
  }
  const field = data.fields.find(f => f.code === code);
  const state = STATES[field.state];
  textNode('h3', `${field.code} · ${field.name}`, panel, 'field-title');
  textNode('p', state.label, panel, 'field-state');
  textNode('p', `RA ${field.ra_deg.toFixed(3)}° · Dec ${field.dec_deg.toFixed(3)}°`, panel, 'field-meta');
  textNode('p', field.result, panel);
  if (Number.isInteger(field.historical_flag_count)) {
    textNode('p', `${field.historical_flag_count} original flags · ${field.candidate_count} awaiting follow-up`, panel, 'small');
  }
  textNode('p', `${field.depth_label || 'Depth uncalibrated'} · ${candidateLabel(field)}`, panel, 'field-meta');
  if (field.depth_note) textNode('p', field.depth_note, panel, 'small');
  if (field.physical_followup_flags) textNode('p', `${field.physical_followup_flags} unresolved flag${field.physical_followup_flags === 1 ? '' : 's'} · no confirmed planet`, panel, 'small');
  textNode('p', field.state === 'queued' ? 'Planned sector footprints' : 'Sector image footprints', panel, 'small');
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
    textNode('p', 'T is the TESS magnitude; V assumes V − T = 0.5. Results combine the field’s searched sectors.', panel, 'small');
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
  if (event.button !== 0) return;
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
$('field-select').addEventListener('change', event => selectField(event.target.value));
$('clear-selection').addEventListener('click', () => selectField(null, false));
for (const button of document.querySelectorAll('[data-region]')) {
  button.addEventListener('click', () => {
    const boxes = {north: [48, 102, 5, 30], crowded: [91, 100.5, 17, 27.4], south: [314.5, 321.5, -34, -28.7]};
    const box = boxes[button.dataset.region];
    const southern = button.dataset.region === 'south' && data
      ? data.fields.filter(f => f.ra_deg > 300 && f.ra_deg < 330 && f.dec_deg < 0).flatMap(f => f.footprints.flatMap(p => p.ra_dec_deg)) : [];
    if (southern.length) fitPoints(southern, 1.1);
    else if (!box) setView({x: 0, y: 0, width: 1000});
    else {
      const [left, right, bottom, top] = box;
      fitPoints([[left, bottom], [left, top], [right, bottom], [right, top]], 1.1);
    }
    button.setAttribute('aria-pressed', 'true');
  });
}
new ResizeObserver(() => setView(view)).observe(sky);

async function updateQueue() {
  try {
    const response = await fetch('../api.php?action=status', {cache: 'no-store', signal: AbortSignal.timeout(12000)});
    if (!response.ok) throw new Error('Queue unavailable');
    const status = await response.json();
    if (!status.tasks || typeof status.tasks !== 'object') throw new Error('Invalid queue status');
    const count = name => Number.isInteger(Number(status.tasks[name] ?? 0)) && Number(status.tasks[name] ?? 0) >= 0 ? Number(status.tasks[name] ?? 0) : 0;
    const expired = Math.min(count('leased'), Math.max(0, Number(status.expired_leases) || 0));
    $('queue-status').textContent = `${count('pending') + expired} ready to claim · ${count('leased') - expired} active leases · ${count('submitted')} awaiting verification`;
    $('queue-time').textContent = `Live queue checked ${new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}. A lease ends at submission while independent verification continues.`;
  } catch {
    $('queue-status').textContent = 'Live queue temporarily unavailable';
    $('queue-time').textContent = 'The published map is still available. Queue status will retry automatically.';
  }
}

async function initialize() {
  try {
    const response = await fetch('snapshot.json', {cache: 'no-store'});
    if (!response.ok) throw new Error('Snapshot unavailable');
    data = await response.json();
    if (data.schema_version !== 1 || !Array.isArray(data.fields)) throw new Error('Unsupported snapshot');
    $('snapshot-time').textContent = `${new Date(data.updated_utc).toLocaleString('en-GB', {day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC'})} UTC`;
    $('searched-count').textContent = data.searched_fields;
    $('followup-count').textContent = data.physical_followup_flags;
    $('field-count').textContent = data.fields.length;
    $('recovered-expectation').textContent = data.sensitivity_proxy.value.toPrecision(4);
    if (data.expectation_forecast) {
      $('recoverable-expectation').textContent = data.expectation_forecast.value.toPrecision(2);
      $('forecast-note').textContent = 'The recoverable expectation is the earlier 100 machine-day projection from the initial two-worker benchmark. It assumes new, disjoint fields with comparable brightness and crowding. It is a sparse planning estimate, not a calibrated estimate of the total sky-wide recoverable probability; subsequent fields can change the projected rate.';
    } else {
      $('recoverable-expectation').textContent = 'Not estimated';
    }
    for (const [state, info] of Object.entries(STATES)) {
      const item = textNode('span', '', $('state-legend'));
      const swatch = textNode('i', '', item, info.style || '');
      swatch.style.setProperty('--state-color', info.color);
      swatch.setAttribute('aria-hidden', 'true');
      item.append(`${info.label} (${data.field_counts[state] || 0})`);
    }
    for (const field of [...data.fields].sort((a, b) => a.code.localeCompare(b.code, undefined, {numeric: true}))) {
      textNode('option', `${field.code} · ${STATES[field.state].label}`, $('field-select')).value = field.code;
    }
    $('proxy-note').textContent = `The current finite-prior sensitivity proxy is ${data.sensitivity_proxy.value.toPrecision(4)} expected planets, using P(existence) = 0.2 and V − T = 0.5. This engineering estimate is not a calibrated exclusion probability.`;
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
