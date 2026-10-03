// The bridge of the embedding contract (EmbedBridge) and the glue between it and the viewer (connectEmbed), against a
// fake window and a fake viewer: who is listened to, what is sent where, in what order commands run, and what a set does.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createProjection } from '../maplibre/projection.js';
import { EmbedBridge, EmbedCommandError, connectEmbed, parseEmbedParams } from '../demo/embed.js';

const HOST = 'https://host.example';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** A window inside a frame: `parent` records what is posted to it; `receive` delivers a message event to the listeners. */
function fakeWindow() {
  const listeners = new Set();
  const posted = [];
  const parent = { postMessage: (data, targetOrigin) => posted.push({ data, targetOrigin }) };
  const win = {
    parent,
    location: { href: 'https://chronozarr.org/demo/index.html?embed=1' },
    addEventListener: (type, listener) => type === 'message' && listeners.add(listener),
    removeEventListener: (type, listener) => type === 'message' && listeners.delete(listener),
  };
  return {
    win,
    parent,
    posted,
    listenerCount: () => listeners.size,
    /** Deliver a message as the browser would: `origin` and `source` default to the host and the parent window. */
    receive: (data, { origin = HOST, source = parent } = {}) => listeners.forEach((listener) => listener({ data, origin, source })),
  };
}

const setMessage = (fields) => ({ v: 1, type: 'chronozarr:set', ...fields });

test('a bridge is inactive without an origin or outside a frame: no listener, nothing sent', () => {
  const noOrigin = fakeWindow();
  const bridge = new EmbedBridge({ win: noOrigin.win, origin: null, onCommand: () => assert.fail('no command') });
  assert.equal(bridge.active, false);
  bridge.start();
  bridge.post('time', { t: 1 });
  bridge.scheduleView(() => assert.fail('no view'));
  assert.equal(noOrigin.listenerCount(), 0);
  assert.deepEqual(noOrigin.posted, []);

  const top = fakeWindow();
  top.win.parent = top.win;
  const unframed = new EmbedBridge({ win: top.win, origin: HOST, onCommand: () => assert.fail('no command') });
  assert.equal(unframed.active, false, 'a page that is its own parent has nobody to talk to');
  unframed.start();
  assert.equal(top.listenerCount(), 0);
});

test('messages from another origin are ignored without any side effect, valid or not', async () => {
  const w = fakeWindow();
  const commands = [];
  const bridge = new EmbedBridge({ win: w.win, origin: HOST, onCommand: (command) => commands.push(command) });
  bridge.start();
  for (const data of [setMessage({ t: 1 }), { type: 'chronozarr:get' }, setMessage({ zoom: -1 }), { type: 'chronozarr:nonsense' }, setMessage({ v: 9 }), 'junk']) {
    w.receive(data, { origin: 'https://evil.example' });
    w.receive(data, { origin: 'https://host.example.evil.example' });
    w.receive(data, { origin: 'null' });
    w.receive(data, { origin: '' });
  }
  await sleep(10);
  assert.deepEqual(commands, [], 'no command reached the viewer');
  assert.deepEqual(w.posted, [], 'no error, no reply, nothing: the other origin learns nothing from the viewer either');
});

test('a message from the right origin but another window is ignored (a sibling frame, a popup)', async () => {
  const w = fakeWindow();
  const commands = [];
  new EmbedBridge({ win: w.win, origin: HOST, onCommand: (command) => commands.push(command) }).start();
  w.receive(setMessage({ t: 1 }), { source: {} });
  w.receive(setMessage({ t: 1 }), { source: null });
  await sleep(10);
  assert.deepEqual(commands, []);
  assert.deepEqual(w.posted, []);
});

test('commands from the host are validated, handed over in order, one at a time', async () => {
  const w = fakeWindow();
  const log = [];
  const bridge = new EmbedBridge({
    win: w.win,
    origin: HOST,
    onCommand: async (command) => {
      log.push(`start ${command.kind} ${JSON.stringify(command.set ?? null)}`);
      await sleep(command.set?.t?.index === 1 ? 20 : 1);
      log.push(`end ${command.kind}`);
    },
  });
  bridge.start();
  w.receive(setMessage({ t: 1 }));
  w.receive({ type: 'chronozarr:get' });
  w.receive(setMessage({ t: 2, speed: 5 }));
  await sleep(80);
  assert.deepEqual(log, ['start set {"t":{"index":1}}', 'end set', 'start get null', 'end get', 'start set {"t":{"index":2},"speed":5}', 'end set']);
  assert.deepEqual(w.posted, []);
});

test('a malformed command from the host is answered with chronozarr:error, sent to the host origin only', () => {
  const w = fakeWindow();
  new EmbedBridge({ win: w.win, origin: HOST, onCommand: () => assert.fail('a bad command is not handed over') }).start();
  w.receive(setMessage({ zoom: 'big' }));
  w.receive({ type: 'chronozarr:fly' });
  w.receive({ v: 2, type: 'chronozarr:set' });
  w.receive({ type: 'somebody:else' });
  w.receive({ v: 1, type: 'chronozarr:ready' });
  assert.equal(w.posted.length, 3, 'foreign messages and echoes of the viewer\'s own types get no answer');
  assert.ok(w.posted.every(({ targetOrigin }) => targetOrigin === HOST), 'never "*"');
  assert.deepEqual(w.posted.map(({ data }) => [data.v, data.type, data.code]), [[1, 'chronozarr:error', 'bad_set'], [1, 'chronozarr:error', 'bad_message'], [1, 'chronozarr:error', 'bad_message']]);
  assert.match(w.posted[0].data.message, /^zoom must be a number above 0/);
});

test('a command that fails is answered with chronozarr:error, and the next one still runs', async () => {
  const w = fakeWindow();
  const seen = [];
  const original = console.error;
  const logged = [];
  console.error = (...args) => logged.push(args.join(' '));
  try {
    new EmbedBridge({
      win: w.win,
      origin: HOST,
      onCommand: (command) => {
        seen.push(command.set?.t?.index);
        if (command.set.t.index === 1) throw new EmbedCommandError('bad_set', 'no such timestep');
        if (command.set.t.index === 2) throw new Error('boom');
      },
    }).start();
    for (const t of [1, 2, 3]) w.receive(setMessage({ t }));
    await sleep(20);
  } finally {
    console.error = original;
  }
  assert.deepEqual(seen, [1, 2, 3]);
  assert.deepEqual(w.posted.map(({ data }) => [data.code, data.message]), [['bad_set', 'no such timestep'], ['internal', 'boom']]);
  assert.equal(logged.length, 1, 'only the unexpected failure is logged');
});

test('post sends plain JSON with the version to the host origin', () => {
  const w = fakeWindow();
  const bridge = new EmbedBridge({ win: w.win, origin: HOST, onCommand: () => {} });
  bridge.post('click', { value: NaN, gone: undefined, nested: { inf: Infinity } });
  assert.deepEqual(w.posted, [{ data: { v: 1, type: 'chronozarr:click', value: null, nested: { inf: null } }, targetOrigin: HOST }]);
});

test('scheduleView sends one message after the camera has been still, and not again for the same view', async () => {
  const w = fakeWindow();
  const bridge = new EmbedBridge({ win: w.win, origin: HOST, onCommand: () => {}, debounceMs: 15 });
  let zoom = 1;
  const build = () => ({ zoom, center: { x: 1, y: 2 } });
  for (let i = 0; i < 5; i++) {
    zoom = 1 + i;
    bridge.scheduleView(build);
    await sleep(3);
  }
  assert.equal(w.posted.length, 0, 'still moving: nothing yet');
  await sleep(40);
  assert.deepEqual(w.posted.map(({ data }) => data.zoom), [5]);
  bridge.scheduleView(build);
  await sleep(40);
  assert.equal(w.posted.length, 1, 'the same view is not sent twice');
  zoom = 6;
  bridge.scheduleView(build);
  await sleep(40);
  assert.deepEqual(w.posted.map(({ data }) => data.zoom), [5, 6]);
});

test('stop removes the listener and cancels a pending view', async () => {
  const w = fakeWindow();
  const bridge = new EmbedBridge({ win: w.win, origin: HOST, onCommand: () => assert.fail('stopped'), debounceMs: 5 });
  bridge.start();
  bridge.scheduleView(() => ({ zoom: 1 }));
  bridge.stop();
  w.receive(setMessage({ t: 1 }));
  await sleep(30);
  assert.equal(w.listenerCount(), 0);
  assert.deepEqual(w.posted, []);
});

// ---- connectEmbed against a fake viewer ----

const GEOREF = [10, 0, 500000, 0, -10, 4000000];

function fakeViewer({ transform = GEOREF, crs = 'EPSG:32631' } = {}) {
  const calls = [];
  const viewer = {
    hooks: {},
    store: {
      url: 'https://data.example/stores/ucayali/',
      crs,
      transform,
      times: ['2024-01-01T00:00:00Z', '2024-02-01T00:00:00Z', '2024-03-01T00:00:00Z', '2024-04-01T00:00:00Z'],
      levels: [
        { lod: 0, width: 200, height: 200, resolution: 10 },
        { lod: 1, width: 100, height: 100, resolution: 20 },
      ],
    },
    bands: [
      { name: 'B02', scale: 1e-4, offset: 0, units: 'reflectance' },
      { name: 'B04', common_name: 'red', scale: 1e-4, offset: 0 },
    ],
    products: [
      { id: 'true_color', name: 'True color', available: false },
      { id: 'ndvi', name: 'NDVI', available: true },
      { id: 'band', name: 'Single band', available: true },
    ],
    productIndex: 1,
    bandChoice: 0,
    t: 0,
    camera: { cx: 100, cy: 100, scale: 1 },
    zoom: 1,
    speed: 4,
    playback: { playing: false },
    goToTime(t) {
      calls.push(['goToTime', t]);
      viewer.t = t;
    },
    setProduct: (index) => calls.push(['setProduct', index]),
    setBandChoice: (index) => calls.push(['setBandChoice', index]),
    setView: (view) => calls.push(['setView', view]),
    setSpeed: (speed) => calls.push(['setSpeed', speed]),
    play: () => calls.push(['play']),
    pause: () => calls.push(['pause']),
  };
  return { viewer, calls };
}

const realProjection = async (crs) => createProjection(crs);

function connected(options = {}, viewerOptions = {}) {
  const w = fakeWindow();
  const { viewer, calls } = fakeViewer(viewerOptions);
  const params = parseEmbedParams(`?embed=1&origin=${encodeURIComponent(HOST)}`);
  const bridge = connectEmbed(viewer, params, { win: w.win, loadProjection: realProjection, ...options });
  return { w, viewer, calls, bridge };
}

test('connectEmbed: ready carries the store, its times, bands, products, levels and the current state', async () => {
  const { w, viewer } = connected();
  viewer.hooks.ready();
  await sleep(5);
  assert.equal(w.posted.length, 1);
  const { data, targetOrigin } = w.posted[0];
  assert.equal(targetOrigin, HOST);
  assert.deepEqual([data.v, data.type], [1, 'chronozarr:ready']);
  assert.deepEqual(data.store, { url: 'https://data.example/stores/ucayali/', name: 'ucayali', crs: 'EPSG:32631' });
  assert.equal(data.times.length, 4);
  assert.deepEqual(data.bands, [
    { name: 'B02', common_name: null, units: 'reflectance', scale: 1e-4, offset: 0 },
    { name: 'B04', common_name: 'red', units: null, scale: 1e-4, offset: 0 },
  ]);
  assert.deepEqual(data.products, [{ id: 'true_color', name: 'True color', available: false }, { id: 'ndvi', name: 'NDVI', available: true }, { id: 'band', name: 'Single band', available: true }]);
  assert.deepEqual(data.levels, [{ lod: 0, width: 200, height: 200, resolution: 10 }, { lod: 1, width: 100, height: 100, resolution: 20 }]);
  assert.equal(data.state.t, 0);
  assert.equal(data.state.time, '2024-01-01T00:00:00Z');
  assert.equal(data.state.product, 'ndvi');
  assert.equal(data.state.band, 'B02');
  assert.equal(data.state.zoom, 1);
  assert.equal(data.state.playing, false);
  assert.equal(data.state.speed, 4);
  assert.deepEqual([data.state.center.x, data.state.center.y, data.state.center.lon], [501000, 3999000, data.state.center.lon]);
  assert.ok(Math.abs(data.state.center.lat - 36.1) < 0.1);
});

test('connectEmbed: get answers with ready again once the store is open, and with nothing before', async () => {
  const { w, viewer } = connected();
  w.receive({ type: 'chronozarr:get' });
  await sleep(5);
  assert.deepEqual(w.posted, [], 'the ready message is still to come');
  viewer.hooks.ready();
  await sleep(5);
  viewer.t = 2;
  w.receive({ type: 'chronozarr:get' });
  await sleep(5);
  assert.deepEqual(w.posted.map(({ data }) => [data.type, data.state.t]), [['chronozarr:ready', 0], ['chronozarr:ready', 2]]);
});

test('connectEmbed: time, view and click messages wait for ready and carry lon/lat and the values', async () => {
  const { w, viewer } = connected();
  viewer.hooks.time({ t: 1 });
  viewer.hooks.click({ pixel: { x: 1, y: 1 }, t: 1, lod: 0, info: { valid: true, bands: [] } });
  assert.deepEqual(w.posted, [], 'nothing before ready');
  viewer.hooks.ready();
  await sleep(5);
  w.posted.length = 0;

  viewer.hooks.time({ t: 3 });
  assert.deepEqual(w.posted.map(({ data }) => data), [{ v: 1, type: 'chronozarr:time', t: 3, time: '2024-04-01T00:00:00Z' }]);

  w.posted.length = 0;
  viewer.hooks.click({
    pixel: { x: 10, y: 20 },
    t: 2,
    lod: 0,
    info: { valid: true, bands: [{ name: 'B02', value: 0.0412 }, { name: 'B04', value: 0.0633 }] },
  });
  const click = w.posted[0].data;
  assert.deepEqual([click.type, click.pixel, click.t, click.time, click.level, click.valid], ['chronozarr:click', { x: 10, y: 20 }, 2, '2024-03-01T00:00:00Z', 0, true]);
  assert.deepEqual(click.values, { B02: 0.0412, B04: 0.0633 });
  // The centre of pixel (10, 20): 500000 + 10.5 * 10 east, 4000000 - 20.5 * 10 north in UTM 31N.
  const expected = createProjection('EPSG:32631').toLonLat(500105, 3999795);
  assert.ok(Math.abs(click.lon - expected[0]) < 1e-6 && Math.abs(click.lat - expected[1]) < 1e-6, `lon/lat ${click.lon}, ${click.lat} vs ${expected}`);

  w.posted.length = 0;
  viewer.hooks.click({ pixel: { x: 0, y: 0 }, t: 0, lod: 1, info: { valid: false, bands: [{ name: 'B02', value: 0 }, { name: 'B04', value: NaN }] } });
  assert.deepEqual([w.posted[0].data.valid, w.posted[0].data.values, w.posted[0].data.level], [false, { B02: null, B04: null }, 1], 'no values for a pixel without data');
});

test('connectEmbed: errors of the viewer reach the host at once, even before ready', () => {
  const { w, viewer } = connected();
  viewer.hooks.error({ code: 'store_open_failed', title: 'Could not open store', message: 'HTTP 404' });
  assert.deepEqual(w.posted.map(({ data }) => [data.type, data.code, data.message]), [['chronozarr:error', 'store_open_failed', 'Could not open store: HTTP 404']]);
});

test('connectEmbed: a set is applied in a fixed order: product, band, time, camera, speed, playback', async () => {
  const { w, viewer, calls } = connected();
  viewer.hooks.ready();
  await sleep(5);
  w.posted.length = 0;
  w.receive(setMessage({ playing: true, speed: 12, center: { x: 501000, y: 3999000 }, zoom: 3, t: '2024-03-10', band: 'B04', product: 'band' }));
  await sleep(10);
  assert.deepEqual(calls, [
    ['setProduct', 2],
    ['setBandChoice', 1],
    ['goToTime', 2],
    ['setView', { zoom: 3, center: { col: 100, row: 100 } }],
    ['setSpeed', 12],
    ['play'],
  ]);
  assert.deepEqual(w.posted, [], 'a good set is not answered; the viewer\'s own events (time, view) follow from the changes');
});

test('connectEmbed: playing false pauses; a center in lon/lat goes through the projection', async () => {
  const { w, viewer, calls } = connected();
  viewer.hooks.ready();
  await sleep(5);
  const [lon, lat] = createProjection('EPSG:32631').toLonLat(500500, 3999500);
  w.receive(setMessage({ playing: false, center: { lon, lat } }));
  await sleep(10);
  assert.deepEqual(calls[0], ['setView', { zoom: undefined, center: calls[0][1].center }]);
  assert.ok(Math.abs(calls[0][1].center.col - 50) < 0.01 && Math.abs(calls[0][1].center.row - 50) < 0.01, JSON.stringify(calls[0]));
  assert.deepEqual(calls[1], ['pause']);
});

test('connectEmbed: a set the store cannot satisfy changes nothing and is answered with bad_set', async () => {
  const { w, viewer, calls } = connected();
  viewer.hooks.ready();
  await sleep(5);
  w.posted.length = 0;
  w.receive(setMessage({ t: 1, product: 'true_color' }));
  w.receive(setMessage({ t: 9 }));
  w.receive(setMessage({ t: 1, band: 'B99' }));
  await sleep(10);
  assert.deepEqual(calls, [], 'not even the time, which was fine on its own');
  assert.deepEqual(w.posted.map(({ data }) => data.code), ['bad_set', 'bad_set', 'bad_set']);
  assert.match(w.posted[0].data.message, /"true_color" is not available for this store; available: ndvi, band/);
  assert.match(w.posted[1].data.message, /past the last timestep/);
  assert.match(w.posted[2].data.message, /bands: B02, B04/);
});

test('connectEmbed: lon/lat for a store with no transform is an error and moves nothing; x and y are level-0 pixels there', async () => {
  const { w, viewer, calls } = connected({}, { transform: null });
  viewer.hooks.ready();
  await sleep(5);
  w.posted.length = 0;
  w.receive(setMessage({ t: 1, center: { lon: 3, lat: 36 } }));
  await sleep(10);
  assert.deepEqual(calls, []);
  assert.match(w.posted[0].data.message, /needs a georeferenced store/);
  w.receive(setMessage({ center: { x: 40, y: 60 } }));
  await sleep(10);
  assert.deepEqual(calls, [['setView', { zoom: undefined, center: { col: 40, row: 60 } }]]);
});

test('connectEmbed: a store whose CRS cannot be projected still reports and accepts coordinates, without lon/lat', async () => {
  // The default loader (a dynamic import of maplibre/projection.js) is the one that turns an unsupported CRS into "no lon/lat".
  const { w, viewer, calls } = connected({ loadProjection: undefined }, { crs: 'EPSG:2056' });
  const original = console.warn;
  const warned = [];
  console.warn = (...args) => warned.push(args.join(' '));
  try {
    viewer.hooks.ready();
    await sleep(5);
  } finally {
    console.warn = original;
  }
  const center = w.posted[0].data.state.center;
  assert.deepEqual([center.x, center.y, center.lon, center.lat], [501000, 3999000, null, null]);
  assert.equal(warned.length, 1);
  assert.match(warned[0], /no lon\/lat for this store.*unsupported store CRS "EPSG:2056"/);
  w.receive(setMessage({ center: { x: 501000, y: 3999000 } }));
  await sleep(5);
  assert.equal(calls[0][0], 'setView');
});

test('connectEmbed: sets that arrive before the store has opened are merged and applied right after ready', async () => {
  const { w, viewer, calls } = connected();
  w.receive(setMessage({ t: 1, speed: 6 }));
  w.receive(setMessage({ t: 2, zoom: 4 }));
  await sleep(5);
  assert.deepEqual(calls, [], 'nothing to apply them to yet');
  viewer.hooks.ready();
  await sleep(10);
  assert.equal(w.posted[0].data.type, 'chronozarr:ready');
  assert.deepEqual(calls, [['goToTime', 2], ['setView', { zoom: 4, center: undefined }], ['setSpeed', 6]], 'later fields win');
});

test('connectEmbed: a camera change that leaves the view as ready reported it sends no view', async () => {
  const w = fakeWindow();
  const { viewer } = fakeViewer();
  connectEmbed(viewer, parseEmbedParams(`?embed=1&origin=${encodeURIComponent(HOST)}`), { win: w.win, loadProjection: realProjection });
  viewer.hooks.ready();
  await sleep(5);
  w.posted.length = 0;
  viewer.hooks.view();
  await sleep(250);
  assert.deepEqual(w.posted, [], 'a resize that refits to the same zoom and centre says nothing new');
  viewer.zoom = 1.5;
  viewer.hooks.view();
  await sleep(250);
  assert.deepEqual(w.posted.map(({ data }) => data.zoom), [1.5]);
});

test('connectEmbed: view messages follow camera changes, debounced', async () => {
  const w = fakeWindow();
  const { viewer } = fakeViewer();
  connectEmbed(viewer, parseEmbedParams(`?embed=1&origin=${encodeURIComponent(HOST)}`), { win: w.win, loadProjection: realProjection });
  viewer.hooks.ready();
  await sleep(5);
  w.posted.length = 0;
  viewer.zoom = 2;
  viewer.hooks.view();
  viewer.zoom = 3;
  viewer.hooks.view();
  assert.deepEqual(w.posted, []);
  await sleep(250);
  assert.deepEqual(w.posted.map(({ data }) => [data.type, data.zoom]), [['chronozarr:view', 3]]);
  assert.deepEqual(Object.keys(w.posted[0].data.center).sort(), ['lat', 'lon', 'x', 'y']);
});

test('connectEmbed: returns null and says why when the viewer has nobody to talk to', () => {
  const warned = [];
  const original = console.warn;
  console.warn = (...args) => warned.push(args.join(' '));
  try {
    const noOrigin = fakeWindow();
    assert.equal(connectEmbed(fakeViewer().viewer, parseEmbedParams('?embed=1', ''), { win: noOrigin.win }), null);
    assert.equal(warned.length, 1);
    assert.match(warned[0], /no origin= parameter and no referrer.*Add &origin=/);

    warned.length = 0;
    const wrong = fakeWindow();
    assert.equal(connectEmbed(fakeViewer().viewer, parseEmbedParams('?embed=1&origin=*', ''), { win: wrong.win }), null);
    assert.equal(warned.length, 1);
    assert.match(warned[0], /origin="\*" is not an http\(s\) origin.*postMessage API is off/);

    warned.length = 0;
    const top = fakeWindow();
    top.win.parent = top.win;
    assert.equal(connectEmbed(fakeViewer().viewer, parseEmbedParams('?embed=1', ''), { win: top.win }), null);
    assert.deepEqual(warned, [], 'opened on its own (the wordmark link, a bookmark): nothing to warn about');

    const viaReferrer = fakeWindow();
    assert.ok(connectEmbed(fakeViewer().viewer, parseEmbedParams('?embed=1', 'https://host.example/page'), { win: viaReferrer.win }), 'the referrer origin is enough');
  } finally {
    console.warn = original;
  }
});
