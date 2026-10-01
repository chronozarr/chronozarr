// Runs the viewer's playBench against a store in a headless Chrome of its own, optionally behind a CDP
// bandwidth throttle, and prints the playback numbers as JSON.
//
//   PLAYWRIGHT_CORE=/path/to/playwright-core CHROME=/path/to/chrome \
//   node js/support/reader-bench/play.mjs <store url> '{"stepsPerSecond":10,"loops":2,"cold":true}' ['{"latency":30,"downloadThroughput":6250000}']
//
// The viewer is served from the repository root by the local static server, so it runs whatever reader is in
// js/chronozarr. The throttle (Network.emulateNetworkConditions fields) is applied just before the bench and
// covers the cold reopen the bench starts with.

import { createRequire } from 'node:module';
import path from 'node:path';
import { startStaticServer } from '../static-server.js';

const REPO_ROOT = path.resolve(import.meta.dirname, '../../..');
const [storeUrl, optionsJson = '{}', throttleJson = 'null'] = process.argv.slice(2);
const playwrightPath = process.env.PLAYWRIGHT_CORE;
const chromePath = process.env.CHROME;
if (!storeUrl || !playwrightPath || !chromePath) {
  console.error('usage: PLAYWRIGHT_CORE=<dir> CHROME=<binary> node play.mjs <store url> [bench options json] [throttle json]');
  process.exit(2);
}
const { chromium } = createRequire(import.meta.url)(playwrightPath);
const options = JSON.parse(optionsJson);
const throttle = JSON.parse(throttleJson);

const server = await startStaticServer(process.env.BENCH_ROOT ?? REPO_ROOT);
const browser = await chromium.launch({ executablePath: chromePath, headless: true, args: ['--ignore-gpu-blocklist', '--use-angle=metal'] });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.on('pageerror', (error) => console.error('pageerror', error.message));
  await page.goto(`${server.url}/js/tileripper/index.html?store=${encodeURIComponent(storeUrl)}`);
  await page.waitForFunction(() => window.tileripper?.ready, null, { timeout: 60000 });
  await page.evaluate(() => window.tileripper.ready);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1, ...throttle });
  const result = await page.evaluate((benchOptions) => window.tileripper.playBench(benchOptions), options);
  const keep = ['store', 'mode', 'lod', 'resolution', 'visibleCells', 'requestedStepsPerSecond', 'achievedStepsPerSecond', 'perLoopStepsPerSecond', 'heldFrames', 'heldAtWrap', 'heldElsewhere', 'longestHoldMs', 'totalHoldMs', 'network'];
  console.log(JSON.stringify(Object.fromEntries(keep.filter((k) => k in result).map((k) => [k, result[k]])), null, 1));
} finally {
  await browser.close();
  await server.close();
}
