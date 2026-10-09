// Run after generating data/reports/leafmap/{widget.js,model.json} from a real leafmap Map.
import assert from 'node:assert/strict';
import path from 'node:path';
import { chromium } from '../../js/node_modules/playwright/index.mjs';
import { startStaticServer } from '../../js/support/static-server.js';

const root = path.resolve(import.meta.dirname, '../..');
const server = await startStaticServer(root, 8765);
const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
try {
  const page = await browser.newPage({ viewport: { width: 1000, height: 750 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/leafmap-check.html', route => route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><body></body></html>' }));
  await page.goto('http://127.0.0.1:8765/leafmap-check.html');
  await page.evaluate(async () => {
    document.body.innerHTML = '<div id="widget"></div>';
    const values = await (await fetch('/data/reports/swot-intensity/model.json')).json();
    const options = values.calls.find(([method]) => method === 'addLayer')[1][0].options;
    if (JSON.stringify(options.range) !== '[30,80]' || options.band !== 0) throw Error('Missing fixed display limits');
    const listeners = new Map();
    const model = {
      get: key => values[key], set: (key, value) => { values[key] = value; },
      save_changes() {},
      on: (key, callback) => listeners.set(key, callback),
      off: key => listeners.delete(key),
    };
    const widget = (await import('/data/reports/swot-intensity/widget.js')).default;
    window.dispose = await widget.render({ model, el: document.getElementById('widget') });
  });
  await page.waitForFunction(() => document.querySelector('input[type=range]')?.disabled === false, null, { timeout: 60000 });
  assert.equal(await page.locator('input[type=range]').getAttribute('max'), '1');
  await page.waitForFunction(() => document.querySelector('canvas')?.width > 0);
  await page.locator('input[type=range]').fill('1');
  await page.locator('input[type=range]').dispatchEvent('input');
  await page.waitForFunction(() => document.querySelector('label span').textContent.includes('2025-05-06'));
  await page.waitForTimeout(1500);
  await page.screenshot({ path: path.join(root, 'data/reports/swot-intensity/browser.png') });
  assert.deepEqual(errors, []);
  await page.evaluate(() => window.dispose());
  console.log('SWOT intensity leafmap: float layer opens with fixed range, two-date slider changes time, canvas renders and cleanup succeeds; no page errors.');
} finally {
  await browser.close();
  await server.close();
}
