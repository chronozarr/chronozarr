// MODEL, not measurement: the cold open of the deployed viewer for a first-time visitor in each region.
//
//   node scripts/fieldperf/model.mjs [--date 2026-10-18] [--validate-date 2026-10-18] [--md]
//
// Structure (from the request list of a real cold open, cold_open_bare1.json and browser_opens.jsonl):
//   boot      navigation start -> the page's first fetch (catalog.json): TCP + TLS + HTML (3 round trips), then the ES-module
//             graph, a median of 6 sequential levels (4 to 9), plus local compile and evaluation.
//             boot = a + 9 x RTT, `a` calibrated per measurement date from the browser runs (local time and the site's Worker wait).
//   catalog   1 round trip.
//   Staging (js/demo/coarse.js): the level-1 batch is requested when the coarse chunk has landed, or stageLead = clamp(2 x open time, 30, 400 ms)
//             after the coarse chunk was requested if it has not; then the two share the link (coarse chunk weight fitted, see L3_WEIGHT).
//   metadata  DNS, TCP, TLS 1.3 and the zarr.json request to data.chronozarr.org: DNS + 3 x RTT + W_meta.
//   coarse    one level-3 chunk (about 0.7 MB): RTT + W + bytes / bandwidth, then decode and paint -> "first whole frame".
//   batch     nine level-1 chunks (about 11 MB) requested together on one connection: RTT, each chunk's own server wait W_i, then the
//             bytes share the link equally (fluid processor-sharing) -> "frame at the target level".
// W = a response's first-byte time minus the probe's TCP connect time (one round trip), i.e. the server's own wait: tens of ms for a HIT,
// hundreds for a MISS, because the object is pulled from the R2 bucket (ENAM) to the PoP. W comes from the Globalping samples of the
// region, labelled by cf-cache-status (cold = non-HIT, warm = HIT). The nine W_i are independent draws (not measured for parallel misses).
// RTT and bandwidth are access scenarios (good 25 ms / 25 MB/s, typical 50 / 10, poor 150 / 2, slow 250 / 0.7), the same for every region: no probe
// here measures a visitor's last mile. What differs by region is W and, for Singapore on 2026-10-16, which PoP answered.
//
// Validation: the same structure, fed with each browser run's own RTT and measured bandwidth and with W from the Globalping Ashburn
// samples of the same date, is compared with the run's measured frame times.

import fs from 'node:fs';
import path from 'node:path';
import { DATA_ROOT, quantile, readJsonl, rng } from './lib.mjs';

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(`--${name}`) ? args[args.indexOf(`--${name}`) + 1] : fallback);
const date = opt('date', '2026-10-18');
const md = args.includes('--md');
const DIR = path.join(DATA_ROOT, date);
const REGIONS = ['ashburn', 'frankfurt', 'sao_paulo', 'singapore'];
const DNS_MS = 50;
const DRAWS = 10000;
const BOOT_ROUND_TRIPS = 9; // default for a region: TCP + TLS + HTML (3) + 6 sequential module levels (median of both dates, range 4 to 9)

// ---- Globalping inputs ----
// The Globalping samples of the dates in --gp-dates (default: both 2026-10-16 and 2026-10-18); each row keeps its date.
const gpDates = opt('gp-dates', '2026-10-16,2026-10-18').split(',');
function loadGlobalping(dates) {
  const rows = [];
  for (const d of dates) {
    const dir = path.join(DATA_ROOT, d);
    for (const f of fs.existsSync(dir) ? fs.readdirSync(dir) : []) if (/^globalping_(chunks|meta|plain)_.*\.jsonl$/.test(f)) rows.push(...readJsonl(path.join(dir, f)).map((r) => ({ ...r, date: d })));
  }
  return rows.filter((r) => r.statusCode === 200 && r.timings);
}
const gpRows = loadGlobalping(gpDates);
const waits = (rows) => rows.map((r) => r.timings.firstByte - r.timings.tcp).filter((x) => Number.isFinite(x) && x >= 0);
function waitsFor(region, kind, hit, tagSet = 'dc') {
  const rows = gpRows.filter((r) => r.region === region && r.tagSet === tagSet && (kind === 'any' ? r.kind !== 'plain' : r.kind === kind) && (r.cfCacheStatus === 'HIT') === hit);
  return waits(rows);
}
// ---- browser inputs: calibration of the local constants ----
function loadRuns(d) {
  return readJsonl(path.join(DATA_ROOT, d, 'browser_opens.jsonl')).filter((r) => !r.error && r.requests);
}
const median = (a) => {
  const s = a.filter(Number.isFinite).sort((x, y) => x - y);
  return s.length ? quantile(s, 0.5) : NaN;
};
function phases(r) {
  const rq = r.requests;
  const doc = rq[0];
  const cat = rq.find((q) => q.path.endsWith('catalog.json'));
  const zj = rq.find((q) => q.path.split('?')[0].endsWith('/zarr.json') && q.host === 'data.chronozarr.org');
  const pre = rq.filter((q) => q.beforeFirstFrame && q.host === 'data.chronozarr.org' && q.path.includes('/data/c/'));
  const l3 = pre.find((q) => q.path.includes('/3/data/c/'));
  const l1 = pre.filter((q) => q.path.includes('/1/data/c/') && q.finishMs != null);
  if (!doc || !cat || !zj || !l3 || l1.length === 0) return null;
  // Sequential levels of the module graph: groups of module requests that start together (within 15 ms), before catalog.json is fetched.
  const mods = rq.filter((q) => q.type === 'Script' && q.host === 'chronozarr.org' && q.startMs < cat.startMs).sort((a, b) => a.startMs - b.startMs);
  let moduleLevels = 0;
  let lastStart = -Infinity;
  for (const q of mods) {
    if (q.startMs - lastStart > 15) moduleLevels += 1;
    lastStart = q.startMs;
  }
  const l1Start = Math.min(...l1.map((q) => q.startMs));
  const l1End = Math.max(...l1.map((q) => q.finishMs));
  return {
    // The round trip is the TCP connect to data.chronozarr.org (always TCP, so it means the same in every run); the document's own
    // connection is QUIC in about two thirds of the 2026-10-18 runs (one handshake round trip instead of two).
    rtt: zj.connectMs - (zj.sslMs ?? 0),
    // Handshake and HTML (TCP + TLS 1.3 + request = 3 round trips; QUIC + request = 2), then one round trip per level of the module graph.
    moduleLevels,
    bootRoundTrips: (doc.protocol === 'h3' ? 2 : 3) + moduleLevels,
    boot: cat.startMs,
    catalog: cat.finishMs - cat.startMs,
    metaStart: zj.startMs,
    meta: zj.finishMs - zj.startMs,
    metaW: zj.ttfbMs - (zj.connectMs - (zj.sslMs ?? 0)),
    l3: { start: l3.startMs, finish: l3.finishMs, bytes: l3.encodedDataLength, ttfb: l3.ttfbMs },
    l1: l1.map((q) => ({ bytes: q.encodedDataLength, ttfb: q.ttfbMs, start: q.startMs, finish: q.finishMs })),
    l1Start,
    l1End,
    l1Bytes: l1.reduce((n, q) => n + q.encodedDataLength, 0),
    coarsePage: r.coarseFramePageMs,
    targetPage: r.firstFramePageMs,
    l3Finish: l3.finishMs,
    openMs: r.ready.openMs,
    overlapped: l1Start < l3.finishMs,
    loadStartPage: r.loadStoreStartPageMs,
    cfL1Hit: r.beforeFirstFrame.level1Hit,
  };
}

/**
 * Fluid processor-sharing: flow i starts moving bytes at starts[i] ms; the active flows share bw (bytes/ms) equally.
 * Returns each flow's finish time.
 */
function shareLinkTimes(starts, sizes, bwBytesPerMs, weights = starts.map(() => 1)) {
  const n = starts.length;
  const rem = [...sizes];
  const finish = Array(n).fill(NaN);
  let t = 0;
  let left = n;
  while (left > 0) {
    const active = [];
    for (let i = 0; i < n; i++) if (rem[i] > 0 && starts[i] <= t) active.push(i);
    const nextStart = Math.min(Infinity, ...starts.filter((x, i) => rem[i] > 0 && x > t));
    if (active.length === 0) {
      t = nextStart;
      continue;
    }
    const totalWeight = active.reduce((n, i) => n + weights[i], 0);
    const rateOf = (i) => (bwBytesPerMs * weights[i]) / totalWeight;
    const soonestDone = Math.min(...active.map((i) => rem[i] / rateOf(i)));
    const dt = Math.min(soonestDone, nextStart - t);
    for (const i of active) rem[i] -= rateOf(i) * dt;
    t += dt;
    for (const i of active) {
      if (rem[i] <= 1e-6) {
        rem[i] = 0;
        finish[i] = t;
        left -= 1;
      }
    }
  }
  return finish;
}

const shareLink = (starts, sizes, bw) => Math.max(...shareLinkTimes(starts, sizes, bw));

/**
 * Share of a shared link the coarse chunk gets, against 1 for each level-1 chunk. Equal sharing predicts the coarse frame far too late on
 * the 2026-10-16 link (the coarse chunk is requested first and Chromium serves it ahead of the later streams); the weight is fitted on the
 * runs of either date in which the two overlapped, by the least median absolute error of the coarse frame time.
 */
const L3_WEIGHT = { value: 1 };

/** coarse.js stageLeadMs: the next stage starts two store round trips after the previous one, if that one has not landed by then (30-400 ms). */
const stageLead = (roundTripMs) => Math.min(400, Math.max(30, 2 * roundTripMs));

/**
 * Predicted frame times (ms from navigation start) for one set of inputs. The viewer stages the first frame: the coarse chunk
 * (level 3) is requested when the store is open; the nine level-1 chunks are requested when it has landed (plus a local planning gap),
 * or stageLead(open time) after it was requested if it has not landed by then, in which case the two share the link.
 */
function predict({ rtt, wMeta, wL3, wL1, l3Bytes, l1Sizes, bwBytesPerMs, local, bootRoundTrips = BOOT_ROUND_TRIPS }) {
  const boot = local.boot + bootRoundTrips * rtt;
  const catalog = rtt + local.catalogServer;
  const meta = DNS_MS + 3 * rtt + wMeta;
  const open = boot + catalog + meta;
  const l3Request = open + local.betweenMetaAndL3;
  const l3Flows = l3Request + rtt + wL3;
  const l3Alone = l3Flows + l3Bytes / bwBytesPerMs;
  const lead = stageLead(meta + local.openLocal);
  const sequential = l3Alone + local.gapAfterL3 <= l3Request + lead;
  const batchRequest = sequential ? l3Alone + local.gapAfterL3 : l3Request + lead;
  const starts = [l3Flows, ...wL1.map((w) => batchRequest + rtt + w)];
  const finish = shareLinkTimes(starts, [l3Bytes, ...l1Sizes], bwBytesPerMs, [L3_WEIGHT.value, ...l1Sizes.map(() => 1)]);
  const coarse = finish[0] + local.coarseDecode;
  const target = Math.max(...finish.slice(1)) + local.finalPaint;
  const fromCall = (x) => x - boot - catalog;
  return { coarsePage: coarse, targetPage: target, coarseFromCall: fromCall(coarse), targetFromCall: fromCall(target), overlapped: !sequential };
}

function calibrate(runs) {
  const ph = runs.map(phases).filter(Boolean);
  const local = {
    boot: median(ph.map((p) => p.boot - p.bootRoundTrips * p.rtt)),
    catalogServer: median(ph.map((p) => p.catalog - p.rtt)),
    betweenMetaAndL3: median(ph.map((p) => p.l3.start - (p.metaStart + p.meta))),
    coarseDecode: median(ph.map((p) => p.coarsePage - p.l3.finish)),
    // From the runs in which the batch was requested after the coarse chunk had landed (the others overlapped, see stageLead).
    gapAfterL3: median(ph.filter((p) => p.l1Start >= p.l3.finish).map((p) => p.l1Start - p.l3.finish)),
    // Time the page spends between the store opening and its own `openMs` mark, beyond the zarr.json fetch.
    openLocal: median(ph.map((p) => p.openMs - p.meta)),
    finalPaint: median(ph.map((p) => p.targetPage - p.l1End)),
  };
  // Does the boot time really scale with ten round trips? An ordinary least-squares slope, when the RTT varies enough to tell.
  const xs = ph.map((p) => p.rtt);
  const ys = ph.map((p) => p.boot);
  const meanRoundTrips = ph.reduce((n, p) => n + p.bootRoundTrips, 0) / ph.length;
  const mx = xs.reduce((a, b) => a + b, 0) / xs.length;
  const my = ys.reduce((a, b) => a + b, 0) / ys.length;
  const sxx = xs.reduce((a, x) => a + (x - mx) ** 2, 0);
  const slope = sxx > 1 ? xs.reduce((a, x, i) => a + (x - mx) * (ys[i] - my), 0) / sxx : NaN;
  const over = ph.filter((p) => p.overlapped);
  const staging = {
    overlappedShare: over.length / ph.length,
    observedLeadMs: median(over.map((p) => p.l1Start - p.l3.start)),
    predictedLeadMs: median(over.map((p) => stageLead(p.meta + local.openLocal))),
  };
  return { n: ph.length, staging, local, rttSpread: { min: Math.min(...xs), p50: median(xs), max: Math.max(...xs) }, bootSlopeRoundTrips: slope, bootRoundTripsAssumed: meanRoundTrips, phases: ph };
}

const out = { generatedAt: new Date().toISOString(), date, assumptions: { BOOT_ROUND_TRIPS, DNS_MS, DRAWS }, calibration: {}, validation: {}, predictions: [] };

// ---- fit the coarse chunk's share of a shared link ----
{
  const overlapped = ['2026-10-16', '2026-10-18'].flatMap((d) => {
    const runs = loadRuns(d);
    if (runs.length === 0) return [];
    const c = calibrate(runs);
    return c.phases.filter((p) => p.overlapped).map((p) => ({ p, local: c.local }));
  });
  let best = { weight: 1, error: Infinity };
  for (const weight of [1, 2, 4, 8, 16, 32, 64, 128]) {
    L3_WEIGHT.value = weight;
    const errs = overlapped.map(({ p, local }) => {
      const span = p.l1End - p.l1Start;
      const bw = p.l1Bytes / Math.max(1, span - p.rtt);
      const r = predict({ rtt: p.rtt, wMeta: p.metaW, wL3: p.l3.ttfb - p.rtt, wL1: p.l1.map((q) => Math.max(0, q.ttfb - p.rtt)), l3Bytes: p.l3.bytes, l1Sizes: p.l1.map((q) => q.bytes), bwBytesPerMs: bw, local, bootRoundTrips: p.bootRoundTrips });
      return Math.abs(r.coarsePage - p.coarsePage) / p.coarsePage;
    });
    const error = median(errs);
    if (error < best.error) best = { weight, error, n: errs.length };
  }
  L3_WEIGHT.value = overlapped.length >= 5 ? best.weight : 1;
  out.l3Weight = { weight: L3_WEIGHT.value, fittedOn: overlapped.length, medianAbsCoarseErrorAtFit: best.error };
}

// ---- calibration and validation on each date that has browser runs ----
for (const d of ['2026-10-16', '2026-10-18']) {
  const runs = loadRuns(d);
  if (runs.length === 0) continue;
  const cal = calibrate(runs);
  out.calibration[d] = { runs: cal.n, staging: cal.staging, local: cal.local, rttMs: cal.rttSpread, bootSlopeRoundTrips: cal.bootSlopeRoundTrips, bootRoundTripsAssumed: cal.bootRoundTripsAssumed };
  // Validation: each run's own RTT and measured bandwidth; W from the run's own chunk TTFBs (structure check), and, when Ashburn probes of the
  // same date exist, from those samples (independent check).
  const errs = { coarse: [], target: [] };
  const errsGp = { coarse: [], target: [] };
  // Ashburn (IAD) probe samples (chunks: 2026-10-16; metadata: 2026-10-18) as W for the 2026-10-18 browser runs, which reached IAD and EWR;
  // the 2026-10-16 runs reached LAX, for which there are no probes of that date.
  const wd = (kind, hit) => waits((gpRows.filter((r) => r.region === 'ashburn' && r.tagSet === 'dc' && r.kind === kind && (r.cfCacheStatus === 'HIT') === hit)));
  const ashHit = d === '2026-10-18' ? wd('chunks', true) : [];
  const ashMiss = d === '2026-10-18' ? wd('chunks', false) : [];
  const metaHit = d === '2026-10-18' ? wd('meta', true) : [];
  const metaMiss = d === '2026-10-18' ? wd('meta', false) : [];
  const rand = rng(777);
  const pick = (arr) => arr[Math.floor(rand() * arr.length)];
  for (const p of cal.phases) {
    const span = p.l1End - p.l1Start;
    const bw = p.l1Bytes / Math.max(1, span - p.rtt); // bytes/ms the browser actually got
    const own = predict({
      rtt: p.rtt,
      wMeta: p.metaW,
      wL3: p.l3.ttfb - p.rtt,
      wL1: p.l1.map((q) => Math.max(0, q.ttfb - p.rtt)),
      l3Bytes: p.l3.bytes,
      l1Sizes: p.l1.map((q) => q.bytes),
      bwBytesPerMs: bw,
      local: cal.local,
      bootRoundTrips: p.bootRoundTrips,
    });
    errs.coarse.push((own.coarsePage - p.coarsePage) / p.coarsePage);
    errs.target.push((own.targetPage - p.targetPage) / p.targetPage);
    if (ashHit.length >= 10 && ashMiss.length >= 10) {
      const hitRun = p.cfL1Hit === 9;
      const W = hitRun ? ashHit : ashMiss;
      const WM = hitRun ? metaHit : metaMiss;
      const gp = predict({ rtt: p.rtt, wMeta: pick(WM.length ? WM : W), wL3: pick(W), wL1: p.l1.map(() => pick(W)), l3Bytes: p.l3.bytes, l1Sizes: p.l1.map((q) => q.bytes), bwBytesPerMs: bw, local: cal.local, bootRoundTrips: p.bootRoundTrips });
      errsGp.coarse.push((gp.coarsePage - p.coarsePage) / p.coarsePage);
      errsGp.target.push((gp.targetPage - p.targetPage) / p.targetPage);
    }
  }
  const stat = (a) => (a.length ? { n: a.length, medianError: median(a), medianAbsError: median(a.map(Math.abs)), p95AbsError: quantile(a.map(Math.abs).sort((x, y) => x - y), 0.95) } : null);
  out.validation[d] = { ownWaits: { coarse: stat(errs.coarse), target: stat(errs.target) }, globalpingAshburnWaits: errsGp.coarse.length ? { coarse: stat(errsGp.coarse), target: stat(errsGp.target) } : null };
}

// ---- predictions for each region, from the calibration of `date` ----
// The access scenarios are an assumption, the same for every region: the round trip from the visitor to the nearest PoP and the
// bandwidth of the visitor's link. (The Globalping "eyeball" probes in these cities reached their PoP in 1 to 5 ms with first-byte-to-last-byte
// times of 0 to 3 ms for 1 MB: they are not a stand-in for a visitor's last mile, so they are not used for RTT or bandwidth.)
// What differs by region is W, the server wait of a HIT or a MISS, and, for Singapore on 2026-10-16, which PoP answered.
const SCENARIOS = [
  { name: 'good', rtt: 25, bwMBps: 25 },
  { name: 'typical', rtt: 50, bwMBps: 10 },
  { name: 'poor', rtt: 150, bwMBps: 2 },
  { name: 'slow', rtt: 250, bwMBps: 0.7 },
];
out.scenarios = SCENARIOS;
const cal = calibrate(loadRuns(date));
if (cal.n > 0) {
  const typical = cal.phases[Math.floor(cal.phases.length / 2)];
  const l1Sizes = typical.l1.map((q) => q.bytes);
  const l3Bytes = median(cal.phases.map((p) => p.l3.bytes));
  const q = (arr, p) => quantile([...arr].sort((x, y) => x - y), p);
  for (const region of REGIONS) {
    for (const state of ['cold', 'warm']) {
      const hit = state === 'warm';
      const chunkW = waitsFor(region, 'chunks', hit);
      const metaW = waitsFor(region, 'meta', hit);
      if (chunkW.length < 5) continue;
      for (const sc of SCENARIOS) {
        const rand = rng(31337 + REGIONS.indexOf(region) * 101 + sc.rtt);
        const pick = (arr) => arr[Math.floor(rand() * arr.length)];
        const res = { coarsePage: [], targetPage: [], coarseFromCall: [], targetFromCall: [] };
        for (let i = 0; i < DRAWS; i++) {
          const r = predict({ rtt: sc.rtt, wMeta: pick(metaW.length >= 5 ? metaW : chunkW), wL3: pick(chunkW), wL1: l1Sizes.map(() => pick(chunkW)), l3Bytes, l1Sizes, bwBytesPerMs: (sc.bwMBps * 1e6) / 1000, local: cal.local });
          for (const k of Object.keys(res)) res[k].push(r[k]);
        }
        out.predictions.push({
          region,
          state,
          scenario: sc.name,
          rttMs: sc.rtt,
          bwMBps: sc.bwMBps,
          waitSamples: { chunks: chunkW.length, meta: metaW.length, waitMedianMs: { chunks: q(chunkW, 0.5), meta: metaW.length ? q(metaW, 0.5) : null } },
          coarsePage: { p50: q(res.coarsePage, 0.5), p95: q(res.coarsePage, 0.95) },
          targetPage: { p50: q(res.targetPage, 0.5), p95: q(res.targetPage, 0.95) },
          coarseFromCall: { p50: q(res.coarseFromCall, 0.5), p95: q(res.coarseFromCall, 0.95) },
          targetFromCall: { p50: q(res.targetFromCall, 0.5), p95: q(res.targetFromCall, 0.95) },
        });
      }
    }
  }
}
out.sizes = { l1Bytes: cal.n ? cal.phases[Math.floor(cal.phases.length / 2)].l1.map((q) => q.bytes) : null };
fs.writeFileSync(path.join(DIR, `model_summary_gp-${gpDates.join('+')}.json`), `${JSON.stringify(out, null, 1)}\n`);

if (md) {
  const f = (x) => (Number.isFinite(x) ? Math.round(x).toString() : 'n/a');
  console.log(`\nCalibration: ${JSON.stringify(out.calibration)}`);
  console.log(`L3 weight: ${JSON.stringify(out.l3Weight)}`);
  console.log(`Validation: ${JSON.stringify(out.validation)}`);
  console.log('\n| Region | Edge | Scenario (RTT ms, MB/s) | first whole frame, from navigation p50 / p95 (ms) | target-level frame, from navigation p50 / p95 | first whole frame, from loadStore call p50 / p95 | target-level frame, from loadStore call p50 / p95 | W median chunks / metadata (ms) |');
  console.log('|---|---|---|---|---|---|---|---|');
  for (const p of out.predictions) console.log(`| ${p.region} | ${p.state} | ${p.scenario} (${p.rttMs}, ${p.bwMBps}) | ${f(p.coarsePage.p50)} / ${f(p.coarsePage.p95)} | ${f(p.targetPage.p50)} / ${f(p.targetPage.p95)} | ${f(p.coarseFromCall.p50)} / ${f(p.coarseFromCall.p95)} | ${f(p.targetFromCall.p50)} / ${f(p.targetFromCall.p95)} | ${f(p.waitSamples.waitMedianMs.chunks)} / ${f(p.waitSamples.waitMedianMs.meta)} |`);
}
console.log(`wrote ${path.join(DIR, `model_summary_gp-${gpDates.join('+')}.json`)}`);
