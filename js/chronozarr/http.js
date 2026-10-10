// Reads objects of a store over HTTP (or from any zarrita-style readable) with the limiter, retries and
// accounting the decoder needs. A request holds a concurrency slot only while it is on the wire: the wait
// between retries happens outside the limiter, so a failing shard never occupies slots.

import { redactUrl } from './redact.js';

export const abortError = () => new DOMException('Aborted', 'AbortError');
export const isAbort = (error) => error?.name === 'AbortError';

/** A request to the store that failed for good: carries the URL, HTTP status (if any) and attempt count. */
export class FetchError extends Error {
  constructor({ url, method, range, status, statusText, cause, attempts }) {
    const what = status ? `HTTP ${status}${statusText ? ` ${statusText}` : ''}` : `${cause.name}: ${cause.message}`;
    super(`${method} ${redactUrl(url)}${range ? ` [${range}]` : ''}: ${what} (${attempts} attempt${attempts === 1 ? '' : 's'})`);
    this.name = 'FetchError';
    Object.assign(this, { url, method, range, status, attempts, cause });
  }
}

export function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError());
    const onAbort = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', onAbort);
      reject(abortError());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

/**
 * Store over HTTP. `get(key)` and `getRange(key, range)` resolve to a Uint8Array, or undefined on 404. Keys
 * start with '/'. Options per call: `signal`, and `priority` (0 = a frame waits, 1 = background). Two more for
 * the reader's recovery from a changed store: `get` with `reload` skips the browser's HTTP cache and refreshes it
 * with the answer (`cache: 'reload'`; the browser adds `Cache-Control: no-cache` itself, which is not an author
 * header, so a cross-origin read stays a simple request with no preflight), and `getRange` with `rangeMiss`
 * resolves to `null` instead of failing when the server answers 416 (the range starts beyond the object).
 *
 * Every attempt takes a limiter slot and counts toward `network.requests`; network errors, 5xx and 429 are
 * retried after each delay in `retryDelaysMs` (jittered +-30%); everything else that is not 200/206/404 fails
 * at once. Failures end in a FetchError and are logged to the console.
 */
export class HttpStore {
  #base;
  #fetch;
  #limiter;
  #network;
  #bandwidth;
  #delays;
  #suffixRequests;

  constructor(baseUrl, { fetch, limiter, network, bandwidth, retryDelaysMs, suffixRequests }) {
    const base = new URL(baseUrl, globalThis.location?.href);
    if (!base.pathname.endsWith('/')) base.pathname += '/';
    this.#base = base;
    this.#fetch = fetch;
    this.#limiter = limiter;
    this.#network = network;
    this.#bandwidth = bandwidth;
    this.#delays = retryDelaysMs;
    this.#suffixRequests = suffixRequests;
  }

  #url(key) {
    const url = new URL(key.slice(1), this.#base);
    url.search = this.#base.search;
    return url;
  }

  async get(key, { signal, priority = 0, reload = false } = {}) {
    const { bytes } = await this.#send(this.#url(key), 'GET', undefined, signal, priority, { reload });
    return bytes;
  }

  /** `range` is {offset, length} or {suffixLength}. A suffix costs a HEAD first unless the store sends suffix ranges. */
  async getRange(key, range, { signal, priority = 0, rangeMiss = false } = {}) {
    const url = this.#url(key);
    let start;
    let end;
    if ('suffixLength' in range) {
      if (this.#suffixRequests) {
        const { bytes } = await this.#send(url, 'GET', `bytes=-${range.suffixLength}`, signal, priority);
        return bytes && bytes.slice(Math.max(0, bytes.length - range.suffixLength));
      }
      const { headers } = await this.#send(url, 'HEAD', undefined, signal, priority);
      if (!headers) return undefined;
      const contentLength = headers.get('Content-Length');
      const size = Number(contentLength);
      if (contentLength === null || !/^\d+$/.test(contentLength) || !Number.isSafeInteger(size)) {
        throw new Error(`HEAD ${url}: missing or invalid Content-Length, cannot read the last ${range.suffixLength} bytes`);
      }
      if (size === 0) return new Uint8Array(0);
      start = Math.max(0, size - range.suffixLength);
      end = size - 1;
    } else {
      start = range.offset;
      end = range.offset + range.length - 1;
    }
    const { bytes, status } = await this.#send(url, 'GET', `bytes=${start}-${end}`, signal, priority, { rangeMiss });
    if (status === 416) return null;
    // A server that ignores Range answers 200 with the whole object.
    return bytes && status === 200 ? bytes.slice(start, end + 1) : bytes;
  }

  async #send(url, method, range, signal, priority, { reload = false, rangeMiss = false } = {}) {
    const request = new Request(url, { method, signal, headers: range ? { Range: range } : undefined, ...(reload ? { cache: 'reload' } : {}) });
    for (let attempt = 1; ; attempt++) {
      const outcome = await this.#limiter.run(priority, signal, () => this.#attempt(request, method, rangeMiss));
      if (outcome.ok) return outcome;
      const { status, statusText, cause } = outcome;
      const retryable = status === undefined || status >= 500 || status === 429;
      const delayMs = retryable && attempt <= this.#delays.length ? this.#delays[attempt - 1] * (0.7 + 0.6 * Math.random()) : null;
      const details = { url: redactUrl(request.url), method, range, status, error: `${cause.name}: ${cause.message}`, attempt, of: this.#delays.length + 1, retryInMs: delayMs === null ? null : Math.round(delayMs) };
      if (delayMs === null) {
        console.error('chronozarr: request failed, giving up', details);
        throw new FetchError({ url: request.url, method, range, status, statusText, cause, attempts: attempt });
      }
      console.warn('chronozarr: request failed, retrying', details);
      await sleep(delayMs, signal);
    }
  }

  /** One trip to the server, inside a limiter slot. Never throws except on abort: failures come back as data. */
  async #attempt(request, method, rangeMiss) {
    this.#network.requests++;
    this.#bandwidth.begin();
    let received = 0;
    try {
      const response = await this.#fetch(request);
      if (response.status === 416 && rangeMiss) {
        await response.body?.cancel();
        return { ok: true, status: 416, bytes: undefined };
      }
      if (response.status !== 200 && response.status !== 206 && response.status !== 404) {
        await response.body?.cancel();
        return { ok: false, status: response.status, statusText: response.statusText, cause: new Error(`HTTP ${response.status}`) };
      }
      if (method === 'HEAD') return { ok: true, status: response.status, headers: response.status === 404 ? undefined : response.headers };
      if (response.status === 404) {
        await response.body?.cancel();
        return { ok: true, status: 404, bytes: undefined };
      }
      const bytes = new Uint8Array(await response.arrayBuffer());
      received = bytes.length;
      this.#network.bytes += received;
      return { ok: true, status: response.status, bytes };
    } catch (error) {
      if (isAbort(error)) throw error;
      return { ok: false, cause: error };
    } finally {
      this.#bandwidth.end(received);
    }
  }
}

/**
 * Wraps a zarrita-style readable (`get`, `getRange` with {signal}) so its calls go through the limiter and
 * are counted. The readable decides for itself how to retry; priority is not passed on to it.
 */
export class LimitedReadable {
  #readable;
  #limiter;
  #network;
  #bandwidth;

  constructor(readable, { limiter, network, bandwidth }) {
    this.#readable = readable;
    this.#limiter = limiter;
    this.#network = network;
    this.#bandwidth = bandwidth;
  }

  /** `reload` is passed on as `cache: 'reload'` (a zarrita FetchStore hands it to fetch); other readables ignore it. */
  get(key, options = {}) {
    return this.#call(options, () => this.#readable.get(key, { signal: options.signal, ...(options.reload ? { cache: 'reload' } : {}) }));
  }

  getRange(key, range, options = {}) {
    return this.#call(options, () => this.#readable.getRange(key, range, { signal: options.signal }));
  }

  #call({ signal, priority = 0 }, read) {
    return this.#limiter.run(priority, signal, async () => {
      this.#network.requests++;
      this.#bandwidth.begin();
      let received = 0;
      try {
        const bytes = await read();
        received = bytes?.length ?? 0;
        this.#network.bytes += received;
        return bytes;
      } finally {
        this.#bandwidth.end(received);
      }
    });
  }
}
