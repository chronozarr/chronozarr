// Records every HTTP request made through the global `fetch`, which both geotiff.js and the chronozarr reader use:
// URL, method, byte range, status, body bytes, and the context (phase and step) the request was made under.
// `context.run({phase, step}, fn)` tags the requests of `fn`, however deep and however concurrent.

import { AsyncLocalStorage } from 'node:async_hooks';

export const context = new AsyncLocalStorage();

/** Wraps globalThis.fetch. The response body is read here, so the caller gets an equivalent in-memory Response. */
export function installRecorder() {
  const realFetch = globalThis.fetch;
  const log = [];
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    const entry = { url: request.url, method: request.method, range: request.headers.get('range'), ctx: context.getStore() ?? null, startedAt: performance.now(), endedAt: null, status: null, bytes: 0 };
    log.push(entry);
    const response = await realFetch(request);
    entry.status = response.status;
    const body = request.method === 'HEAD' ? null : await response.arrayBuffer();
    entry.bytes = body?.byteLength ?? 0;
    entry.endedAt = performance.now();
    return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
  };
  return {
    log,
    restore() {
      globalThis.fetch = realFetch;
    },
  };
}

/** `bytes=a-b` as [start, end] inclusive, or null (no range or a suffix range). */
export function parseRange(range) {
  const match = /^bytes=(\d+)-(\d+)$/.exec(range ?? '');
  return match ? [Number(match[1]), Number(match[2])] : null;
}
