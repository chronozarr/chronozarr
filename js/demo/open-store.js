// "Open store URL": the field in the header that opens a store the user pastes, without writing ?store= by hand.
//
// The store is opened (root and array metadata read) BEFORE the viewer lets go of what it shows: a URL that is wrong,
// blocked or not a chronozarr store fails here, with a message that says which, and the view on screen stays as it
// was. Only a store that opened is handed to the viewer, which swaps to it without a second read. Credentials never
// reach a message or the address bar: a login in the URL is refused (fetch refuses it too), and a signed query
// is used for the reads but dropped from everything that is shown or written to the history (see redact.js).

import { openStore as readerOpenStore } from '../chronozarr/decoder.js';
import { isAbort } from '../chronozarr/http.js';
import { redactText, redactUrl } from '../chronozarr/redact.js';

/** One retry, so a blip does not fail a paste while a dead host still answers within about a second. */
const RETRY_DELAYS_MS = [250];
const REACHABLE_TIMEOUT_MS = 5000;

/** A store that cannot be opened, said for the person who pasted the URL. `code` is stable, `summary` is the headline. */
export class StoreOpenError extends Error {
  constructor(code, summary, message) {
    super(message);
    this.name = 'StoreOpenError';
    this.code = code;
    this.summary = summary;
  }

  /** Headline and explanation as one line. */
  get text() {
    return `${this.summary}. ${this.message}`;
  }
}

function isLoopback(hostname) {
  return hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]' || hostname.endsWith('.localhost');
}

/**
 * The store URL a user typed, checked and normalized. `url` is what to read (the signed query stays: every object of the
 * store is fetched with it); `display` is what may be shown, logged and put in the address bar (scheme, host, path).
 * A relative URL resolves against the page, as ?store= does. Throws StoreOpenError; no message repeats the input.
 * @param {string} input
 * @param {{href: string, protocol: string}} [page]
 * @returns {{url: string, display: string}}
 */
export function parseStoreUrl(input, page = globalThis.location) {
  const text = String(input ?? '').trim();
  if (!text) throw new StoreOpenError('empty_url', 'No URL', 'Paste the URL of a chronozarr store: the directory that contains its root zarr.json.');
  let url;
  try {
    url = new URL(text, page?.href);
  } catch {
    throw new StoreOpenError('invalid_url', 'Not a URL', 'That is not a valid address. A store URL looks like https://example.org/data/my-store.');
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new StoreOpenError('unsupported_scheme', 'Unsupported address', `A browser can only read http: and https: URLs, and this one uses ${url.protocol}. Use the https address of the bucket or CDN that serves the store.`);
  }
  if (url.username || url.password) {
    throw new StoreOpenError('credentials_in_url', 'Login in the URL', 'Browsers refuse URLs that contain a username or password. Use a public URL, or a signed URL, which carries its signature in the query string.');
  }
  if (page?.protocol === 'https:' && url.protocol === 'http:' && !isLoopback(url.hostname)) {
    throw new StoreOpenError('mixed_content', 'Insecure address', 'This page is served over https, and a browser blocks it from reading an http: address. Use the https address of the store.');
  }
  url.hash = '';
  url.pathname = url.pathname.replace(/\/+$/, '') || '/';
  return { url: url.href, display: redactUrl(url.href) };
}

/** Every level must chunk the same way: the viewer's texture pool has one slot shape. Throws StoreOpenError. */
export function checkUniformChunks(store) {
  const first = store.levels[0];
  for (const level of store.levels) {
    if (level.chunkWidth !== first.chunkWidth || level.chunkHeight !== first.chunkHeight) {
      throw new StoreOpenError('unsupported_layout', 'Layout not supported', `Level ${level.lod} has ${level.chunkWidth}x${level.chunkHeight} chunks and level 0 has ${first.chunkWidth}x${first.chunkHeight}; the viewer needs the same chunk size at every level.`);
    }
  }
}

/**
 * Whether the host of the store answers at all, asked in a way that does not need CORS: an opaque answer to a no-cors
 * request still proves a server was reached. Tells "blocked by CORS" from "no connection" when a read fails without a status.
 */
export async function probeReachable(url, { fetch = globalThis.fetch, timeoutMs = REACHABLE_TIMEOUT_MS } = {}) {
  const root = new URL(url);
  root.pathname = `${root.pathname.replace(/\/+$/, '')}/zarr.json`;
  try {
    await fetch(root, { mode: 'no-cors', cache: 'no-store', signal: AbortSignal.timeout(timeoutMs) });
    return true;
  } catch {
    return false;
  }
}

function hostOf(url) {
  try {
    return new URL(url, globalThis.location?.href).host || 'the host';
  } catch {
    return 'the host';
  }
}

/**
 * A StoreOpenError for whatever a failed open threw: access, CORS, network, not found or format. The kind of read failure
 * comes from the reader's FetchError (HTTP status, or none when the request never completed) and its format errors
 * ("not a valid chronozarr store"). Nothing of the original message is repeated except with URLs reduced to scheme, host and path.
 * @param {unknown} error
 * @param {{url: string}} target
 * @param {{reachable?: (url: string) => Promise<boolean>}} [deps]
 */
export async function explainOpenError(error, target, { reachable = probeReachable } = {}) {
  if (error instanceof StoreOpenError) return error;
  const host = hostOf(target.url);
  const message = redactText(error?.message ?? String(error));
  const status = error?.status;
  if (error?.name === 'FetchError' && status === undefined) {
    if (await reachable(target.url)) {
      return new StoreOpenError(
        'cors_blocked',
        'Blocked by the browser',
        `${host} answered, but the browser blocked this page from reading the answer. The host has to send an Access-Control-Allow-Origin header (for example *). A wrong URL looks the same when its error page has no such header, so check the address too; "chronozarr doctor <url>" tells which.`,
      );
    }
    return new StoreOpenError('unreachable', 'Host not reachable', `Could not connect to ${host}. Check the address and your network connection, and that the host's certificate is valid.`);
  }
  if (status === 401 || status === 403) {
    return new StoreOpenError('access_denied', 'Access denied', `${host} refused the read (HTTP ${status}). The store has to be public, or the URL a signed URL that has not expired.`);
  }
  if (status === 404 || /root zarr\.json not found/.test(message)) {
    return new StoreOpenError('not_found', 'Store not found', `There is no zarr.json at this address${status ? ` (HTTP ${status})` : ''}. The URL must be the directory that contains the root zarr.json, not the file itself.`);
  }
  if (status === 429 || status >= 500) {
    return new StoreOpenError('server_error', 'Server error', `${host} failed to answer (HTTP ${status}). Try again in a moment.`);
  }
  if (status) return new StoreOpenError('http_error', 'Request failed', `${host} answered HTTP ${status}.`);
  if (error?.name === 'SyntaxError') {
    return new StoreOpenError('not_a_store', 'Not a chronozarr store', 'The root zarr.json at this address is not valid JSON; the URL probably serves a web page instead of a store.');
  }
  const format = /not a valid chronozarr store: (.*)$/s.exec(message);
  if (format) return new StoreOpenError('invalid_store', 'Not a chronozarr store', `${format[1].replace(/\.$/, '')}.`);
  return new StoreOpenError('open_failed', 'Could not open the store', message);
}

/**
 * Parse the URL, open the store and check it, all before the caller changes anything. Resolves to `{url, display, store}`;
 * the store is open and owned by the caller. Rejects with a StoreOpenError, or with an AbortError when `signal` aborts.
 * @param {string} input
 * @param {{open?: typeof readerOpenStore, reachable?: (url: string) => Promise<boolean>, page?: object, signal?: AbortSignal}} [deps]
 */
export async function validateStore(input, { open = readerOpenStore, reachable = probeReachable, page = globalThis.location, signal } = {}) {
  const target = parseStoreUrl(input, page);
  let store;
  try {
    store = await open(target.url, { signal, retryDelaysMs: RETRY_DELAYS_MS });
    checkUniformChunks(store);
  } catch (error) {
    store?.close();
    if (isAbort(error)) throw error;
    throw await explainOpenError(error, target, { reachable });
  }
  return { ...target, store };
}

/**
 * Validate, then hand the opened store to `load({url, display, store})`, which swaps the view. `load` is not called when the
 * URL or the store is refused, so the view on screen is untouched. Never rejects:
 * `{ok: true, display}`, `{ok: false, error: StoreOpenError}`, or `{ok: false, cancelled: true}` when `signal` aborted.
 */
export async function submitStoreUrl(input, { load, signal, ...deps }) {
  let target;
  try {
    target = await validateStore(input, { ...deps, signal });
  } catch (error) {
    if (isAbort(error)) return { ok: false, cancelled: true };
    return { ok: false, error: error instanceof StoreOpenError ? error : new StoreOpenError('open_failed', 'Could not open the store', redactText(error?.message ?? error)) };
  }
  if (signal?.aborted) {
    target.store.close();
    return { ok: false, cancelled: true };
  }
  try {
    await load(target);
  } catch (error) {
    return { ok: false, error: new StoreOpenError('open_failed', 'Could not show the store', redactText(error?.message ?? error)) };
  }
  return { ok: true, display: target.display };
}

/**
 * Wire the form. `elements`: form, input, status (a role=status line) and error (a role=alert line). A second submit is
 * ignored while one runs. A failure is written next to the field and the field keeps what was typed, so it can be fixed;
 * a success replaces it with the display address, which has no credentials in it. Returns `{cancel}` to drop a pending open
 * (another store was chosen meanwhile).
 */
export function bindOpenStoreForm({ form, input, status, error }, { load, ...deps }) {
  let pending = null;
  const clearError = () => {
    error.hidden = true;
    error.replaceChildren();
    input.removeAttribute('aria-invalid');
    input.removeAttribute('aria-describedby');
  };
  const showError = ({ summary, message }) => {
    const headline = document.createElement('strong');
    headline.textContent = `${summary}. `;
    error.replaceChildren(headline, message);
    error.hidden = false;
    input.setAttribute('aria-invalid', 'true');
    input.setAttribute('aria-describedby', error.id);
  };
  const setStatus = (text) => {
    status.textContent = text;
    status.hidden = !text;
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (pending) return;
    const controller = new AbortController();
    pending = controller;
    form.setAttribute('aria-busy', 'true');
    clearError();
    setStatus('Opening store…');
    try {
      const result = await submitStoreUrl(input.value, { ...deps, load, signal: controller.signal });
      if (result.ok) input.value = result.display;
      else if (!result.cancelled) showError(result.error);
    } finally {
      pending = null;
      form.removeAttribute('aria-busy');
      setStatus('');
    }
  });
  input.addEventListener('input', clearError);
  return { cancel: () => pending?.abort() };
}
