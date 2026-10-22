// Recheck the existing reader gates with the final mask fix, without paced playback.
// node bench/sar/gates.mjs > bench/sar/results/mask-fix-gates.json
import fs from 'node:fs';
import crypto from 'node:crypto';
import { startServer, launchBrowser, newPage } from '../lib/harness.mjs';

const sources = ['js/chronozarr/decoder.js', 'js/chronozarr/aux-cache.js', 'js/demo/viewer.js', 'js/demo/bench.js'];
const sha256 = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const stores = {
  sentinel1: 'ucayali_santa_maria/s1-rtc-monthly-v1',
  nisar: 'colorado_yuma/nisar-gcov-v2',
};
const within = (promise, ms) => Promise.race([
  promise,
  new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error(`reader gates exceeded ${ms} ms`)), ms);
    timer.unref();
  }),
]);
const server = await startServer();
const output = { at: new Date().toISOString(), sources: Object.fromEntries(sources.map(file => [file, sha256(file)])), sensors: {} };
try {
  for (const [sensor, store] of Object.entries(stores)) {
    const browser = await launchBrowser({ channel: 'chromium' });
    try {
      const { page } = await newPage(browser, { width: 1440, height: 900, deviceScaleFactor: 2, blockCatalog: true });
      const url = `${server.url}/data/stores/${store}`;
      await page.goto(`${server.url}/js/demo/index.html?store=${encodeURIComponent(url)}`);
      await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: 30000 });
      output.sensors[sensor] = await within(page.evaluate(async () => {
        await chronozarr.ready;
        const { runBenchmarks } = await import('/js/demo/bench.js');
        // This helper deliberately stresses LOD 0. Incomplete warm switches are
        // retained as failures, not folded into successful switch statistics.
        const originalLog = console.log;
        console.log = () => {};
        try {
          return await runBenchmarks(chronozarr.viewer, { coldRuns: 1, switches: 4 });
        } finally {
          console.log = originalLog;
        }
      }), 60000);
    } finally {
      await browser.close();
    }
  }
} finally {
  await server.close();
}
console.log(JSON.stringify(output, null, 2));
