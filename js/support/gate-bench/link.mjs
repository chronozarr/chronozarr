// The viewer of two builds on throttled links: what scrubbing, a far jump and playback cost a visitor whose link, not the
// machine, is the limit. The counterpart of run.mjs (the speed gates, on an unthrottled link with every chunk in memory),
// for the changes that the gates cannot see: what the viewer fetches, in which order, and what it shows while it waits.
//
//   node support/gate-bench/link.mjs --base <viewer root> --head <viewer root> --store <long store dir>
//        [--pairs 3] [--links 50mbit-40ms,8mbit-80ms] [--scenarios drag40,drag150,jump,play4,play30] [--play-seconds 30]
//        [--gpu metal|swiftshader] [--ignore-guards] [--out results.json] [--summary summary.md]
//
// A viewer root holds demo/ as support/build-viewer.mjs writes it; the store is one that `store.mjs <dir> --times 48` writes,
// with enough timesteps for a drag, a jump and a movie to need more than the viewer holds. Both builds and the store are served
// over HTTP/2 (TLS, a certificate made with openssl for the run), as the site and its data host are, so the requests of the
// viewer share a connection as they do on Cloudflare. Each run is a new headless Chromium with an empty HTTP cache:
//
//   1. CDP throttles the link to the profile of the run (link-compare.js: LINKS) before the page is loaded, so the viewer, its
//      workers and the first frame at the target level all arrive over the link, as they do for a visitor on it. The local
//      server does not compress, so the viewer's script and codecs cost more bytes than from a CDN.
//   2. The viewer idles for 5 s, as a visitor who has looked at the first frame does, with whatever it fetches then. What the
//      reader holds and has in flight when the scenario starts is in the record (`detail.start`): runs that start from
//      different states are not comparable.
//   3. The scenario (link-compare.js: SCENARIOS; link-scenarios.js drives the page): a drag of the timeline over the whole
//      time axis at 40 or 150 ms per step, a press on the timeline half the axis away, or Play at 4 or 30 steps/s for 30 s.
//
// Its figures: for a drag, the frames it drew at the target level and at a coarser one (a repaint of the frame on screen is not one), the time the frame on screen was older than the timestep asked for, the timesteps never shown,
// the delay from each input to a frame that shows it and the time from the release to the frame at the target level; for a
// jump, the time to the first whole frame of the timestep at any level and at the target level; for playback, the steps it
// took, the rate it kept, the wait before the first step and the pauses to buffer; for every scenario the requests and the
// bytes on the wire (CDP). The builds alternate (base then head, then head then base) in pairs of runs of one scenario on one
// link, and a pair is compared on its own, as run.mjs does. Throttled runs are limited by the link, so they are less sensitive
// to a busy machine than the gates, but not insensitive: look at the load in the log, and run --base and --head on the same
// build once to see the noise of a metric before reading a difference. One pair of the first scenario on the first link
// is run first and not counted, as run.mjs does, so that the first browsers do not pay for cold file and GPU caches.
//
// The raw page results of every run are written to --out as each run ends, and the comparison is added at the end: a run
// that fails after 20 minutes keeps what was measured before it.
//
// A run refuses to start on a machine whose load or power source would make the timings noise, and discards a run whose
// browser was not drawing at 60 Hz (guards.js); `--ignore-guards` measures anyway.
//
// Not a CI job: five scenarios on two links, three pairs of two builds, is about 30 minutes (CONTRIBUTING.md).

import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { appendFile, mkdir, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { startStaticServer } from '../static-server.js';
import { BROWSER_ARGS, LOOPBACK_ONLY, VIEWPORT, launchBrowser, measureFrameInterval, rendererInfo, withTimeout } from './browser.mjs';
import { attachNetworkLog, setLink } from './cdp.js';
import { LOAD_LIMIT, RAF_RANGE_MS, guardsEnabled, measureGuarded, readMachine, refuseNoisyMachine } from './guards.js';
import { LINKS, SCENARIOS, compareLinks, linkSummaryMarkdown, runFigures } from './link-compare.js';
import { runScenario } from './link-scenarios.js';

const IDLE_MS = 5000;
// What a drag or a jump waits for its last frame, after the release: a run that cannot show it reports this, flagged `timedOut`.
const SETTLE_CAP_MS = 60_000;
const QUIET_MS = 500;
const RUN_TIMEOUT_MS = 5 * 60_000;
const MIN_STEPS = 24;

const { values: args } = parseArgs({
  options: {
    base: { type: 'string' },
    head: { type: 'string' },
    store: { type: 'string' },
    pairs: { type: 'string', default: '3' },
    links: { type: 'string', default: Object.keys(LINKS).join(',') },
    scenarios: { type: 'string', default: Object.keys(SCENARIOS).join(',') },
    'play-seconds': { type: 'string', default: '30' },
    gpu: { type: 'string', default: 'metal' },
    'ignore-guards': { type: 'boolean', default: false },
    out: { type: 'string' },
    summary: { type: 'string' },
  },
});
const links = args.links.split(',');
const scenarios = args.scenarios.split(',');
const pairs = Number(args.pairs);
const playMs = Number(args['play-seconds']) * 1000;
if (!args.base || !args.head || !args.store || !BROWSER_ARGS[args.gpu] || !(pairs >= 1) || !(playMs > 0) || links.some((link) => !LINKS[link]) || scenarios.some((scenario) => !SCENARIOS[scenario])) {
  console.error(`Usage: node support/gate-bench/link.mjs --base <viewer root> --head <viewer root> --store <long store dir> [--pairs 3] [--links ${Object.keys(LINKS)}] [--scenarios ${Object.keys(SCENARIOS)}] [--play-seconds 30] [--gpu metal|swiftshader] [--ignore-guards] [--out results.json] [--summary summary.md]`);
  process.exit(2);
}
const guards = guardsEnabled({ ignore: args['ignore-guards'] });
if (guards) refuseNoisyMachine(readMachine());

const load = () => os.loadavg()[0].toFixed(1);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** A certificate for 127.0.0.1 that lives as long as this process, as scripts/fieldperf/local_site.mjs makes one. */
function selfSignedCertificate() {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'link-bench-cert-'));
  try {
    execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1', '-keyout', path.join(dir, 'key.pem'), '-out', path.join(dir, 'cert.pem')], { stdio: 'ignore' });
    return { key: readFileSync(path.join(dir, 'key.pem')), cert: readFileSync(path.join(dir, 'cert.pem')) };
  } catch (error) {
    throw new Error(`link.mjs needs openssl to make the certificate of its HTTPS server (${error.message})`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

async function measure({ variant, pair, link, scenario, appUrl, storeUrl, dataOrigin }) {
  const browser = await launchBrowser(args.gpu, [LOOPBACK_ONLY]);
  const errors = [];
  try {
    const frameBefore = guards ? await measureFrameInterval(browser) : null;
    const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 1, serviceWorkers: 'block', ignoreHTTPSErrors: true });
    const page = await context.newPage();
    page.on('console', (message) => message.type() === 'error' && errors.push(`console.error: ${message.text()}`));
    page.on('pageerror', (error) => errors.push(`uncaught: ${error.stack || error.message}`));
    const { cdp, log } = await attachNetworkLog(context, page, [dataOrigin]);
    await setLink(cdp, LINKS[link]);
    const loadAtStart = load();
    await page.goto(`${appUrl}/demo/index.html?store=${encodeURIComponent(storeUrl)}`);
    await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: 120_000 });
    const opened = await page.evaluate(() => window.chronozarr.ready);
    if (!opened) throw new Error('the viewer did not open the store');
    const steps = await page.evaluate(() => window.chronozarr.viewer.store.times.length);
    if (steps < MIN_STEPS) throw new Error(`the store has ${steps} timesteps: the link scenarios need at least ${MIN_STEPS} (node support/gate-bench/store.mjs <dir> --times 48)`);
    await sleep(IDLE_MS);
    const before = log.snapshot();
    const raw = await withTimeout(page.evaluate(runScenario, { ...SCENARIOS[scenario], capMs: SETTLE_CAP_MS, windowMs: playMs }), `the scenario ${scenario}`, RUN_TIMEOUT_MS);
    await sleep(QUIET_MS);
    const after = log.snapshot();
    const frameAfter = guards ? await measureFrameInterval(browser) : null;
    const { figures, timedOut, detail } = runFigures(scenario, raw, { requests: after.requests - before.requests, bytes: after.bytes - before.bytes, failed: after.failed - before.failed });
    return {
      variant,
      pair,
      link,
      scenario,
      load: { start: Number(loadAtStart), end: Number(load()) },
      browser: await page.evaluate(rendererInfo),
      figures,
      timedOut,
      // What the page returned, for reading the figures again with other definitions.
      raw,
      detail: { ...detail, steps, open: { openMs: opened.openMs, coarseMs: opened.coarseMs, firstPaintMs: opened.firstPaintMs } },
      errors,
      ...(guards && { frameInterval: { before: frameBefore, after: frameAfter } }),
    };
  } finally {
    await browser.close();
  }
}

const describe = (record) => `${Object.entries(record.figures).map(([key, value]) => `${key} ${value}`).join(', ')}${record.timedOut.length ? `; hit the wait: ${record.timedOut}` : ''}; ${record.errors.length} page errors (load ${record.load.start} to ${record.load.end})`;

const tls = selfSignedCertificate();
const data = await startStaticServer(path.resolve(args.store), 0, { tls });
const apps = { base: await startStaticServer(path.resolve(args.base), 0, { tls }), head: await startStaticServer(path.resolve(args.head), 0, { tls }) };
const storeUrl = `${data.url}/`;
const records = [];
const discarded = [];
const startedAt = new Date().toISOString();
const loadBefore = load();
const groups = links.flatMap((link) => scenarios.map((scenario) => ({ link, scenario })));

function environmentLines() {
  const { browser } = records[0];
  return [
    `${browser.userAgent.match(/(?:Headless)?Chrome\/[\d.]+/)?.[0] ?? browser.userAgent}, WebGL renderer: ${browser.renderer}`,
    `${os.cpus().length} × ${os.cpus()[0]?.model ?? 'unknown CPU'}, ${(os.totalmem() / 1024 ** 3).toFixed(0)} GB; 1-minute load ${loadBefore} at the start, ${load()} at the end`,
    `Viewport ${VIEWPORT.width} × ${VIEWPORT.height} at pixel ratio 1; a store of ${records[0].detail.steps} timesteps over HTTP/2; per run the link is throttled before the page loads, the viewer idles ${IDLE_MS / 1000} s after its first frame, then the scenario (${playMs / 1000} s of playback, at most ${SETTLE_CAP_MS / 1000} s for the last frame of a drag or a jump)`,
    ...(guards ? [`Guards: a load of ${LOAD_LIMIT} or less and no battery power at the start; the median animation frame interval before and after each run in ${RAF_RANGE_MS[0]} to ${RAF_RANGE_MS[1]} ms (${discarded.length} runs discarded)`] : []),
  ];
}

/** The raw records so far, and the comparison once there is one. */
async function writeResults(result = null) {
  if (!args.out) return;
  await mkdir(path.dirname(path.resolve(args.out)), { recursive: true });
  await writeFile(args.out, `${JSON.stringify({ startedAt, pairs, links, scenarios, environment: environmentLines(), result, records, discarded }, null, 1)}\n`);
}

// Not counted: the first browsers of a job pay for cold file and GPU caches.
for (const variant of ['base', 'head']) {
  const record = await measureGuarded(() => measure({ variant, pair: -1, ...groups[0], appUrl: apps[variant].url, storeUrl, dataOrigin: data.url }), { enabled: guards, label: `warm-up ${groups[0].link} ${groups[0].scenario} ${variant} run`, discarded });
  console.log(`warm-up ${groups[0].link} ${groups[0].scenario} ${variant}: ${describe(record)}`);
}
for (let pair = 0; pair < pairs; pair++) {
  for (const [index, { link, scenario }] of groups.entries()) {
    const order = (pair + index) % 2 === 0 ? ['base', 'head'] : ['head', 'base'];
    for (const variant of order) {
      const label = `pair ${pair + 1} ${link} ${scenario} ${variant} run`;
      const record = await measureGuarded(() => measure({ variant, pair, link, scenario, appUrl: apps[variant].url, storeUrl, dataOrigin: data.url }), { enabled: guards, label, discarded });
      console.log(`pair ${pair + 1}/${pairs} ${link} ${scenario} ${variant}: ${describe(record)}`);
      records.push(record);
      await writeResults();
    }
  }
}

const result = compareLinks(records);
const environment = environmentLines();
const summary = linkSummaryMarkdown(result, { environment, records });
console.log(`\n${summary}`);
await writeResults(result);
if (args.summary) await appendFile(args.summary, summary);
// An HTTP/2 server's close() waits for the browsers' sessions to end; exiting is what stops the three.
process.exit(0);
