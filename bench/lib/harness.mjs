// Shared by the zarr-layer and COG drivers: a static server for the repository, headless Chromium with GPU-backed
// WebGL, CDP network throttling and request accounting from CDP's own events (bytes on the wire, including headers).

import path from 'node:path';
import { chromium } from 'playwright';
import { startStaticServer } from '../../js/support/static-server.js';

export const REPO_ROOT = path.resolve(import.meta.dirname, '../..');

/** Link profiles: CDP throttling after page load. `natural` leaves the link as it is. */
export const PROFILES = {
  natural: null,
  '50Mbit-40ms': { mbps: 50, latencyMs: 40 },
  '10Mbit-100ms': { mbps: 10, latencyMs: 100 },
};

export const startServer = () => startStaticServer(REPO_ROOT);

/** Headless Chromium on the Metal ANGLE backend (the GPU of this machine), as js/support/reader-bench does. */
export const launchBrowser = () => chromium.launch({ headless: true, args: ['--ignore-gpu-blocklist', '--use-angle=metal'] });

/**
 * A fresh context (empty HTTP cache, no service workers) with one page. The viewer's catalog is made to 404 so that
 * the page opens nothing by itself and the driver decides what is opened, when, and at which level and camera.
 */
export async function newPage(browser, { width = 1500, height = 1500, blockCatalog = false } = {}) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1, serviceWorkers: 'block' });
  const page = await context.newPage();
  if (blockCatalog) {
    await page.addInitScript(() => {
      const original = window.fetch;
      window.fetch = (input, init) => (String(input?.url ?? input).endsWith('catalog.json') ? Promise.resolve(new Response(null, { status: 404 })) : original(input, init));
    });
  }
  return { context, page };
}

/**
 * Counts requests and bytes of one page from CDP events. A request's bytes are `encodedDataLength` of its
 * loadingFinished event (headers and body as transferred); for a request that did not finish (aborted), the body bytes
 * received before it ended. Requests are classified: `preflight` (CORS OPTIONS), `head`, `metadata` (zarr.json,
 * COG headers are not distinguishable here), `data` (everything else). `aborted` counts requests the page cancelled, with the
 * body bytes they had received (a lower bound of what crossed the wire: data still in flight when a stream is reset is not seen). Only URLs starting with one of `origins` count.
 */
export class NetCounter {
  #requests = new Map();
  #origins;
  #cdp;

  constructor(cdp, origins) {
    this.#cdp = cdp;
    this.#origins = origins;
    cdp.on('Network.requestWillBeSent', (e) => {
      if (!this.#origins.some((o) => e.request.url.startsWith(o))) return;
      const kind = e.type === 'Preflight' || e.request.method === 'OPTIONS' ? 'preflight' : e.request.method === 'HEAD' ? 'head' : /zarr\.json(\?|$)/.test(e.request.url) ? 'metadata' : 'data';
      this.#requests.set(e.requestId, { url: e.request.url, method: e.request.method, range: e.request.headers.Range ?? e.request.headers.range ?? null, kind, startedAt: e.timestamp, responseAt: null, endedAt: null, body: 0, wire: null, status: null, ended: false, failed: false, cache: null, protocol: null });
    });
    cdp.on('Network.responseReceived', (e) => {
      const r = this.#requests.get(e.requestId);
      if (r) Object.assign(r, { responseAt: e.timestamp, status: e.response.status, cache: e.response.headers['cf-cache-status'] ?? null, protocol: e.response.protocol ?? null });
    });
    cdp.on('Network.dataReceived', (e) => {
      const r = this.#requests.get(e.requestId);
      if (r) r.body += e.encodedDataLength;
    });
    cdp.on('Network.loadingFinished', (e) => {
      const r = this.#requests.get(e.requestId);
      if (r) Object.assign(r, { wire: e.encodedDataLength, ended: true, endedAt: e.timestamp });
    });
    cdp.on('Network.loadingFailed', (e) => {
      const r = this.#requests.get(e.requestId);
      if (r) Object.assign(r, { ended: true, failed: true, endedAt: e.timestamp });
    });
  }

  /** Cumulative {requests, bytes, byKind, cache}; `cache` counts Cloudflare's cf-cache-status values. */
  snapshot() {
    const byKind = {};
    const cache = {};
    let bytes = 0;
    let requests = 0;
    for (const r of this.#requests.values()) {
      if (r.cache) cache[r.cache] = (cache[r.cache] ?? 0) + 1;
      const wire = r.wire ?? r.body;
      bytes += wire;
      requests++;
      const k = (byKind[r.kind] ??= { requests: 0, bytes: 0, aborted: 0, abortedBytes: 0 });
      k.requests++;
      k.bytes += wire;
      if (r.failed) {
        k.aborted++;
        k.abortedBytes += wire;
      }
    }
    return { requests, bytes, byKind, cache };
  }

  /** The raw requests (for diagnostics). */
  entries() {
    return [...this.#requests.values()];
  }
}

export const subtract = (after, before) => {
  const byKind = {};
  for (const kind of new Set([...Object.keys(after.byKind), ...Object.keys(before.byKind)])) {
    const a = after.byKind[kind] ?? { requests: 0, bytes: 0, aborted: 0, abortedBytes: 0 };
    const b = before.byKind[kind] ?? { requests: 0, bytes: 0, aborted: 0, abortedBytes: 0 };
    byKind[kind] = { requests: a.requests - b.requests, bytes: a.bytes - b.bytes, aborted: a.aborted - b.aborted, abortedBytes: a.abortedBytes - b.abortedBytes };
  }
  const cache = {};
  for (const key of new Set([...Object.keys(after.cache ?? {}), ...Object.keys(before.cache ?? {})])) cache[key] = (after.cache?.[key] ?? 0) - (before.cache?.[key] ?? 0);
  return { requests: after.requests - before.requests, bytes: after.bytes - before.bytes, byKind, cache };
};

/** Attach CDP accounting to a page: also disables the HTTP cache so that bytes mean network bytes. */
export async function attachNet(context, page, origins) {
  const cdp = await context.newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
  const counter = new NetCounter(cdp, origins);
  return { cdp, counter };
}

export async function applyProfile(cdp, profile) {
  if (!profile) {
    await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    return;
  }
  const bytesPerSecond = (profile.mbps * 1e6) / 8;
  await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: profile.latencyMs, downloadThroughput: bytesPerSecond, uploadThroughput: bytesPerSecond });
}

export const round = (x, digits = 1) => (x === null || x === undefined ? null : Math.round(x * 10 ** digits) / 10 ** digits);
