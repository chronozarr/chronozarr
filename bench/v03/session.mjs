// One benchmark session: a fresh headless Chromium (full Chromium build, Metal GPU) opens one system at one view over one
// link profile and runs the same script for all three systems:
//
//   open     the call that opens the data, to the first complete frame
//   step     STEPS single-date steps forward (one input, wait for the complete frame, think for THINK_MS, again)
//   revisit  the same dates back again (dates the system has shown already)
//   jump     one input JUMP_DISTANCE dates away from where the revisit ended
//   idle     TRAILING_IDLE_MS with nothing asked, to see what a system keeps fetching
//
// Requests and bytes come from CDP events of the page (lib/harness.mjs NetCounter), for the origin the browser reads the
// data from: the data server for A and B, TiTiler for C. The HTTP cache is disabled. The animation-frame rate of the
// idle page is measured before and after; the caller discards a session whose rate is not nominal.

import os from 'node:os';
import { applyProfile, attachNet, launchBrowser, newPage, round, subtract } from '../lib/harness.mjs';
import { JUMP_DISTANCE, OPEN_TIMEOUT_MS, PROFILES, RAF_NOMINAL_MS, START_T, STEPS, STEP_TIMEOUT_MS, SYSTEMS, THINK_MS, TITILER_TILE_QUERY, TRAILING_IDLE_MS, VIEWPORT } from './config.mjs';
import { median, rafIsNominal } from './stats.mjs';
import { footprintLonLat, viewerSearch } from './view.mjs';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** The viewer's chrome (header, timeline, inspector) hidden so that its canvas is the whole window, like the map of the other two. */
const VIEWER_FULL_WINDOW_CSS = 'nav, .timeline-bar, .sidebar, .click-hint, .perf-overlay { display: none !important; }';

const withTimeout = (promise, ms, label) =>
  Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(new Error(`${label} did not return within ${ms} ms`)), ms))]);

const rafGaps = (page, ms) =>
  page.evaluate(
    (windowMs) =>
      new Promise((resolve) => {
        const gaps = [];
        let last = null;
        const end = performance.now() + windowMs;
        const tick = (now) => {
          if (last !== null) gaps.push(now - last);
          last = now;
          if (performance.now() < end) requestAnimationFrame(tick);
          else resolve(gaps);
        };
        requestAnimationFrame(tick);
      }),
    ms,
  );

/** Sum of the bytes a list of requests asked of the data server by Range header (what TiTiler read of the COGs); null ranges (whole file, HEAD) are counted as requests only. */
function originDelta(requests, from, to) {
  const slice = requests.slice(from, to);
  let rangeBytes = 0;
  for (const request of slice) {
    const match = /^bytes=(\d+)-(\d+)$/.exec(request.range ?? '');
    if (match) rangeBytes += Number(match[2]) - Number(match[1]) + 1;
  }
  return { requests: slice.length, rangeBytes };
}

/** The numbers of a phase kept in the results: wall time and what crossed the browser's link. */
const netOf = (delta) => ({ requests: delta.requests, bytes: delta.bytes, aborted: Object.values(delta.byKind).reduce((n, k) => n + k.aborted, 0), byKind: delta.byKind });

export function loadSnapshot() {
  return { loadavg1: round(os.loadavg()[0], 2), loadavg5: round(os.loadavg()[1], 2), ncpu: os.cpus().length, freeMemGB: round(os.freemem() / 2 ** 30, 1) };
}

/**
 * Run one session. `servers` = { app, data, titiler, store, cogUrl } with the URLs the page and the container use.
 * Returns the raw record; never throws for a failure inside the page (the record has `error`).
 */
export async function runSession({ system, profileName, view, servers, startT = START_T, steps = STEPS, jumpDistance = JUMP_DISTANCE }) {
  const spec = SYSTEMS[system];
  const record = { system, systemName: spec.name, profile: profileName, view: view.name, load: loadSnapshot(), error: null };
  const browser = await launchBrowser({ channel: 'chromium' });
  try {
    record.browser = { version: browser.version() };
    const { context, page } = await newPage(browser, { width: VIEWPORT.width, height: VIEWPORT.height, deviceScaleFactor: VIEWPORT.deviceScaleFactor, blockCatalog: system === 'A' });
    const messages = [];
    const debug = process.env.BENCH_DEBUG ? (message) => console.error(`  [${system} ${profileName} ${view.name}] ${message}`) : () => {};
    page.on('console', (m) => {
      debug(`console.${m.type()}: ${m.text().slice(0, 200)}`);
      if (['warning', 'error'].includes(m.type())) messages.push(`${m.type()}: ${m.text().slice(0, 300)}`);
    });
    page.on('pageerror', (e) => messages.push(`pageerror: ${String(e.message).slice(0, 300)}`));
    const dataOrigin = `${servers.data.url}/`;
    const titilerOrigin = `${servers.titiler.url}/`;
    const { cdp, counter } = await attachNet(context, page, system === 'C' ? [titilerOrigin] : [dataOrigin]);
    await applyProfile(cdp, null);
    await page.goto(`${servers.app.url}${spec.page}`);
    await page.waitForFunction(() => Boolean(window.chronozarr || window.zl || window.maplibregl), null, { timeout: 60000 });
    if (system === 'A') await page.addStyleTag({ content: VIEWER_FULL_WINDOW_CSS });
    await page.addScriptTag({ path: new URL('common.js', import.meta.url).pathname });
    await page.addScriptTag({ path: new URL(spec.driver, import.meta.url).pathname });

    const pre = await rafGaps(page, 1500);
    record.raf = { preMedianMs: round(median(pre), 2), pre: pre.length };
    record.page = await page.evaluate(() => ({ dpr: window.devicePixelRatio, innerWidth: window.innerWidth, innerHeight: window.innerHeight }));

    await applyProfile(cdp, PROFILES[profileName]);
    const mark = () => ({ net: counter.snapshot(), origin: servers.data.requests.length });
    const done = (from) => {
      const now = mark();
      return { net: netOf(subtract(now.net, from.net)), origin: originDelta(servers.data.requests, from.origin, now.origin) };
    };

    const view1 = system === 'A' ? { search: viewerSearch(view, startT), pin: view.pin.A ?? null } : { view };
    const cfg = { cogUrls: Array.from({ length: servers.timesteps }, (_, t) => servers.cogUrl(t)), titilerUrl: servers.titiler.url, tileQuery: TITILER_TILE_QUERY, bounds: footprintLonLat(), latitude: view.centerLonLat[1], maxzoom: view.pin.C?.maxzoom ?? 22 };
    const openArgs = { storeUrl: servers.store, t: startT, timeoutMs: OPEN_TIMEOUT_MS, cfg, ...view1 };

    const sessionStart = mark();
    let from = sessionStart;
    const opened = await withTimeout(page.evaluate((args) => window.__driver.open(args), openArgs), OPEN_TIMEOUT_MS + 30000, 'open');
    record.open = { ...opened, ...done(from) };
    debug(`open ${JSON.stringify(record.open).slice(0, 300)}`);

    const walk = async (name, targets, key) => {
      const rows = [];
      const start = mark();
      for (const t of targets) {
        await sleep(THINK_MS);
        const before = mark();
        const result = await withTimeout(page.evaluate(([target, options]) => window.__driver.show(target, options), [t, { key: system === 'A' ? key : null, timeoutMs: STEP_TIMEOUT_MS }]), STEP_TIMEOUT_MS + 30000, `${name} t=${t}`);
        rows.push({ t, ...result, ...done(before) });
        debug(`${name} t=${t}: ${result.ms === null ? 'timeout' : `${Math.round(result.ms)} ms`}`);
      }
      record[name] = { steps: rows, ...done(start) };
    };

    await walk('step', Array.from({ length: steps }, (_, i) => startT + 1 + i), 'ArrowRight');
    await walk('revisit', Array.from({ length: steps }, (_, i) => startT + steps - 1 - i), 'ArrowLeft');
    await walk('jump', [startT + jumpDistance], null);

    from = mark();
    await sleep(TRAILING_IDLE_MS);
    record.idle = { ms: TRAILING_IDLE_MS, ...done(from) };

    record.reader = await page.evaluate(() => window.__driver.stats());
    await applyProfile(cdp, null);
    const post = await rafGaps(page, 1500);
    record.raf.postMedianMs = round(median(post), 2);
    record.rafNominal = rafIsNominal(pre, RAF_NOMINAL_MS) && rafIsNominal(post, RAF_NOMINAL_MS);
    record.session = done(sessionStart);
    record.messages = messages.slice(0, 12);
    await context.close();
  } catch (error) {
    record.error = String(error.stack ?? error).split('\n').slice(0, 6).join(' | ');
  } finally {
    await browser.close();
  }
  return record;
}
