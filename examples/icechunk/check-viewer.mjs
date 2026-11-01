// Optional headless viewer smoke check after qualify.py; needs `npm ci --prefix js`.
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { startStaticServer } from '../../js/support/static-server.js';

const { chromium } = createRequire(new URL('../../js/package.json', import.meta.url))('@playwright/test');
const directory = path.resolve(process.argv[2]);
const receiptFile = path.join(directory, 'receipt.json');
const receipt = JSON.parse(await readFile(receiptFile, 'utf8'));
const app = await startStaticServer(path.resolve(import.meta.dirname, '../../js'));
const data = await startStaticServer(directory);
const browser = await chromium.launch({ headless: true, args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
const checks = [];
try {
  for (const source of receipt.sources) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${app.url}/demo/index.html?store=${encodeURIComponent(`${data.url}/${source.output}/`)}&p=band&r=15,25`);
    await page.waitForFunction(() => window.chronozarr?.ready, { timeout: 30_000 });
    assert.ok(await page.evaluate(() => window.chronozarr.ready), 'first frame must complete');
    const state = await page.evaluate(() => {
      const viewer = window.chronozarr.viewer;
      return { paintedT: viewer.paintedT, glError: viewer.canvas.getContext('webgl2').getError(), times: viewer.store.times };
    });
    assert.equal(state.glError, 0);
    await page.evaluate(() => window.chronozarr.viewer.goToTime(1));
    await page.waitForFunction(() => window.chronozarr.viewer.paintedT === 1);
    await page.evaluate(() => window.chronozarr.viewer.goToTime(0));
    await page.waitForFunction(() => window.chronozarr.viewer.paintedT === 0);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: path.join(directory, `viewer-${source.name}.png`) });
    checks.push({ source: source.name, completeFirstFrame: true, glError: 0, paintedSteps: [state.paintedT, 1, 0], pageErrors: errors });
    await page.close();
  }
} finally {
  await browser.close();
  await app.close();
  await data.close();
}
receipt.browser = { renderer: 'existing viewer, headless Chromium SwiftShader; functional smoke, no timing claim', checks };
await writeFile(receiptFile, JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify(receipt.browser));
