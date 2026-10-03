// Repeat the existing 36-cell viewer benchmark against a checkout or a saved JS tree.
// Usage: node scripts/audit_browser.mjs JS_ROOT STORE_PATH OUTPUT_JSON
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from '../js/node_modules/@playwright/test/index.mjs';
import { startStaticServer } from '../js/support/static-server.js';

const [jsRoot, storePath, output] = process.argv.slice(2);
if (!jsRoot || !storePath || !output) {
  throw new Error('Usage: node scripts/audit_browser.mjs JS_ROOT STORE_PATH OUTPUT_JSON');
}
await mkdir(path.dirname(path.resolve(output)), { recursive: true });
const app = await startStaticServer(path.resolve(jsRoot));
const data = await startStaticServer(path.dirname(path.resolve(storePath)));
let browser;
try {
  browser = await chromium.launch({
    headless: true,
    args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on('pageerror', (error) => errors.push(`uncaught: ${error.stack || error.message}`));
  page.on('console', (message) => message.type() === 'error' && errors.push(`console.error: ${message.text()}`));
  const storeUrl = `${data.url}/${path.basename(storePath)}`;
  await page.goto(`${app.url}/demo/index.html?store=${encodeURIComponent(storeUrl)}`);
  await page.waitForFunction(() => window.chronozarr?.ready);
  const ready = await page.evaluate(() => window.chronozarr.ready);
  if (!ready) throw new Error('Viewer failed to open the benchmark store');
  const results = await page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    const { runBenchmarks } = await import('/demo/bench.js');
    const results = await runBenchmarks(viewer, { coldRuns: 3, switches: 20 });
    // A warm gate requires every tested frame in memory. The ordinary idle prefetch
    // intentionally stops after 64 MiB, which cannot warm this 36-cell fixture.
    await viewer.loadStore(viewer.store.url, { lod: 0, maxCacheBytes: 2 * 1024 ** 3 });
    const store = viewer.store;
    const level = store.levels[0];
    for (let t = 0; t < store.times.length; t++) {
      const pending = [];
      for (let row = 0; row < level.gridRows; row++) {
        for (let col = 0; col < level.gridCols; col++) pending.push(store.getRaw(0, row, col, t));
      }
      await Promise.all(pending);
    }
    const frames = [];
    let t = 0;
    let direction = 1;
    viewer.goToTime(t);
    viewer.renderNow();
    for (let i = 0; i < 20; i++) {
      await new Promise((resolve) => setTimeout(resolve, 120));
      if (store.times.length > 1) {
        if (t + direction < 0 || t + direction >= store.times.length) direction = -direction;
        t += direction;
      }
      const before = store.stats.network.bytes;
      const start = performance.now();
      viewer.goToTime(t);
      const frame = viewer.renderNow();
      viewer.renderer.finish();
      frames.push({ t, ms: performance.now() - start, complete: frame.complete, bytes: store.stats.network.bytes - before });
    }
    results.warmFullyLoaded = { maxCacheBytes: 2 * 1024 ** 3, frames };
    return results;
  });
  const grid = await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    const level = viewer.store.levels[0];
    const gl = viewer.canvas.getContext('webgl2');
    const debug = gl.getExtension('WEBGL_debug_renderer_info');
    return { cells: level.gridRows * level.gridCols, renderer: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER) };
  });
  await writeFile(output, JSON.stringify({ jsRoot: path.resolve(jsRoot), storePath: path.resolve(storePath), grid, results, errors }, null, 2) + '\n');
  if (errors.length) throw new Error(errors.join('\n'));
  console.log(JSON.stringify({ output, grid, cold: results.coldOpenConsolidated, warmFullyLoaded: results.warmFullyLoaded, decode: results.decodePerChunk }));
} finally {
  await browser?.close();
  await Promise.all([app.close(), data.close()]);
}
