// Shared helpers for the field-performance scripts in this directory (Phase 0 baseline, 2026-10-16).
//
// Method follows bench/live/measure.mjs: Playwright chromium (channel 'chromium', real GPU through ANGLE/Metal),
// 1440x900 at DPR 2, a new browser process for every run, the HTTP cache disabled over CDP. Not the Claude app
// browser pane (rAF runs at 1 Hz there when it is hidden).

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '../../js/node_modules/playwright/index.mjs';

// FIELDPERF_SITE and FIELDPERF_STORE point the scripts at a local viewer and a local copy of the store
// (for example http://127.0.0.1:8231/js/demo/index.html and http://127.0.0.1:8231/stores/ucayali_santa_maria_v03).
export const SITE = process.env.FIELDPERF_SITE ?? 'https://chronozarr.org/demo/';
export const STORE = process.env.FIELDPERF_STORE ?? 'https://data.chronozarr.org/ucayali_santa_maria_v03';

/** The address of a visit. The catalog's default store is the public one, so a local site (FIELDPERF_SITE) always names its store. */
export function viewerUrl({ t = null, store = null } = {}) {
  const query = [];
  if (store !== null || process.env.FIELDPERF_SITE) query.push(`store=${encodeURIComponent(store ?? STORE)}`);
  if (t !== null) query.push(`t=${t}`);
  return query.length ? `${SITE}?${query.join('&')}` : SITE;
}
// Raw data and summaries go to one directory per measurement date. FIELDPERF_OUT overrides the default (the 2026-10-18 resume),
// so an analysis script can be pointed at the 2026-10-16 directory, whose data stays as it was written.
export const HERE = path.dirname(fileURLToPath(import.meta.url));
// data/fieldperf of the checkout that holds this script; a worktree has no data/, so set FIELDPERF_OUT there.
export const DATA_ROOT = path.resolve(HERE, '../../data/fieldperf');
export const OUT_DIR = process.env.FIELDPERF_OUT ?? `${DATA_ROOT}/2026-10-18`;

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function appendJsonl(file, obj) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.appendFileSync(file, `${JSON.stringify(obj)}\n`);
}

export function readJsonl(file) {
  if (!fs.existsSync(file)) return [];
  return fs
    .readFileSync(file, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

/** Seeded PRNG (mulberry32), so a sample of random timesteps and cells can be reproduced. */
export function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function launchOptions() {
  return { channel: 'chromium', headless: true, args: ['--ignore-gpu-blocklist', '--use-angle=metal'] };
}

/**
 * Open a browser the way measure.mjs does and attach a CDP request log.
 * The log keeps one record per request with the timing breakdown Chromium reports; `wallTime` is epoch seconds.
 */
export async function launch({ throttle = null } = {}) {
  const browser = await chromium.launch(launchOptions());
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, serviceWorkers: 'block' });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
  if (throttle) {
    await cdp.send('Network.emulateNetworkConditions', {
      offline: false,
      latency: throttle.latencyMs,
      downloadThroughput: throttle.bytesPerSecond,
      uploadThroughput: throttle.bytesPerSecond,
      connectionType: 'cellular4g',
    });
  }
  const requests = new Map();
  const order = [];
  const errors = [];
  const consoleErrors = [];
  cdp.on('Network.requestWillBeSent', (e) => {
    // A redirect reuses the request id; the earlier hop is finished by its own response.
    const record = {
      id: e.requestId,
      url: e.request.url,
      method: e.request.method,
      type: e.type,
      initiator: e.initiator?.type,
      wallTime: e.wallTime,
      requestTs: e.timestamp,
      range: e.request.headers?.Range ?? e.request.headers?.range ?? null,
    };
    requests.set(e.requestId, record);
    order.push(record);
  });
  cdp.on('Network.responseReceived', (e) => {
    const r = requests.get(e.requestId);
    if (!r) return;
    const h = lowerKeys(e.response.headers);
    r.status = e.response.status;
    r.protocol = e.response.protocol;
    r.remoteIP = e.response.remoteIPAddress;
    r.connectionReused = e.response.connectionReused;
    r.connectionId = e.response.connectionId;
    r.fromCache = e.response.fromDiskCache || e.response.fromPrefetchCache || false;
    r.contentLength = h['content-length'] ? Number(h['content-length']) : null;
    r.contentType = h['content-type'] ?? null;
    r.contentEncoding = h['content-encoding'] ?? null;
    r.cfCacheStatus = h['cf-cache-status'] ?? null;
    r.cfRay = h['cf-ray'] ?? null;
    r.age = h.age ?? null;
    r.cacheControl = h['cache-control'] ?? null;
    r.timing = e.response.timing ?? null;
    r.responseTs = e.timestamp;
  });
  cdp.on('Network.loadingFinished', (e) => {
    const r = requests.get(e.requestId);
    if (!r) return;
    r.finishTs = e.timestamp;
    r.encodedDataLength = e.encodedDataLength;
  });
  cdp.on('Network.loadingFailed', (e) => {
    const r = requests.get(e.requestId);
    if (!r) return;
    r.failed = e.errorText;
    r.canceled = Boolean(e.canceled);
    r.finishTs = e.timestamp;
  });
  page.on('pageerror', (error) => errors.push(String(error.message).slice(0, 300)));
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text().slice(0, 300));
  });
  return { browser, context, page, cdp, requests: order, errors, consoleErrors };
}

function lowerKeys(headers = {}) {
  return Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
}

/** Plain-data version of a request record, with times in ms from `t0Ts` (a CDP monotonic timestamp in seconds). */
export function describeRequest(r, t0Ts) {
  const ms = (ts) => (ts == null ? null : Math.round((ts - t0Ts) * 1000 * 10) / 10);
  const t = r.timing;
  const url = new URL(r.url);
  const colo = r.cfRay ? r.cfRay.split('-').pop() : null;
  return {
    url: r.url,
    host: url.host,
    path: url.pathname + url.search,
    method: r.method,
    type: r.type,
    initiator: r.initiator,
    status: r.status ?? null,
    protocol: r.protocol ?? null,
    connectionId: r.connectionId ?? null,
    connectionReused: r.connectionReused ?? null,
    contentLength: r.contentLength,
    contentEncoding: r.contentEncoding,
    encodedDataLength: r.encodedDataLength ?? null,
    cfCacheStatus: r.cfCacheStatus,
    colo,
    age: r.age,
    startMs: ms(r.requestTs),
    headersMs: ms(r.responseTs),
    finishMs: ms(r.finishTs),
    // Chromium's breakdown, in ms relative to the request start; -1 means the phase did not happen (reused connection).
    dnsMs: t && t.dnsStart >= 0 ? round1(t.dnsEnd - t.dnsStart) : null,
    connectMs: t && t.connectStart >= 0 ? round1(t.connectEnd - t.connectStart) : null,
    sslMs: t && t.sslStart >= 0 ? round1(t.sslEnd - t.sslStart) : null,
    ttfbMs: t ? round1(t.receiveHeadersEnd - t.sendEnd) : null,
    downloadMs: r.finishTs != null && r.responseTs != null ? round1((r.finishTs - r.responseTs) * 1000) : null,
    wallTime: r.wallTime,
    failed: r.failed ?? null,
    canceled: r.canceled ?? false,
  };
}

function round1(x) {
  return Math.round(x * 10) / 10;
}

/** A request for something inside the store (metadata, coordinates, chunks, masks), not the page or its modules. */
export function isStoreRequest(r) {
  return r.url.startsWith(STORE);
}

/** What a store request is, from its path: a data, mask or coverage chunk of a level, or metadata. */
export function classifyStorePath(urlPath) {
  const match = /\/(\d+)\/(data|mask|coverage)\/c\/[\d/]+$/.exec(urlPath.split('?')[0]);
  return match ? { kind: match[2], level: Number(match[1]) } : { kind: 'meta', level: null };
}

/**
 * Wire bytes of the store requests (as describeRequest returns them) that started between `fromS` and `toS`, epoch seconds,
 * by kind and, for data chunks, by level. A request the page aborted reports no bytes over CDP: `aborted` counts them.
 */
export function wireSummary(described, fromS, toS) {
  const out = { requests: 0, bytes: 0, aborted: 0, failed: 0, data: { n: 0, bytes: 0, byLevel: {} }, mask: { n: 0, bytes: 0 }, coverage: { n: 0, bytes: 0 }, meta: { n: 0, bytes: 0 } };
  for (const r of described) {
    if (!r.url.startsWith(STORE) || r.method === 'OPTIONS' || r.wallTime < fromS || r.wallTime > toS) continue;
    const { kind, level } = classifyStorePath(r.path);
    const bytes = r.encodedDataLength ?? 0;
    out.requests++;
    out.bytes += bytes;
    out[kind].n++;
    out[kind].bytes += bytes;
    if (kind === 'data') {
      const entry = (out.data.byLevel[level] ??= { n: 0, bytes: 0 });
      entry.n++;
      entry.bytes += bytes;
    }
    if (r.canceled) out.aborted++;
    else if (r.failed || (r.status && r.status >= 400)) out.failed++;
  }
  return out;
}

export function systemState() {
  const [l1, l5] = os.loadavg();
  return { load1: Math.round(l1 * 100) / 100, load5: Math.round(l5 * 100) / 100, cpus: os.cpus().length };
}

/** Median gap between animation frames over `ms`, a cheap check of what refresh rate and main-thread load the page gets. */
export function rafMedian(page, ms = 1000) {
  return page.evaluate(
    (duration) =>
      new Promise((resolve) => {
        const gaps = [];
        let last = performance.now();
        const start = last;
        const tick = (now) => {
          gaps.push(now - last);
          last = now;
          if (now - start < duration) requestAnimationFrame(tick);
          else {
            gaps.sort((a, b) => a - b);
            resolve(gaps[Math.floor(gaps.length / 2)]);
          }
        };
        requestAnimationFrame(tick);
      }),
    ms,
  );
}

// ---- statistics ----

export function quantile(sorted, q) {
  // Linear interpolation between order statistics (type 7, the default of R and numpy).
  if (sorted.length === 0) return NaN;
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

/** Percentile bootstrap: `resamples` draws with replacement; returns the 2.5 % and 97.5 % points of the statistic. */
export function bootstrapCI(values, q, { resamples = 10000, seed = 12345, level = 0.95 } = {}) {
  const n = values.length;
  if (n < 2) return [NaN, NaN];
  const rand = rng(seed);
  const stats = new Float64Array(resamples);
  const draw = new Float64Array(n);
  for (let b = 0; b < resamples; b++) {
    for (let i = 0; i < n; i++) draw[i] = values[Math.floor(rand() * n)];
    draw.sort();
    stats[b] = quantile(draw, q);
  }
  stats.sort();
  const alpha = (1 - level) / 2;
  return [quantile(stats, alpha), quantile(stats, 1 - alpha)];
}

/** n, p50, p95 with bootstrap 95 % CIs (10,000 resamples). */
export function summarize(values, opts = {}) {
  const v = values.filter((x) => Number.isFinite(x));
  const sorted = [...v].sort((a, b) => a - b);
  const p50 = quantile(sorted, 0.5);
  const p95 = quantile(sorted, 0.95);
  const [p50lo, p50hi] = bootstrapCI(v, 0.5, opts);
  const [p95lo, p95hi] = bootstrapCI(v, 0.95, { ...opts, seed: (opts.seed ?? 12345) + 1 });
  return { n: v.length, min: sorted[0], p50, p50ci: [p50lo, p50hi], p95, p95ci: [p95lo, p95hi], max: sorted.at(-1) };
}

export function fmtSummary(s, digits = 0) {
  const f = (x) => (Number.isFinite(x) ? x.toFixed(digits) : 'n/a');
  return `N=${s.n}  p50 ${f(s.p50)} [${f(s.p50ci[0])}, ${f(s.p50ci[1])}]  p95 ${f(s.p95)} [${f(s.p95ci[0])}, ${f(s.p95ci[1])}]`;
}
