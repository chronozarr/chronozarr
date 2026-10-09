// "Open store URL": validation of the pasted URL, the diagnosis of a store that does not open, credentials kept out of
// every message, and the rule that a store which fails to open leaves the view on screen alone.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { openStore } from '../chronozarr/decoder.js';
import { FetchError } from '../chronozarr/http.js';
import { redactText, redactUrl } from '../chronozarr/redact.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';
import { StoreOpenError, bindOpenStoreForm, checkUniformChunks, explainOpenError, parseStoreUrl, submitStoreUrl, validateStore } from '../demo/open-store.js';

const SECRET = 'SECRET-SIGNATURE';
const PAGE = { href: 'https://chronozarr.org/demo/', protocol: 'https:' };
const LOCAL_PAGE = { href: 'http://localhost:8000/js/demo/index.html', protocol: 'http:' };
const SPEC = { nTime: 3, nBand: 1, height: 40, width: 40, chunk: 32, sharded: false };

const response = (status, body = '', headers = {}) => new Response(body, { status, headers });

/** An `open` for validateStore that reads through the real reader with `fetch` as the network, without waiting between retries. */
const openOver = (fetch) => (url, options) => openStore(url, { ...options, fetch, retryDelaysMs: [], workers: 0 });

/** An `open` that serves the in-memory fixture, as if the host were a healthy store. */
const openFixture = (spec = SPEC) => (url, options) => openStore(url, { ...options, store: buildSyntheticStore(spec), workers: 0 });

const never = () => assert.fail('the host was probed');

/** The error a rejecting validateStore gives, for the host behaviour `fetch`; `reachable` answers the no-cors probe. */
async function failureOver(fetch, { reachable = never, input = 'https://data.example.org/store?sig=' + SECRET } = {}) {
  const result = await submitStoreUrl(input, { open: openOver(fetch), reachable, page: PAGE, load: () => assert.fail('load must not run') });
  assert.equal(result.ok, false);
  return result.error;
}

test('a store URL is trimmed, resolved against the page, stripped of its fragment and trailing slash, and keeps its signed query only for reading', () => {
  assert.deepEqual(parseStoreUrl('  https://data.example.org/a/store/#frag  ', PAGE), { url: 'https://data.example.org/a/store', display: 'https://data.example.org/a/store' });
  assert.deepEqual(parseStoreUrl('https://data.example.org/a/store/?X-Amz-Signature=' + SECRET, PAGE), {
    url: `https://data.example.org/a/store?X-Amz-Signature=${SECRET}`,
    display: 'https://data.example.org/a/store',
  });
  assert.equal(parseStoreUrl('../data/spike/v03', LOCAL_PAGE).url, 'http://localhost:8000/js/data/spike/v03');
  assert.equal(parseStoreUrl('https://data.example.org', PAGE).url, 'https://data.example.org/');
});

test('an http store is refused on an https page except on loopback, where browsers allow it', () => {
  assert.equal(parseStoreUrl('http://localhost:8000/data/v03', PAGE).display, 'http://localhost:8000/data/v03');
  assert.equal(parseStoreUrl('http://127.0.0.1:8000/v03', PAGE).display, 'http://127.0.0.1:8000/v03');
  assert.equal(parseStoreUrl('http://[::1]:8000/v03', PAGE).display, 'http://[::1]:8000/v03');
  assert.throws(() => parseStoreUrl('http://data.example.org/store', PAGE), { code: 'mixed_content' });
  assert.equal(parseStoreUrl('http://data.example.org/store', LOCAL_PAGE).display, 'http://data.example.org/store');
});

for (const [name, input, code] of [
  ['nothing', '   ', 'empty_url'],
  ['text that is not an address', 'not a url', 'invalid_url'],
  ['a scheme a browser cannot read', 's3://bucket/store', 'unsupported_scheme'],
  ['a file URL', 'file:///data/store', 'unsupported_scheme'],
]) {
  test(`${name} is refused before any request`, () => {
    assert.throws(() => parseStoreUrl(input, { href: 'about:blank', protocol: 'about:' }), (error) => {
      assert.ok(error instanceof StoreOpenError);
      assert.equal(error.code, code);
      return true;
    });
  });
}

test('a login in the URL is refused, and no message repeats it', () => {
  for (const input of ['https://alice:hunter2@data.example.org/store', 'https://alice@data.example.org/store?token=abc']) {
    assert.throws(() => parseStoreUrl(input, PAGE), (error) => {
      assert.equal(error.code, 'credentials_in_url');
      assert.doesNotMatch(`${error.summary} ${error.message}`, /alice|hunter2|token|abc/);
      return true;
    });
  }
});

test('redactUrl keeps scheme, host and path, and drops login, query and fragment', () => {
  assert.equal(redactUrl('https://alice:hunter2@data.example.org:8443/a/b?X-Amz-Signature=abc&x=1#frag'), 'https://data.example.org:8443/a/b');
  assert.equal(redactUrl('memory://store/zarr.json'), 'memory://store/zarr.json');
  assert.equal(redactUrl('not a url'), '');
  assert.equal(
    redactText(`GET https://u:p@h.example/s/zarr.json?sig=${SECRET}: HTTP 403, then http://h.example/x?token=${SECRET}.`),
    'GET https://h.example/s/zarr.json: HTTP 403, then http://h.example/x.',
  );
});

test('a read that fails keeps the signed query out of the reader\'s own message', async () => {
  const error = await openStore(`https://data.example.org/store?sig=${SECRET}`, { fetch: async () => response(403), retryDelaysMs: [], workers: 0 }).catch((e) => e);
  assert.ok(error instanceof FetchError);
  assert.equal(error.status, 403);
  assert.doesNotMatch(error.message, new RegExp(SECRET));
  assert.match(error.message, /^GET https:\/\/data\.example\.org\/store\/zarr\.json: HTTP 403/);
});

test('a host that is up but sends no CORS headers is reported as blocked by the browser', async () => {
  const error = await failureOver(async () => Promise.reject(new TypeError('Failed to fetch')), { reachable: async () => true });
  assert.equal(error.code, 'cors_blocked');
  assert.match(error.message, /Access-Control-Allow-Origin/);
  assert.match(error.message, /data\.example\.org/);
  assert.doesNotMatch(error.text, new RegExp(SECRET));
});

test('a host that cannot be reached at all is reported as unreachable, not as CORS', async () => {
  const probed = [];
  const error = await failureOver(async () => Promise.reject(new TypeError('Failed to fetch')), {
    reachable: async (url) => {
      probed.push(url);
      return false;
    },
  });
  assert.equal(error.code, 'unreachable');
  assert.deepEqual(probed, [`https://data.example.org/store?sig=${SECRET}`], 'the probe is given the readable URL; only messages are redacted');
  assert.doesNotMatch(error.text, new RegExp(SECRET));
});

test('HTTP answers are told apart: access denied, not found, server error, anything else', async () => {
  const cases = [
    [401, 'access_denied', /signed URL/],
    [403, 'access_denied', /HTTP 403/],
    [404, 'not_found', /directory that contains the root zarr\.json/],
    [503, 'server_error', /HTTP 503/],
    [418, 'http_error', /HTTP 418/],
  ];
  for (const [status, code, text] of cases) {
    const error = await failureOver(async () => response(status));
    assert.equal(error.code, code, `HTTP ${status}`);
    assert.match(error.message, text, `HTTP ${status}`);
    assert.doesNotMatch(error.text, new RegExp(SECRET), `HTTP ${status}`);
  }
});

test('a web page or another JSON document at the address is a format error that says what was wrong', async () => {
  const html = await failureOver(async () => response(200, '<!doctype html><title>Sign in</title>'));
  assert.equal(html.code, 'not_a_store');
  assert.match(html.message, /not valid JSON/);

  const other = await failureOver(async () => response(200, JSON.stringify({ zarr_format: 3, node_type: 'group', attributes: {} })));
  assert.equal(other.code, 'invalid_store');
  assert.match(other.message, /no "chronozarr" entry/);
  assert.doesNotMatch(other.text, new RegExp(SECRET));
  assert.doesNotMatch(other.text, /https?:/, 'the reader prefixes its messages with the URL; the diagnosis does not repeat it');
});

test('a store whose levels chunk differently is refused as a layout the viewer cannot show', () => {
  const store = { levels: [{ lod: 0, chunkWidth: 512, chunkHeight: 512 }, { lod: 1, chunkWidth: 256, chunkHeight: 256 }] };
  assert.throws(() => checkUniformChunks(store), { code: 'unsupported_layout' });
  assert.doesNotThrow(() => checkUniformChunks({ levels: [store.levels[0], { lod: 1, chunkWidth: 512, chunkHeight: 512 }] }));
});

test('explainOpenError never lets a URL with a login or signed query through in the fallback message', async () => {
  const error = await explainOpenError(new Error(`boom at https://u:p@h.example/s?sig=${SECRET}.`), { url: 'https://h.example/s' });
  assert.equal(error.code, 'open_failed');
  assert.equal(error.message, 'boom at https://h.example/s.');
});

test('validateStore resolves to the open store, and closes it when the layout check refuses it', async () => {
  const valid = await validateStore(`https://data.example.org/store?sig=${SECRET}`, { open: openFixture(), page: PAGE });
  assert.equal(valid.display, 'https://data.example.org/store');
  assert.equal(valid.url, `https://data.example.org/store?sig=${SECRET}`);
  assert.equal(valid.store.times.length, 3);
  valid.store.close();

  let closed = false;
  const lopsided = { levels: [{ lod: 0, chunkWidth: 2, chunkHeight: 2 }, { lod: 1, chunkWidth: 1, chunkHeight: 1 }], close: () => (closed = true) };
  await assert.rejects(validateStore('https://data.example.org/store', { open: async () => lopsided, page: PAGE }), { code: 'unsupported_layout' });
  assert.equal(closed, true);
});

test('a store that is refused leaves the current view alone: nothing is loaded, torn down or written to the address bar', async () => {
  const history = [];
  const viewer = {
    store: { name: 'on screen' },
    loads: [],
    async loadStore(url, options) {
      this.loads.push([url, options]);
      this.store = options.store;
      history.push(url);
    },
  };
  const load = async ({ url, display, store }) => viewer.loadStore(url, { store, display });

  for (const fetch of [async () => response(404), async () => response(403), async () => Promise.reject(new TypeError('Failed to fetch')), async () => response(200, 'not json')]) {
    const result = await submitStoreUrl('https://data.example.org/store?sig=' + SECRET, { open: openOver(fetch), reachable: async () => true, page: PAGE, load });
    assert.equal(result.ok, false);
    assert.ok(result.error instanceof StoreOpenError);
  }
  assert.equal(await submitStoreUrl('https://alice:pw@data.example.org/store', { open: openFixture(), page: PAGE, load }).then((r) => r.ok), false);
  assert.deepEqual(viewer.loads, [], 'the viewer was never asked to load');
  assert.deepEqual(viewer.store, { name: 'on screen' });
  assert.deepEqual(history, []);

  const result = await submitStoreUrl(`https://data.example.org/store/?sig=${SECRET}`, { open: openFixture(), page: PAGE, load });
  assert.deepEqual(result, { ok: true, display: 'https://data.example.org/store' });
  assert.equal(viewer.loads.length, 1);
  assert.equal(viewer.loads[0][0], `https://data.example.org/store?sig=${SECRET}`);
  assert.equal(viewer.loads[0][1].store.times.length, 3, 'the viewer gets the store that was already opened');
  viewer.store.close();
});

test('opening is cancelled by an abort: the opened store is closed and nothing is loaded', async () => {
  const controller = new AbortController();
  let closed = false;
  const open = async () => {
    controller.abort();
    return { levels: [{ lod: 0, chunkWidth: 1, chunkHeight: 1 }], close: () => (closed = true) };
  };
  const result = await submitStoreUrl('https://data.example.org/store', { open, page: PAGE, signal: controller.signal, load: () => assert.fail('load must not run') });
  assert.deepEqual(result, { ok: false, cancelled: true });
  assert.equal(closed, true);

  const rejecting = await submitStoreUrl('https://data.example.org/store', {
    open: async (url, { signal }) => {
      controller.abort();
      signal.throwIfAborted();
    },
    page: PAGE,
    signal: controller.signal,
    load: () => assert.fail('load must not run'),
  });
  assert.deepEqual(rejecting, { ok: false, cancelled: true });
});

test('a store that opened but cannot be shown is a failure with a message, without the signed query', async () => {
  const result = await submitStoreUrl(`https://data.example.org/store?sig=${SECRET}`, {
    open: openFixture(),
    page: PAGE,
    load: async ({ store }) => {
      store.close();
      throw new Error(`cannot show https://data.example.org/store?sig=${SECRET}`);
    },
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, 'open_failed');
  assert.doesNotMatch(result.error.text, new RegExp(SECRET));
});

// ---- the form ----

function fakeElement(extra = {}) {
  const listeners = {};
  return {
    hidden: false,
    textContent: '',
    value: '',
    children: [],
    attributes: new Map(),
    listeners,
    addEventListener(type, handler) {
      (listeners[type] ??= []).push(handler);
    },
    setAttribute(name, value) {
      this.attributes.set(name, String(value));
    },
    removeAttribute(name) {
      this.attributes.delete(name);
    },
    replaceChildren(...nodes) {
      this.children = nodes;
      this.textContent = nodes.map((node) => (typeof node === 'string' ? node : node.textContent)).join('');
    },
    async dispatch(type) {
      const event = { preventDefault() {} };
      await Promise.all((listeners[type] ?? []).map((handler) => handler(event)));
    },
    ...extra,
  };
}

function fakeForm(deps) {
  globalThis.document = { createElement: () => ({ textContent: '' }) };
  const elements = { form: fakeElement(), input: fakeElement(), status: fakeElement({ hidden: true }), error: fakeElement({ hidden: true, id: 'open-store-error' }) };
  return { elements, bound: bindOpenStoreForm(elements, { page: PAGE, ...deps }) };
}

test('the form shows why a store did not open next to the field, keeps what was typed, and marks the field invalid until it is edited', async (t) => {
  t.after(() => delete globalThis.document);
  const { elements } = fakeForm({ open: openOver(async () => response(404)), load: () => assert.fail('load must not run') });
  elements.input.value = `https://data.example.org/missing?sig=${SECRET}`;
  await elements.form.dispatch('submit');
  assert.equal(elements.error.hidden, false);
  assert.match(elements.error.textContent, /^Store not found\. /);
  assert.doesNotMatch(elements.error.textContent, new RegExp(SECRET));
  assert.equal(elements.input.value, `https://data.example.org/missing?sig=${SECRET}`, 'the field keeps the input so it can be fixed');
  assert.equal(elements.input.attributes.get('aria-invalid'), 'true');
  assert.equal(elements.input.attributes.get('aria-describedby'), 'open-store-error');
  assert.equal(elements.form.attributes.has('aria-busy'), false);
  assert.equal(elements.status.hidden, true);

  await elements.input.dispatch('input');
  assert.equal(elements.error.hidden, true);
  assert.equal(elements.input.attributes.has('aria-invalid'), false);
});

test('the form replaces a signed URL with its display address once the store is open, and ignores a second submit while one runs', async (t) => {
  t.after(() => delete globalThis.document);
  let release;
  const gate = new Promise((resolve) => (release = resolve));
  let opens = 0;
  const loaded = [];
  const { elements } = fakeForm({
    open: async (url, options) => {
      opens++;
      await gate;
      return openFixture()(url, options);
    },
    load: async ({ url, store }) => {
      loaded.push(url);
      store.close();
    },
  });
  elements.input.value = `https://data.example.org/store?sig=${SECRET}`;
  const first = elements.form.dispatch('submit');
  await elements.form.dispatch('submit');
  assert.equal(opens, 1, 'the second submit was ignored');
  assert.equal(elements.form.attributes.get('aria-busy'), 'true');
  assert.equal(elements.status.textContent, 'Opening store…');
  release();
  await first;
  assert.deepEqual(loaded, [`https://data.example.org/store?sig=${SECRET}`]);
  assert.equal(elements.input.value, 'https://data.example.org/store');
  assert.equal(elements.error.hidden, true);
  assert.equal(elements.form.attributes.has('aria-busy'), false);
});

test('cancel() drops an open that is still running', async (t) => {
  t.after(() => delete globalThis.document);
  let release;
  const gate = new Promise((resolve) => (release = resolve));
  const { elements, bound } = fakeForm({
    open: async (url, options) => {
      await gate;
      return openFixture()(url, options);
    },
    load: () => assert.fail('load must not run'),
  });
  elements.input.value = 'https://data.example.org/store';
  const submitted = elements.form.dispatch('submit');
  bound.cancel();
  release();
  await submitted;
  assert.equal(elements.error.hidden, true, 'a cancelled open is not an error');
  assert.equal(elements.input.value, 'https://data.example.org/store');
});

// ---- the page ----

const html = readFileSync(new URL('../demo/index.html', import.meta.url), 'utf8');
const css = html.slice(html.indexOf('<style>'), html.indexOf('</style>'));
const viewerSource = readFileSync(new URL('../demo/viewer.js', import.meta.url), 'utf8');

test('index.html has a labelled field and button in a form, an announced status and an announced error line', () => {
  assert.match(html, /<form id="open-store" class="open-store" novalidate>/);
  assert.match(html, /<label for="open-store-url">Open store URL<\/label>/);
  assert.match(html, /<input id="open-store-url" type="text" inputmode="url"[^>]*autocomplete="off"/);
  assert.match(html, /<button type="submit" class="export-btn">Open<\/button>/);
  assert.match(html, /<p id="open-store-status"[^>]*role="status"/);
  assert.match(html, /<p id="open-store-error"[^>]*role="alert"/);
});

test('the field is hidden in an embed, has a ring on keyboard focus and 44 px targets and a 16 px font for a finger', () => {
  assert.match(css, /html\[data-embed\] \.open-store \{ display: none; \}/);
  assert.match(css, /\.open-store input:focus-visible, \.open-store button:focus-visible \{ outline: 2px solid var\(--accent\)/);
  assert.match(css, /@media \(pointer: coarse\) \{\s*\/\*[^*]*\*\/\s*\.open-store input \{ font-size: 16px; \}\s*\.open-store input, \.open-store button \{ min-height: 44px; \}/);
});

test('the viewer takes the opened store, and writes only the display address to the address bar', () => {
  assert.match(viewerSource, /store \?\?= await openStore\(/);
  assert.match(viewerSource, /encodeURIComponent\(redactUrl\(this\.pinnedStore\)\)/);
  assert.match(viewerSource, /bindOpenStoreForm\(/);
});
