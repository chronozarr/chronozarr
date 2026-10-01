// Opens the viewer on a store in a headless Chrome of its own, touches nothing, and samples what the reader
// has downloaded and holds in its caches at fixed times after the page load. This is the "leave it open" case:
// it shows whether idle prefetch stops.
//
//   PLAYWRIGHT_CORE=/path/to/playwright-core CHROME=/path/to/chrome \
//   node js/support/reader-bench/idle.mjs <store url> ['{"dpr":2,"samplesS":[10,60,180]}']
//
// `dpr` is the device pixel ratio of the page (2 picks the level one finer than 1 does, as a retina laptop would:
// on the Ucayali store 9 cells at lod 1 instead of 4 at lod 2). Reported per sample: bytes the reader counted
// (`network.bytes`, response bodies), bytes the browser received from the store's origin (CDP encodedDataLength,
// headers included), requests, and the decoded / compressed / estimated bytes of the reader's cache.

import { createRequire } from 'node:module';
import path from 'node:path';
import { startStaticServer } from '../static-server.js';

const REPO_ROOT = path.resolve(import.meta.dirname, '../../..');
const [storeUrl, optionsJson = '{}'] = process.argv.slice(2);
const playwrightPath = process.env.PLAYWRIGHT_CORE;
const chromePath = process.env.CHROME;
if (!storeUrl || !playwrightPath || !chromePath) {
  console.error('usage: PLAYWRIGHT_CORE=<dir> CHROME=<binary> node idle.mjs <store url> [options json]');
  process.exit(2);
}
const { chromium } = createRequire(import.meta.url)(playwrightPath);
const { dpr = 2, samplesS = [10, 60, 180] } = JSON.parse(optionsJson);
const MB = 1e6;
const round = (x, digits = 1) => Math.round(x * 10 ** digits) / 10 ** digits;

const server = await startStaticServer(process.env.BENCH_ROOT ?? REPO_ROOT);
const browser = await chromium.launch({ executablePath: chromePath, headless: true, args: ['--ignore-gpu-blocklist', '--use-angle=metal'] });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: dpr });
  page.on('pageerror', (error) => console.error('pageerror', error.message));
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  const origin = new URL(storeUrl).origin;
  const urls = new Map();
  let wireBytes = 0;
  cdp.on('Network.responseReceived', ({ requestId, response }) => urls.set(requestId, response.url));
  cdp.on('Network.loadingFinished', ({ requestId, encodedDataLength }) => {
    if (urls.get(requestId)?.startsWith(origin)) wireBytes += encodedDataLength;
  });

  const started = Date.now();
  await page.goto(`${server.url}/js/tileripper/index.html?store=${encodeURIComponent(storeUrl)}`);
  await page.waitForFunction(() => window.tileripper?.ready, null, { timeout: 60000 });
  await page.evaluate(() => window.tileripper.ready);
  const readyS = round((Date.now() - started) / 1000, 2);
  const view = await page.evaluate(() => {
    const { viewer } = window.tileripper;
    return { lod: viewer.movieInfo.baseLod, canvas: [viewer.canvas.width, viewer.canvas.height], timesteps: viewer.store.times.length };
  });

  const samples = [];
  for (const at of samplesS) {
    const wait = at * 1000 - (Date.now() - started);
    if (wait > 0) await page.waitForTimeout(wait);
    const sample = await page.evaluate(() => {
      const { store } = window.tileripper.viewer;
      const stats = store.stats();
      return {
        requests: stats.network.requests,
        networkBytes: stats.network.bytes,
        inflight: stats.network.inflight,
        decodedBytes: stats.cache.decodedBytes,
        compressedBytes: stats.cache.compressedBytes,
        estimatedBytes: typeof store.estimatedBytes === 'function' ? store.estimatedBytes() : null,
        speculativeBytes: stats.cache.speculativeBytes,
        cache: store.cacheInfo(),
      };
    });
    samples.push({
      atS: round((Date.now() - started) / 1000),
      requests: sample.requests,
      downloadedMB: round(sample.networkBytes / MB),
      wireMB: round(wireBytes / MB),
      speculativeMB: round(sample.speculativeBytes / MB),
      decodedMB: round(sample.decodedBytes / MB),
      compressedMB: round(sample.compressedBytes / MB),
      cacheMB: round((sample.decodedBytes + sample.compressedBytes) / MB),
      estimatedMB: sample.estimatedBytes === null ? null : round(sample.estimatedBytes / MB),
      decodedChunks: sample.cache.entries,
      compressedChunks: sample.cache.compressedEntries,
      inflight: sample.inflight,
    });
  }
  console.log(JSON.stringify({ store: storeUrl, dpr, readyS, ...view, samples }, null, 1));
} finally {
  await browser.close();
  await server.close();
}
