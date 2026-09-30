// Runs reader-bench/bench.html in a headless Chrome of its own (so it cannot disturb, or be disturbed by, any
// other browser session) and prints the results as JSON.
//
//   PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core \
//   CHROME=/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
//   node js/support/reader-bench/run.mjs '[{"call":"runColdOpen","args":{"impl":"new","url":"/data/spike/stress6x6/"},"throttle":{"latency":30,"downloadThroughput":6250000}}]'
//
// BENCH_PAGE picks another page under js/support (default reader-bench/bench.html; codec-bench/bench.html
// exposes window.runBench).
//
// Each step is {call, args, throttle?}: `call` is a window.run* function of bench.html, `args.url` may be a
// path on the local static server (served from the repository root) or an absolute URL, and `throttle`
// (CDP Network.emulateNetworkConditions fields) applies to that step only.

import { createRequire } from 'node:module';
import path from 'node:path';
import { startStaticServer } from '../static-server.js';

const REPO_ROOT = path.resolve(import.meta.dirname, '../../..');
const playwrightPath = process.env.PLAYWRIGHT_CORE;
const chromePath = process.env.CHROME;
if (!playwrightPath || !chromePath || !process.argv[2]) {
  console.error('usage: PLAYWRIGHT_CORE=<playwright-core dir> CHROME=<chrome binary> node run.mjs \'<json steps>\'');
  process.exit(2);
}
const { chromium } = createRequire(import.meta.url)(playwrightPath);
const steps = JSON.parse(process.argv[2]);

const server = await startStaticServer(REPO_ROOT);
const browser = await chromium.launch({ executablePath: chromePath, headless: true });
try {
  const page = await browser.newPage();
  page.on('pageerror', (error) => console.error('pageerror', error.message));
  await page.goto(`${server.url}/js/support/${process.env.BENCH_PAGE ?? 'reader-bench/bench.html'}`);
  await page.waitForFunction(() => window.benchReady === true);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  const results = [];
  for (const step of steps) {
    const args = { ...step.args };
    if (typeof args.url === 'string' && args.url.startsWith('/')) args.url = `${server.url}${args.url}`;
    await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1, ...step.throttle });
    const result = await page.evaluate(([call, callArgs]) => window[call](callArgs), [step.call, args]);
    results.push({ step, result });
  }
  console.log(JSON.stringify(results, null, 1));
} finally {
  await browser.close();
  await server.close();
}
