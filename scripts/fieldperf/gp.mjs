// Minimal Globalping client (https://api.globalping.io, unauthenticated) for http GET measurements.
//
// Limits (from https://api.globalping.io/v1/spec.yaml, 2026-10-16): unauthenticated 250 tests/hour (one test = one
// probe running one measurement), at most 50 probes per measurement, measurement status 2 requests/second each.
// This client sends one request at a time, polls no faster than every 1.2 s, and on 429 sleeps until the limit resets.

import { sleep } from './lib.mjs';

const API = 'https://api.globalping.io';
const USER_AGENT = 'chronozarr-fieldperf/0.1 (Phase 0 baseline, a few hundred tests; contact james.gearon@unc.edu)';

export const rate = { limit: null, remaining: null, resetAt: 0, consumedTotal: 0, requests429: 0 };

function noteRate(headers) {
  const num = (k) => (headers.get(k) != null ? Number(headers.get(k)) : null);
  const limit = num('x-ratelimit-limit');
  const remaining = num('x-ratelimit-remaining');
  const reset = num('x-ratelimit-reset');
  if (limit != null) rate.limit = limit;
  if (remaining != null) rate.remaining = remaining;
  if (reset != null) rate.resetAt = Date.now() + reset * 1000;
  const consumed = num('x-ratelimit-consumed');
  if (consumed != null) rate.consumedTotal += consumed;
}

async function api(method, path, body, extraHeaders = {}) {
  const response = await fetch(`${API}${path}`, {
    method,
    headers: { 'User-Agent': USER_AGENT, Accept: 'application/json', 'Accept-Encoding': 'gzip, br', ...(body ? { 'Content-Type': 'application/json' } : {}), ...extraHeaders },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(30000),
  });
  noteRate(response.headers);
  return response;
}

/** Wait out the hourly window when no test is left (or when the API said 429), then continue. */
export async function waitForBudget(needed = 1) {
  if (rate.remaining !== null && rate.remaining < needed) {
    const waitMs = Math.max(0, rate.resetAt - Date.now()) + 3000;
    console.error(`[gp] rate limit exhausted (${rate.remaining} left), sleeping ${Math.round(waitMs / 1000)} s`);
    await sleep(waitMs);
    rate.remaining = null;
  }
}

/**
 * Create an http GET measurement. `locations` is a Globalping locations array, or the id of an earlier measurement
 * to reuse the very same probes. Returns {id, probesCount}. Retries a 429 after the reset the API reports.
 */
export async function createHttp({ target, path, query = '', locations, limit = 1, headers = {}, protocol = 'HTTP2', timeout = 30 }) {
  const body = {
    type: 'http',
    target,
    locations,
    ...(typeof locations === 'string' ? {} : { limit }),
    timeout,
    measurementOptions: { request: { path, method: 'GET', ...(query ? { query } : {}), headers }, protocol },
  };
  for (let attempt = 0; attempt < 6; attempt++) {
    await waitForBudget(typeof locations === 'string' ? 1 : limit);
    const response = await api('POST', '/v1/measurements', body);
    if (response.status === 202) {
      const json = await response.json();
      return { id: json.id, probesCount: json.probesCount };
    }
    if (response.status === 429) {
      rate.requests429 += 1;
      const retryAfter = Number(response.headers.get('retry-after') ?? 60);
      const waitMs = (Number.isFinite(retryAfter) ? retryAfter : 60) * 1000 + 3000 * (attempt + 1);
      console.error(`[gp] 429, sleeping ${Math.round(waitMs / 1000)} s`);
      rate.remaining = 0;
      rate.resetAt = Date.now() + waitMs;
      await sleep(waitMs);
      continue;
    }
    const text = await response.text();
    throw new Error(`Globalping POST /v1/measurements -> ${response.status}: ${text.slice(0, 400)}`);
  }
  throw new Error('Globalping POST /v1/measurements kept returning 429');
}

/** Poll until the measurement is finished (the API itself times each test out at `timeout` seconds). */
export async function waitFinished(id, { maxMs = 120000 } = {}) {
  const started = Date.now();
  let etag = null;
  let pollMs = 1500;
  while (Date.now() - started < maxMs) {
    await sleep(pollMs);
    const response = await api('GET', `/v1/measurements/${id}`, null, etag ? { 'If-None-Match': etag } : {});
    if (response.status === 304) continue;
    if (response.status === 429) {
      await sleep(5000);
      continue;
    }
    if (!response.ok) throw new Error(`Globalping GET ${id} -> ${response.status}`);
    etag = response.headers.get('etag');
    const json = await response.json();
    if (json.status !== 'in-progress') return json;
    pollMs = Math.min(pollMs + 300, 3000);
  }
  throw new Error(`measurement ${id} not finished after ${maxMs} ms`);
}

/** The fields of one finished http test that the analysis uses; the 10 kB of body text is dropped (binary chunk data). */
export function distill(measurement, resultIndex = 0) {
  const result = measurement.results[resultIndex];
  const probe = result?.probe ?? {};
  const r = result?.result ?? {};
  const h = Object.fromEntries(Object.entries(r.headers ?? {}).map(([k, v]) => [k.toLowerCase(), v]));
  const cfRay = typeof h['cf-ray'] === 'string' ? h['cf-ray'] : null;
  return {
    measurementId: measurement.id,
    createdAt: measurement.createdAt,
    status: r.status ?? null,
    probe: {
      continent: probe.continent,
      country: probe.country,
      city: probe.city,
      asn: probe.asn,
      network: probe.network,
      tags: probe.tags,
      resolvers: probe.resolvers,
    },
    statusCode: r.statusCode ?? null,
    httpVersion: r.rawHeaders?.split('\n')[0] ?? null,
    resolvedAddress: r.resolvedAddress ?? null,
    timings: r.timings ?? null,
    cfCacheStatus: h['cf-cache-status'] ?? null,
    cfRay,
    colo: cfRay ? cfRay.split('-').pop() : null,
    age: h.age ?? null,
    contentLength: h['content-length'] ? Number(h['content-length']) : null,
    contentEncoding: h['content-encoding'] ?? null,
    etag: h.etag ?? null,
    truncated: r.truncated ?? null,
    bodyBytesSeen: typeof r.rawBody === 'string' ? r.rawBody.length : null,
    error: r.status && r.status !== 'finished' ? (r.rawOutput ?? '').slice(0, 300) : null,
  };
}
