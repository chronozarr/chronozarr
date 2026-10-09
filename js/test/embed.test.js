// The pure parts of the embedding contract (js/demo/embed.js): URL parameters, who may talk to the viewer,
// validation of the host's commands, time lookup, pixel <-> lon/lat, message shapes; and the markup and stylesheet
// of index.html that implement the embed layout.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createProjection } from '../maplibre/projection.js';
import {
  EMBED_VERSION,
  embedAttributes,
  embedMessage,
  isTrustedSender,
  makeGeo,
  nearestTime,
  normalizeOrigin,
  parseCommand,
  parseEmbedParams,
  parseIsoMs,
  resolveSet,
  toPlainJson,
} from '../demo/embed.js';

const html = readFileSync(new URL('../demo/index.html', import.meta.url), 'utf8');
const css = html.slice(html.indexOf('<style>'), html.indexOf('</style>'));

// ---- origin ----

test('normalizeOrigin accepts exactly an http(s) origin and nothing else', () => {
  assert.equal(normalizeOrigin('https://example.com'), 'https://example.com');
  assert.equal(normalizeOrigin('http://localhost:8080'), 'http://localhost:8080');
  assert.equal(normalizeOrigin('https://example.com/'), 'https://example.com', 'a trailing slash is tolerated');
  for (const wrong of ['*', 'null', '', 'example.com', 'file:///tmp/a.html', 'data:text/html,hi', 'javascript:alert(1)', 'https://example.com/page', 'https://example.com?x=1', 'https://example.com#top', 'https://user@example.com', 'https://EXAMPLE.com', 'https://example.com:443']) {
    assert.equal(normalizeOrigin(wrong), null, `rejected: ${JSON.stringify(wrong)}`);
  }
  for (const notAString of [undefined, null, 5, {}, ['https://example.com']]) assert.equal(normalizeOrigin(notAString), null);
});

test('parseEmbedParams: without embed=1 nothing is embedded, whatever else is there', () => {
  for (const search of ['', '?store=x', '?embed=0', '?embed=true', '?embed=', '?controls=0&theme=light&origin=https://a.com']) {
    const params = parseEmbedParams(search, 'https://a.com/page');
    assert.equal(params.embed, false, search);
    assert.equal(params.query, '');
    assert.equal(params.origin, null, 'the referrer is not read when not embedded');
    assert.deepEqual(embedAttributes(params), {});
  }
});

test('parseEmbedParams: defaults, controls=0 and theme=light', () => {
  const plain = parseEmbedParams('?embed=1&store=s');
  assert.deepEqual({ embed: plain.embed, controls: plain.controls, theme: plain.theme }, { embed: true, controls: true, theme: 'dark' });
  assert.equal(plain.query, 'embed=1', 'the store and the view are not embed parameters');
  const bare = parseEmbedParams('?embed=1&controls=0&theme=light');
  assert.deepEqual({ controls: bare.controls, theme: bare.theme }, { controls: false, theme: 'light' });
  assert.equal(bare.query, 'embed=1&controls=0&theme=light');
  assert.equal(parseEmbedParams('?embed=1&controls=1&theme=dark').query, 'embed=1', 'the defaults are not repeated');
  assert.equal(parseEmbedParams('?embed=1&controls=no').controls, true, 'only controls=0 hides the controls');
  assert.equal(parseEmbedParams('?embed=1&theme=sepia').theme, 'dark', 'an unknown theme is the default one');
});

test('parseEmbedParams: the origin is the origin parameter, else the origin of the referrer, else none', () => {
  const given = parseEmbedParams('?embed=1&origin=https%3A%2F%2Fhost.example', 'https://other.example/page');
  assert.deepEqual([given.origin, given.originSource, given.originError], ['https://host.example', 'param', null]);
  assert.equal(given.query, 'embed=1&origin=https%3A%2F%2Fhost.example');

  const fromReferrer = parseEmbedParams('?embed=1', 'https://host.example/some/page?x=1');
  assert.deepEqual([fromReferrer.origin, fromReferrer.originSource], ['https://host.example', 'referrer']);
  assert.equal(fromReferrer.query, 'embed=1', 'a referrer is not written into the address bar');

  for (const referrer of ['', 'about:blank', 'android-app://com.example', 'not a url']) {
    const none = parseEmbedParams('?embed=1', referrer);
    assert.deepEqual([none.origin, none.originSource, none.originError], [null, null, null], JSON.stringify(referrer));
  }
});

test('parseEmbedParams: a wrong origin parameter turns messaging off, it does not fall back to the referrer', () => {
  for (const wrong of ['*', 'https://host.example/path', 'host.example', '']) {
    const params = parseEmbedParams(`?embed=1&origin=${encodeURIComponent(wrong)}`, 'https://host.example/page');
    assert.equal(params.origin, null, `origin=${wrong}`);
    assert.match(params.originError, /not an http\(s\) origin/);
    assert.equal(params.originSource, null);
  }
});

test('the inline script of index.html sets the same attributes as embedAttributes', () => {
  const script = /<script>([\s\S]*?)<\/script>/.exec(html.slice(0, html.indexOf('<style>')))?.[1];
  assert.ok(script, 'index.html has an inline script before the stylesheet');
  for (const search of ['', '?store=x', '?embed=1', '?embed=1&controls=0', '?embed=1&theme=light', '?embed=1&controls=0&theme=light&t=3', '?embed=0&theme=light', '?controls=0&theme=light', '?embed=true', '?embed=1&controls=1&theme=dark']) {
    const dataset = {};
    vm.runInNewContext(script, { URLSearchParams, location: { search }, document: { documentElement: { dataset } } });
    assert.deepEqual({ ...dataset }, embedAttributes(parseEmbedParams(search)), search);
  }
});

// ---- who may talk to the viewer ----

test('isTrustedSender wants the allowed origin and the parent window, nothing else', () => {
  const parent = {};
  const ok = { origin: 'https://host.example', source: parent };
  assert.equal(isTrustedSender(ok, { origin: 'https://host.example', parent }), true);
  assert.equal(isTrustedSender({ ...ok, origin: 'https://evil.example' }, { origin: 'https://host.example', parent }), false);
  assert.equal(isTrustedSender({ ...ok, origin: 'https://host.example.evil.example' }, { origin: 'https://host.example', parent }), false, 'not a prefix match');
  assert.equal(isTrustedSender({ ...ok, source: {} }, { origin: 'https://host.example', parent }), false, 'a sibling frame or popup of the same origin');
  assert.equal(isTrustedSender({ ...ok, source: null }, { origin: 'https://host.example', parent }), false);
  for (const none of [null, undefined, '']) assert.equal(isTrustedSender({ origin: 'null', source: parent }, { origin: none, parent }), false, `no allowed origin: ${none}`);
  assert.equal(normalizeOrigin('null'), null, 'the opaque origin (a file: page, a sandboxed frame) can never be configured, so it is never trusted');
});

// ---- commands ----

test('parseCommand ignores what is not addressed to the viewer', () => {
  for (const data of [null, undefined, 'chronozarr:set', 42, [], ['chronozarr:set'], {}, { type: 5 }, { type: 'other:set', t: 1 }, { type: 'webpackOk' }]) {
    assert.deepEqual(parseCommand(data), { kind: 'ignore' }, JSON.stringify(data));
  }
  for (const name of ['ready', 'time', 'click', 'view', 'error']) {
    assert.deepEqual(parseCommand({ v: 1, type: `chronozarr:${name}` }), { kind: 'ignore' }, `a host that echoes ${name} back gets no error`);
  }
});

test('parseCommand: get, and the version check', () => {
  assert.deepEqual(parseCommand({ type: 'chronozarr:get' }), { kind: 'get' });
  assert.deepEqual(parseCommand({ v: 1, type: 'chronozarr:get' }), { kind: 'get' });
  assert.equal(EMBED_VERSION, 1);
  for (const v of [2, 0, '1', null]) {
    const result = parseCommand({ v, type: 'chronozarr:set', t: 1 });
    assert.equal(result.kind, 'error');
    assert.equal(result.code, 'bad_message');
    assert.match(result.message, /v .* is not supported: this viewer speaks v 1/);
  }
  const unknown = parseCommand({ type: 'chronozarr:fly' });
  assert.deepEqual([unknown.kind, unknown.code], ['error', 'bad_message']);
  assert.match(unknown.message, /"chronozarr:fly".*chronozarr:set/);
});

test('parseCommand: a set with every field', () => {
  const result = parseCommand({ v: 1, type: 'chronozarr:set', t: 3, product: 'ndvi', band: 'B04', zoom: 2.5, center: { lon: 3.1, lat: 36.2 }, playing: true, speed: 12 });
  assert.deepEqual(result, { kind: 'set', set: { t: { index: 3 }, product: 'ndvi', band: 'B04', zoom: 2.5, center: { lon: 3.1, lat: 36.2 }, playing: true, speed: 12 } });
});

test('parseCommand: an empty set is valid, unknown fields are ignored, v may be left out', () => {
  assert.deepEqual(parseCommand({ type: 'chronozarr:set' }), { kind: 'set', set: {} });
  assert.deepEqual(parseCommand({ type: 'chronozarr:set', t: 0, future: { a: 1 }, __proto__: { x: 1 } }), { kind: 'set', set: { t: { index: 0 } } });
});

test('parseCommand: t is an index or an ISO date, and nothing else', () => {
  assert.deepEqual(parseCommand({ type: 'chronozarr:set', t: 0 }).set.t, { index: 0 });
  assert.deepEqual(parseCommand({ type: 'chronozarr:set', t: '2024-03-01' }).set.t, { ms: Date.UTC(2024, 2, 1) });
  assert.deepEqual(parseCommand({ type: 'chronozarr:set', t: '2024-03-01T12:30:00Z' }).set.t, { ms: Date.UTC(2024, 2, 1, 12, 30) });
  for (const t of [-1, 1.5, NaN, Infinity, '3', 'March', '2024-13-45', '2024-02-30T99:00:00Z', null, true, [], {}]) {
    const result = parseCommand({ type: 'chronozarr:set', t });
    assert.deepEqual([result.kind, result.code], ['error', 'bad_set'], JSON.stringify(t));
    assert.match(result.message, /^t must be a timestep index/);
  }
});

test('parseIsoMs reads a date-time without a zone as UTC and honours an offset', () => {
  assert.equal(parseIsoMs('2024-03-01'), Date.UTC(2024, 2, 1));
  assert.equal(parseIsoMs('2024-03-01T00:00:00'), Date.UTC(2024, 2, 1));
  assert.equal(parseIsoMs('2024-03-01 06:00'), Date.UTC(2024, 2, 1, 6));
  assert.equal(parseIsoMs('2024-03-01T06:00:00+02:00'), Date.UTC(2024, 2, 1, 4));
  assert.equal(parseIsoMs('2024-03-01T06:00:00.250Z'), Date.UTC(2024, 2, 1, 6, 0, 0, 250));
  for (const wrong of ['', '2024', '24-03-01', 'now', 5, null]) assert.equal(parseIsoMs(wrong), null, String(wrong));
});

test('parseCommand: each field has its own error naming the field and the value', () => {
  const cases = [
    [{ product: '' }, /^product must be a non-empty string/],
    [{ product: 3 }, /^product must be a non-empty string, got 3/],
    [{ band: null }, /^band must be a non-empty string, got null/],
    [{ zoom: 0 }, /^zoom must be a number above 0/],
    [{ zoom: -2 }, /^zoom must be a number above 0/],
    [{ zoom: '2' }, /^zoom must be a number above 0.*"2"/],
    [{ zoom: NaN }, /^zoom must be a number above 0/],
    [{ center: [1, 2] }, /^center must be \{x, y\}/],
    [{ center: { lon: 3 } }, /^center must be \{x, y\}/],
    [{ center: { x: 1 } }, /^center must be \{x, y\}/],
    [{ center: { lon: 200, lat: 0 } }, /out of range/],
    [{ center: { lon: 0, lat: -91 } }, /out of range/],
    [{ playing: 1 }, /^playing must be true or false, got 1/],
    [{ speed: 0 }, /^speed must be a number above 0/],
    [{ speed: Infinity }, /^speed must be a number above 0/],
  ];
  for (const [fields, pattern] of cases) {
    const result = parseCommand({ type: 'chronozarr:set', ...fields });
    assert.deepEqual([result.kind, result.code], ['error', 'bad_set'], JSON.stringify(fields));
    assert.match(result.message, pattern, JSON.stringify(fields));
  }
});

test('parseCommand: a center is {x, y} or {lon, lat}; when a message carries both (the center of a view message) x and y win', () => {
  assert.deepEqual(parseCommand({ type: 'chronozarr:set', center: { x: 500000, y: 4000000 } }).set.center, { x: 500000, y: 4000000 });
  assert.deepEqual(parseCommand({ type: 'chronozarr:set', center: { lon: -73.5, lat: -9.4 } }).set.center, { lon: -73.5, lat: -9.4 });
  assert.deepEqual(parseCommand({ type: 'chronozarr:set', center: { x: 1, y: 2, lon: 3, lat: 4 } }).set.center, { x: 1, y: 2 });
  assert.deepEqual(parseCommand({ type: 'chronozarr:set', center: { x: 1, y: 2, lon: null, lat: null } }).set.center, { x: 1, y: 2 }, 'unknown lon/lat of a store without a CRS');
});

test('a set with one bad field is an error as a whole', () => {
  const result = parseCommand({ type: 'chronozarr:set', t: 2, zoom: -1 });
  assert.equal(result.kind, 'error');
  assert.equal(result.set, undefined);
});

// ---- time and the store ----

const TIMES = ['2024-01-01T00:00:00Z', '2024-02-01T00:00:00Z', '2024-03-01T00:00:00Z', '2024-04-01T00:00:00Z'];

test('nearestTime: the closest timestep, the earlier on a tie, the ends for dates outside the series', () => {
  assert.equal(nearestTime(TIMES, Date.UTC(2024, 1, 1)), 1, 'exact');
  assert.equal(nearestTime(TIMES, Date.UTC(2024, 1, 10)), 1, '9 days after February 1st is closer to it than to March 1st');
  assert.equal(nearestTime(TIMES, Date.UTC(2024, 1, 25)), 2, 'closer to March 1st');
  assert.equal(nearestTime(['2024-01-01T00:00:00Z', '2024-01-03T00:00:00Z'], Date.UTC(2024, 0, 2)), 0, 'a tie goes to the earlier');
  assert.equal(nearestTime(TIMES, Date.UTC(1999, 0, 1)), 0);
  assert.equal(nearestTime(TIMES, Date.UTC(2030, 0, 1)), 3);
});

const STORE = { times: TIMES, productIds: ['true_color', 'ndvi', 'band'], bandNames: ['B02', 'B03', 'B04', 'B08'] };

test('resolveSet turns a validated set into a plan against the store', () => {
  const resolved = resolveSet(
    { t: { ms: Date.UTC(2024, 1, 20) }, product: 'ndvi', band: 'B08', zoom: 2, center: { x: 1, y: 2 }, playing: false, speed: 6 },
    STORE,
  );
  assert.deepEqual(resolved, { ok: true, plan: { t: 2, productId: 'ndvi', bandName: 'B08', zoom: 2, center: { x: 1, y: 2 }, playing: false, speed: 6 } });
  assert.deepEqual(resolveSet({ t: { index: 3 } }, STORE), { ok: true, plan: { t: 3 } });
  assert.deepEqual(resolveSet({}, STORE), { ok: true, plan: {} });
});

test('resolveSet rejects what the store does not have, with the choices in the message', () => {
  const past = resolveSet({ t: { index: 4 } }, STORE);
  assert.equal(past.ok, false);
  assert.match(past.message, /t 4 is past the last timestep \(3\); the store has 4/);
  const product = resolveSet({ product: 'ndwi' }, STORE);
  assert.equal(product.ok, false);
  assert.match(product.message, /"ndwi" is not available.*true_color, ndvi, band/);
  const band = resolveSet({ band: 'B99' }, STORE);
  assert.equal(band.ok, false);
  assert.match(band.message, /"B99" is not in this store; bands: B02, B03, B04, B08/);
  assert.equal(resolveSet({ t: { index: 1 }, product: 'nope' }, STORE).ok, false, 'one bad field rejects the whole set');
});

// ---- where a pixel is ----

// The 200 x 200 synthetic stores: 10 m pixels, upper left corner at (500000, 4000000) in UTM zone 31 north.
const TRANSFORM = [10, 0, 500000, 0, -10, 4000000];

test('makeGeo with a transform and a projection: pixel <-> store coordinates <-> lon/lat', () => {
  const geo = makeGeo({ transform: TRANSFORM, projection: createProjection('EPSG:32631') });
  assert.equal(geo.geoReferenced, true);
  assert.equal(geo.hasLonLat, true);
  const corner = geo.fromPixel(0, 0);
  assert.deepEqual([corner.x, corner.y], [500000, 4000000]);
  assert.equal(corner.lon, 3, 'the false easting is the central meridian of zone 31');
  assert.ok(Math.abs(corner.lat - 36.137) < 0.01, `latitude of 4000000 m north: ${corner.lat}`);
  const middle = geo.fromPixel(100, 100);
  assert.deepEqual([middle.x, middle.y], [501000, 3999000]);

  const xy = geo.toPixel({ x: 501000, y: 3999000 });
  assert.deepEqual([xy.col, xy.row], [100, 100]);
  const lonLat = geo.toPixel({ lon: middle.lon, lat: middle.lat });
  assert.ok(Math.abs(lonLat.col - 100) < 0.01 && Math.abs(lonLat.row - 100) < 0.01, `round trip: ${JSON.stringify(lonLat)}`);
});

test('makeGeo without a transform: x and y are level-0 pixels, there is no lon/lat', () => {
  const geo = makeGeo({ projection: createProjection('EPSG:32631') });
  assert.equal(geo.geoReferenced, false);
  assert.equal(geo.hasLonLat, false);
  assert.deepEqual(geo.fromPixel(12.34, 56.78), { x: 12.3, y: 56.8, lon: null, lat: null });
  assert.deepEqual(geo.toPixel({ x: 12, y: 34 }), { col: 12, row: 34 });
  assert.throws(() => geo.toPixel({ lon: 3, lat: 36 }), /needs a georeferenced store.*level-0 pixels/);
});

test('makeGeo with a transform but a CRS the viewer cannot project: coordinates yes, lon/lat no, and it says so', () => {
  const geo = makeGeo({ transform: TRANSFORM, projection: null });
  assert.equal(geo.hasLonLat, false);
  assert.deepEqual(geo.fromPixel(0, 0), { x: 500000, y: 4000000, lon: null, lat: null });
  assert.deepEqual(geo.toPixel({ x: 500010, y: 3999990 }), { col: 1, row: 1 });
  assert.throws(() => geo.toPixel({ lon: 3, lat: 36 }), /WGS84 UTM, EPSG:3857 or EPSG:4326/);
});

// ---- messages ----

test('embedMessage puts the contract version and the type on every message', () => {
  assert.deepEqual(embedMessage('time', { t: 3 }), { v: 1, type: 'chronozarr:time', t: 3 });
  assert.deepEqual(embedMessage('ready'), { v: 1, type: 'chronozarr:ready' });
});

test('toPlainJson: undefined is dropped, NaN and Infinity become null, the result is a copy', () => {
  const source = { a: 1, b: undefined, c: NaN, d: [Infinity, -Infinity, 2], e: { f: null, g: undefined } };
  const plain = toPlainJson(source);
  assert.deepEqual(plain, { a: 1, c: null, d: [null, null, 2], e: { f: null } });
  assert.notEqual(plain, source);
});

// ---- the stylesheet ----

/** The declarations of the rule `selector` inside the text, as a sorted list of "property: value" with whitespace collapsed. */
function declarations(text, selector) {
  const start = text.search(new RegExp(`(^|[\\s}])${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{`, 'm'));
  assert.ok(start >= 0, `a rule for ${selector}`);
  const open = text.indexOf('{', start);
  const close = text.indexOf('}', open);
  return text
    .slice(open + 1, close)
    .split(';')
    .map((declaration) => declaration.replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .sort();
}

function mediaBlock(below) {
  const start = css.indexOf(`@media (width < ${below}px)`);
  assert.ok(start >= 0, `a ${below}px query exists`);
  let depth = 0;
  for (let i = css.indexOf('{', start); i < css.length; i++) {
    if (css[i] === '{') depth++;
    if (css[i] === '}' && --depth === 0) return css.slice(start, i + 1);
  }
  throw new Error('unbalanced CSS');
}

test('the embed layout has the drawer rules of the narrow layout at every width, declaration for declaration', () => {
  const narrow = mediaBlock(900);
  for (const selector of ['.sidebar', '.sidebar.open', '.inspector-head', '.inspector-close', '.inspector-close:hover']) {
    assert.deepEqual(declarations(css, `html[data-embed] ${selector}`), declarations(narrow, selector), selector);
  }
});

test('the embed layout drops the catalog selector and the export controls, and has a wordmark that opens in a new tab', () => {
  const hidden = css.slice(css.indexOf('html[data-embed] .brand'), css.indexOf('html[data-embed] .embed-wordmark {'));
  for (const selector of ['.brand', '.nav-sep', '#catalog-select', '.docs-link', '#export-btn', '#export-panel']) assert.ok(hidden.includes(`html[data-embed] ${selector}`), selector);
  assert.match(hidden, /display: none/);
  assert.match(html, /<a id="embed-wordmark"[^>]*target="_blank"[^>]*rel="noopener"[^>]*>chrono<span>zarr<\/span><\/a>/);
  assert.match(css, /\.embed-wordmark \{ display: none; \}/, 'the wordmark is for the embed only');
});

test('controls=0 hides the header, the inspector and every control but the time label', () => {
  assert.match(css, /html\[data-controls="0"\] nav,\s*html\[data-controls="0"\] \.sidebar,\s*html\[data-controls="0"\] \.click-hint \{ display: none; \}/);
  assert.match(css, /html\[data-controls="0"\] \.timeline-bar > :not\(\.month-nav\) \{ display: none; \}/);
  assert.match(css, /html\[data-controls="0"\] \.month-nav button \{ display: none; \}/);
});

test('the light palette redefines every colour of the dark one; the canvas keeps its own', () => {
  const names = (text) => [...text.matchAll(/--([a-z0-9-]+):/g)].map((m) => m[1]);
  const rootStart = css.indexOf(':root {');
  const root = css.slice(rootStart, css.indexOf('}', rootStart));
  const lightStart = css.indexOf(':root[data-theme="light"] {');
  const light = css.slice(lightStart, css.indexOf('}', lightStart));
  const shared = new Set(['radius', 'canvas-bg']);
  assert.deepEqual(names(light).sort(), names(root).filter((name) => !shared.has(name)).sort(), 'a colour added to :root needs a light value');
  assert.doesNotMatch(light, /--canvas-bg/, 'the canvas is not part of the palette');
  assert.match(css, /#gl-canvas \{[^}]*background: var\(--canvas-bg\)/);
});

test('the chart lines and legend use the palette variables, so they stay legible on the light chrome', () => {
  const chart = readFileSync(new URL('../demo/chart.js', import.meta.url), 'utf8');
  assert.doesNotMatch(chart, /COLORS = \{[^}]*#[0-9a-f]{6}/i);
  for (const series of ['red', 'green', 'blue', 'amber', 'grey']) assert.ok(css.includes(`--series-${series}:`), series);
});

test('physical display limits validate atomically and survive resolution', () => {
  for (const range of [[0, 0], [2, 1], [NaN, 1], [0], 'bad']) {
    assert.equal(parseCommand({type: 'chronozarr:set', t: 1, range}).kind, 'error');
  }
  for (const range of [null, [-25, 0]]) {
    const command = parseCommand({type: 'chronozarr:set', range});
    assert.equal(command.kind, 'set');
    assert.deepEqual(resolveSet(command.set, {times: [], productIds: [], bandNames: []}).plan.range, range);
  }
});
