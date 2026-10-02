// The embedding contract of the viewer: the URL parameters of ?embed=1, the postMessage protocol between the
// host page and the viewer in its iframe, and the bridge that speaks it. docs/embedding.md is the reference.
//
//   ?embed=1            compact layout: no catalog selector, no export, the inspector as a drawer opened by a click
//   &controls=0         only the canvas and a thin time readout
//   &theme=light        light palette for the chrome (the canvas is untouched)
//   &origin=<origin>    the origin of the host page: the only origin whose messages are read, and the only one
//                       messages are sent to. Absent: the origin of document.referrer. Neither: no messaging.
//
// Everything that decides something is a pure function (parameters, origin check, command validation, time lookup,
// pixel <-> lon/lat, message shapes), so node tests cover it. EmbedBridge and connectEmbed are the thin parts that
// touch window and the viewer.

import { pixelToProjected, projectedToPixel } from './permalink.js';

/** Contract version, in the `v` field of every message the viewer sends; a command may carry it and then must say 1. */
export const EMBED_VERSION = 1;

const PREFIX = 'tileripper:';
/** Types the viewer sends. A host that echoes messages back at the iframe must not get errors for them. */
const OUTGOING_TYPES = new Set(['ready', 'time', 'click', 'view', 'error'].map((name) => PREFIX + name));

const ISO_TIME = /^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/;
const HAS_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/;

// ---- URL parameters ----

/** `https://example.com:8080` for a value that is exactly an http(s) origin (a trailing slash is tolerated), else null. */
export function normalizeOrigin(value) {
  if (typeof value !== 'string') return null;
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  const bare = value.endsWith('/') ? value.slice(0, -1) : value;
  return url.origin === bare ? url.origin : null;
}

/** The origin of a URL such as document.referrer, if it is http(s); null for '' (no referrer) and other schemes. */
function originOfUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.origin : null;
  } catch {
    return null;
  }
}

/**
 * The embed parameters of a query string. `origin` is the one origin the viewer talks to: the `origin` parameter
 * when given (if it is not a valid origin the result is `origin: null` with `originError`, and there is no fallback to
 * the referrer: a wrong explicit value must not silently trust something else), else the origin of `referrer`.
 * `query` is the embed part of the query string, to keep in the address bar next to the view state.
 */
export function parseEmbedParams(search, referrer = '') {
  const params = new URLSearchParams(search);
  const off = { embed: false, controls: true, theme: 'dark', origin: null, originSource: null, originError: null, query: '' };
  if (params.get('embed') !== '1') return off;
  const controls = params.get('controls') !== '0';
  const theme = params.get('theme') === 'light' ? 'light' : 'dark';
  const kept = new URLSearchParams({ embed: '1' });
  if (!controls) kept.set('controls', '0');
  if (theme === 'light') kept.set('theme', 'light');
  let origin = null;
  let originSource = null;
  let originError = null;
  if (params.has('origin')) {
    const given = params.get('origin');
    origin = normalizeOrigin(given);
    if (origin) originSource = 'param';
    else originError = `origin=${JSON.stringify(given)} is not an http(s) origin such as "https://example.com" (no path, no "*").`;
    kept.set('origin', given);
  } else {
    origin = originOfUrl(referrer);
    if (origin) originSource = 'referrer';
  }
  return { embed: true, controls, theme, origin, originSource, originError, query: kept.toString() };
}

/** The attributes for <html> that the stylesheet keys the embed layout and the light palette on (the inline script of index.html sets the same ones before first paint). */
export function embedAttributes({ embed, controls, theme }) {
  if (!embed) return {};
  return { embed: '1', ...(controls ? {} : { controls: '0' }), ...(theme === 'light' ? { theme: 'light' } : {}) };
}

export function applyEmbedAttributes(root, params) {
  for (const [name, value] of Object.entries(embedAttributes(params))) root.dataset[name] = value;
}

// ---- who may talk to the viewer ----

/**
 * Whether a message event comes from the host: its origin is the allowed one and its source is the parent window
 * (not a sibling frame or a popup of the same origin). With no allowed origin nothing is trusted.
 */
export function isTrustedSender(event, { origin, parent }) {
  return typeof origin === 'string' && origin !== '' && event.origin === origin && event.source === parent;
}

// ---- commands from the host ----

const isPlainObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
/** A value of a message as it is quoted back to the host in an error. */
function show(value) {
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}
const bad = (message) => ({ kind: 'error', code: 'bad_set', message });

/** Milliseconds since the epoch of an ISO 8601 date or date-time, read as UTC when it names no zone; null if it is not one. */
export function parseIsoMs(text) {
  if (typeof text !== 'string' || !ISO_TIME.test(text)) return null;
  const zoned = text.includes('T') || text.includes(' ') ? (HAS_ZONE.test(text) ? text : `${text}Z`) : text;
  const ms = Date.parse(zoned.replace(' ', 'T'));
  return Number.isFinite(ms) ? ms : null;
}

/**
 * Structural check of a message from the host. Returns
 *   {kind: 'ignore'}                 not ours (another protocol on the same window, or one of the viewer's own types)
 *   {kind: 'get'}
 *   {kind: 'set', set}               every field present is valid in itself; `set.t` is {index} or {ms}
 *   {kind: 'error', code, message}   ours, but wrong (code 'bad_message' or 'bad_set')
 * Unknown fields of a set are ignored, so a later version can add some. Nothing is checked against the store here (resolveSet does).
 */
export function parseCommand(data) {
  if (!isPlainObject(data) || typeof data.type !== 'string' || !data.type.startsWith(PREFIX) || OUTGOING_TYPES.has(data.type)) return { kind: 'ignore' };
  if (data.v !== undefined && data.v !== EMBED_VERSION) {
    return { kind: 'error', code: 'bad_message', message: `v ${show(data.v)} is not supported: this viewer speaks v ${EMBED_VERSION}.` };
  }
  if (data.type === `${PREFIX}get`) return { kind: 'get' };
  if (data.type !== `${PREFIX}set`) return { kind: 'error', code: 'bad_message', message: `unknown message type ${show(data.type)}; commands are "tileripper:set" and "tileripper:get".` };

  const set = {};
  if (data.t !== undefined) {
    if (Number.isInteger(data.t) && data.t >= 0) set.t = { index: data.t };
    else if (parseIsoMs(data.t) !== null) set.t = { ms: parseIsoMs(data.t) };
    else return bad(`t must be a timestep index (a non-negative integer) or an ISO 8601 date such as "2024-03-01", got ${show(data.t)}.`);
  }
  for (const field of ['product', 'band']) {
    if (data[field] === undefined) continue;
    if (typeof data[field] !== 'string' || data[field] === '') return bad(`${field} must be a non-empty string, got ${show(data[field])}.`);
    set[field] = data[field];
  }
  if (data.range !== undefined) {
    if (data.range !== null && (!Array.isArray(data.range) || data.range.length !== 2 || !data.range.every(Number.isFinite) || data.range[0] >= data.range[1])) return bad("range must be null or two finite increasing physical limits.");
    set.range = data.range;
  }
  if (data.zoom !== undefined) {
    if (typeof data.zoom !== 'number' || !Number.isFinite(data.zoom) || data.zoom <= 0) return bad(`zoom must be a number above 0 (CSS pixels per level-0 data pixel), got ${show(data.zoom)}.`);
    set.zoom = data.zoom;
  }
  if (data.center !== undefined) {
    const c = data.center;
    const finite = (value) => typeof value === 'number' && Number.isFinite(value);
    if (isPlainObject(c) && finite(c.x) && finite(c.y)) set.center = { x: c.x, y: c.y };
    else if (isPlainObject(c) && finite(c.lon) && finite(c.lat)) {
      if (Math.abs(c.lon) > 180 || Math.abs(c.lat) > 90) return bad(`center lon/lat is out of range (lon -180..180, lat -90..90), got ${show(c)}.`);
      set.center = { lon: c.lon, lat: c.lat };
    } else return bad(`center must be {x, y} in the store's coordinates or {lon, lat} in degrees, got ${show(c)}.`);
  }
  if (data.playing !== undefined) {
    if (typeof data.playing !== 'boolean') return bad(`playing must be true or false, got ${show(data.playing)}.`);
    set.playing = data.playing;
  }
  if (data.speed !== undefined) {
    if (typeof data.speed !== 'number' || !Number.isFinite(data.speed) || data.speed <= 0) return bad(`speed must be a number above 0 (timesteps per second), got ${show(data.speed)}.`);
    set.speed = data.speed;
  }
  return { kind: 'set', set };
}

/** Index of the time closest to `ms`; the earlier one on a tie. `times` are ISO strings; the ends win for a date outside the series. */
export function nearestTime(times, ms) {
  let best = 0;
  let bestGap = Infinity;
  times.forEach((time, index) => {
    const gap = Math.abs(Date.parse(time) - ms);
    if (gap < bestGap) {
      best = index;
      bestGap = gap;
    }
  });
  return best;
}

/**
 * A validated set (parseCommand) checked against the store: `store` = {times, productIds (the available ones),
 * bandNames}. Returns {ok: true, plan} with `t` as an index and `productId`/`bandName`, or {ok: false, message}.
 * The whole set is accepted or rejected: nothing is applied from a set that has one bad field.
 */
export function resolveSet(set, store) {
  const plan = {};
  if (set.t !== undefined) {
    if (set.t.index !== undefined) {
      if (set.t.index >= store.times.length) return { ok: false, message: `t ${set.t.index} is past the last timestep (${store.times.length - 1}); the store has ${store.times.length}.` };
      plan.t = set.t.index;
    } else plan.t = nearestTime(store.times, set.t.ms);
  }
  if (set.product !== undefined) {
    if (!store.productIds.includes(set.product)) return { ok: false, message: `product ${show(set.product)} is not available for this store; available: ${store.productIds.join(', ')}.` };
    plan.productId = set.product;
  }
  if (set.band !== undefined) {
    if (!store.bandNames.includes(set.band)) return { ok: false, message: `band ${show(set.band)} is not in this store; bands: ${store.bandNames.join(', ')}.` };
    plan.bandName = set.band;
  }
  for (const field of ['zoom', 'center', 'playing', 'speed', 'range']) if (set[field] !== undefined) plan[field] = set[field];
  return { ok: true, plan };
}

// ---- where a pixel is on Earth ----

const roundTo = (value, decimals) => Number(value.toFixed(decimals));

/**
 * Level-0 pixel <-> the store's coordinates <-> lon/lat. `transform` is the level-0 affine (or null), `projection`
 * the store CRS <-> lon/lat of maplibre/projection.js (or null when the CRS is not one it knows). Without a
 * transform, x and y are level-0 pixels and there is no lon/lat; without a projection there is no lon/lat.
 */
export function makeGeo({ transform = null, projection = null } = {}) {
  const geoReferenced = transform !== null;
  return {
    geoReferenced,
    hasLonLat: geoReferenced && projection !== null,
    /** {x, y, lon, lat} of a point given in level-0 pixels (col, row; fractions allowed); lon and lat are null when unknown. */
    fromPixel(col, row) {
      if (!geoReferenced) return { x: roundTo(col, 1), y: roundTo(row, 1), lon: null, lat: null };
      const { x, y } = pixelToProjected(transform, col, row);
      const [lon, lat] = projection ? projection.toLonLat(x, y) : [null, null];
      return { x: roundTo(x, 3), y: roundTo(y, 3), lon: lon === null ? null : roundTo(lon, 6), lat: lat === null ? null : roundTo(lat, 6) };
    },
    /** {col, row} of a validated `center` ({x, y} or {lon, lat}); throws an Error that says what is missing for lon/lat. */
    toPixel(center) {
      if (center.x !== undefined) return geoReferenced ? projectedToPixel(transform, center.x, center.y) : { col: center.x, row: center.y };
      if (!geoReferenced) throw new Error('center {lon, lat} needs a georeferenced store; this one has no affine transform. Use {x, y} in level-0 pixels.');
      if (!projection) throw new Error('center {lon, lat} needs a store CRS the viewer can project (WGS84 UTM, EPSG:3857 or EPSG:4326); use {x, y} in the store coordinates.');
      const [x, y] = projection.fromLonLat(center.lon, center.lat);
      return projectedToPixel(transform, x, y);
    },
  };
}

// ---- messages to the host ----

/** A message of the viewer: the contract version and the type are always there. */
export function embedMessage(name, payload = {}) {
  return { v: EMBED_VERSION, type: PREFIX + name, ...payload };
}

/** The message as plain JSON: undefined fields dropped, NaN and Infinity as null, nothing that cannot be cloned. */
export function toPlainJson(value) {
  return JSON.parse(JSON.stringify(value));
}

// ---- the bridge ----

/** A failure of a command that the host should hear about as `tileripper:error` with this code. */
export class EmbedCommandError extends Error {
  constructor(code, text) {
    super(text);
    this.name = 'EmbedCommandError';
    this.code = code;
  }
}

/**
 * Messages between the viewer window and its parent. Inactive (a no-op both ways) unless the page is framed and an
 * origin is known. Incoming messages from any other origin or source are dropped without a trace; accepted commands
 * are handled one at a time, in order. Outgoing messages go to that origin only, never to "*".
 */
export class EmbedBridge {
  #win;
  #parent;
  #origin;
  #onCommand;
  #debounceMs;
  #listener = null;
  #queue = Promise.resolve();
  #viewTimer = 0;
  #lastView = '';

  /** @param {{win?: Window, origin: string|null, onCommand: (command: object) => unknown, debounceMs?: number}} options */
  constructor({ win = globalThis.window, origin, onCommand, debounceMs = 120 }) {
    this.#win = win;
    this.#parent = win.parent;
    this.#origin = origin;
    this.#onCommand = onCommand;
    this.#debounceMs = debounceMs;
  }

  get active() {
    return this.#origin !== null && this.#parent !== this.#win;
  }

  start() {
    if (!this.active || this.#listener) return;
    this.#listener = (event) => this.#receive(event);
    this.#win.addEventListener('message', this.#listener);
  }

  stop() {
    if (this.#listener) this.#win.removeEventListener('message', this.#listener);
    this.#listener = null;
    clearTimeout(this.#viewTimer);
  }

  /** Send `tileripper:<name>` to the host. */
  post(name, payload) {
    if (!this.active) return;
    this.#parent.postMessage(toPlainJson(embedMessage(name, payload)), this.#origin);
  }

  /** Record the view the host already knows (the camera in a `ready` message), so it is not sent again as a `view`. */
  rememberView(payload) {
    this.#lastView = JSON.stringify(payload);
  }

  /** Send `tileripper:view` once the camera has been still for the debounce time, and only if it differs from the last one sent. */
  scheduleView(build) {
    if (!this.active) return;
    clearTimeout(this.#viewTimer);
    this.#viewTimer = setTimeout(() => {
      const payload = build();
      const key = JSON.stringify(payload);
      if (key === this.#lastView) return;
      this.#lastView = key;
      this.post('view', payload);
    }, this.#debounceMs);
  }

  #receive(event) {
    if (!isTrustedSender(event, { origin: this.#origin, parent: this.#parent })) return;
    const command = parseCommand(event.data);
    if (command.kind === 'ignore') return;
    if (command.kind === 'error') {
      this.post('error', { code: command.code, message: command.message });
      return;
    }
    this.enqueue(() => this.#onCommand(command));
  }

  /** Run `work` after the commands queued before it; a failure becomes `tileripper:error` (an unexpected one is logged too). */
  enqueue(work) {
    this.#queue = this.#queue.then(work).catch((error) => {
      if (!(error instanceof EmbedCommandError)) console.error('tileripper embed: a command failed:', error);
      this.post('error', { code: error instanceof EmbedCommandError ? error.code : 'internal', message: error.message });
    });
  }
}

// ---- the viewer side ----

async function loadProjection(crs) {
  try {
    const { createProjection } = await import('../maplibre/projection.js');
    return createProjection(crs);
  } catch (error) {
    console.warn(`tileripper embed: no lon/lat for this store (${error.message})`);
    return null;
  }
}

/**
 * Connect the viewer to its host page: set the hooks the viewer calls (see Viewer#hooks) and answer the host's
 * commands. Returns the bridge, or null when there is nobody to talk to (not framed, or no usable origin; the console
 * says why when the host most likely forgot `origin=`).
 *
 * @param {object} viewer
 * @param {ReturnType<typeof parseEmbedParams>} params
 * @param {{win?: Window, loadProjection?: (crs: string) => Promise<object|null>}} [options]
 */
export function connectEmbed(viewer, params, { win = window, loadProjection: projectionFor = loadProjection } = {}) {
  if (params.originError) console.warn(`tileripper embed: ${params.originError} The postMessage API is off.`);
  const framed = win.parent !== win;
  if (framed && !params.origin && !params.originError) {
    console.warn('tileripper embed: the page has no origin= parameter and no referrer, so the postMessage API is off. Add &origin=<the origin of the embedding page> to the iframe URL.');
  }

  let geo = makeGeo();
  let readySent = false;
  /** `set` messages that arrived before the store opened, merged (later fields win), applied right after `ready`. */
  let early = null;

  const bridge = new EmbedBridge({ win, origin: params.origin, onCommand: (command) => handle(command) });
  if (!bridge.active) return null;

  const timeOf = (t) => viewer.store.times[t];
  const viewPayload = () => ({ zoom: viewer.zoom, center: geo.fromPixel(viewer.camera.cx, viewer.camera.cy) });
  const statePayload = () => ({
    t: viewer.t,
    time: timeOf(viewer.t),
    product: viewer.products[viewer.productIndex].id,
    band: viewer.bands[viewer.bandChoice].name,
    range: viewer.stretchRange,
    ...viewPayload(),
    playing: viewer.playback?.playing ?? false,
    speed: viewer.speed,
  });
  const readyPayload = () => {
    const { store } = viewer;
    return {
      store: { url: store.url, name: new URL(store.url, win.location.href).pathname.replace(/\/$/, '').split('/').pop(), crs: store.crs ?? null },
      times: [...store.times],
      bands: viewer.bands.map((band) => ({ name: band.name, common_name: band.common_name ?? null, units: band.units ?? null, scale: band.scale, offset: band.offset })),
      products: viewer.products.map((product) => ({ id: product.id, name: product.name, available: product.available })),
      levels: store.levels.map((level) => ({ lod: level.lod, width: level.width, height: level.height, resolution: level.resolution })),
      state: statePayload(),
    };
  };

  const apply = (set) => {
    const { store } = viewer;
    const resolved = resolveSet(set, {
      times: store.times,
      productIds: viewer.products.filter((product) => product.available).map((product) => product.id),
      bandNames: viewer.bands.map((band) => band.name),
    });
    if (!resolved.ok) throw new EmbedCommandError('bad_set', resolved.message);
    const { plan } = resolved;
    // Everything that can fail is done before anything changes.
    let center;
    if (plan.center) {
      try {
        center = geo.toPixel(plan.center);
      } catch (error) {
        throw new EmbedCommandError('bad_set', error.message);
      }
    }
    if (plan.productId !== undefined) viewer.setProduct(viewer.products.findIndex((product) => product.id === plan.productId));
    if (plan.bandName !== undefined) viewer.setBandChoice(viewer.bands.findIndex((band) => band.name === plan.bandName));
    if (plan.range === null) viewer.autoStretch();
    else if (plan.range !== undefined) viewer.setStretch(...plan.range);
    if (plan.t !== undefined) viewer.goToTime(plan.t);
    if (plan.zoom !== undefined || center) viewer.setView({ zoom: plan.zoom, center });
    if (plan.speed !== undefined) viewer.setSpeed(plan.speed);
    if (plan.playing === true) viewer.play();
    else if (plan.playing === false) viewer.pause();
  };

  async function handle(command) {
    if (command.kind === 'get') {
      if (readySent && viewer.store) {
        bridge.post('ready', readyPayload());
        bridge.rememberView(viewPayload());
      }
      return;
    }
    if (!readySent || !viewer.store) {
      early = { ...early, ...command.set };
      return;
    }
    apply(command.set);
  }

  viewer.hooks = {
    ready: () => {
      readySent = false;
      const store = viewer.store;
      const projected = store.transform && store.crs ? projectionFor(store.crs) : Promise.resolve(null);
      projected.then((loaded) => {
        if (viewer.store !== store) return;
        geo = makeGeo({ transform: store.transform, projection: loaded });
        readySent = true;
        bridge.post('ready', readyPayload());
        bridge.rememberView(viewPayload());
        if (early) {
          const set = early;
          early = null;
          bridge.enqueue(() => apply(set));
        }
      });
    },
    time: ({ t }) => {
      if (readySent) bridge.post('time', { t, time: timeOf(t) });
    },
    view: () => {
      if (readySent) bridge.scheduleView(viewPayload);
    },
    click: ({ pixel, t, lod, info }) => {
      if (!readySent) return;
      const { lon, lat } = geo.fromPixel(pixel.x + 0.5, pixel.y + 0.5);
      bridge.post('click', {
        pixel,
        lon,
        lat,
        t,
        time: timeOf(t),
        level: lod,
        valid: info.valid,
        values: Object.fromEntries(info.bands.map((band) => [band.name, info.valid ? band.value : null])),
      });
    },
    error: ({ code, title, message: text }) => bridge.post('error', { code, message: `${title}: ${text}` }),
  };

  bridge.start();
  return bridge;
}
