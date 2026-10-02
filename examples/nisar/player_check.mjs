import assert from 'node:assert/strict';
import path from 'node:path';
import { chromium } from '../../js/node_modules/playwright/index.mjs';
import { startStaticServer } from '../../js/support/static-server.js';

const root = path.resolve(import.meta.dirname, '../..');
const server = await startStaticServer(root);
const base = server.url;
const live = process.argv.includes('--live');
const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 850 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/notebook-check.html', route => route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><body></body></html>' }));
  await page.goto(`${base}/notebook-check.html`);
  await page.evaluate(async ({ base, live }) => {
    const values = {
      store_url: live ? 'https://data.tileripper.com/ucayali_santa_maria/png-1' : `${base}/data/stores/nisar/local-20261002`,
      viewer_url: live ? 'https://tileripper.com/tileripper/' : `${base}/js/tileripper/index.html`,
      controls: true, height: 560, theme: 'light', t: 0, product: 'band', band: 'HH_dB', range: [-25, 0], speed: 4, playing: false,
      times: [], products: [], bands: [], ready: false, state: {}, click: {}, error: {},
    };
    const listeners = new Map();
    window.values = values;
    window.saves = 0;
    window.model = {
      get: key => values[key],
      set(key, value) {
        if (values[key] === value) return;
        values[key] = value;
        for (const callback of listeners.get(`change:${key}`) ?? []) callback();
      },
      save_changes: () => { window.saves++; },
      on(key, callback) { if (!listeners.has(key)) listeners.set(key, new Set()); listeners.get(key).add(callback); },
      off(key, callback) { listeners.get(key)?.delete(callback); },
    };
    window.listenerCount = () => [...listeners.values()].reduce((sum, group) => sum + group.size, 0);
    const widget = (await import('/src/chronozarr/player.js')).default;
    window.dispose = widget.render({ model: window.model, el: document.body });
  }, { base, live });
  await page.waitForFunction(() => window.values.ready, null, { timeout: 60000 });
  assert.equal(await page.evaluate(() => window.values.times.length), 2);
  const frame = page.frames().find(frame => frame !== page.mainFrame());
  await frame.waitForFunction(() => window.tileripper?.ready, null, { timeout: 60000 });
  await frame.evaluate(() => window.tileripper.ready);
  await page.waitForFunction(() => window.values.state.range?.[0] === -25);
  assert.equal(await page.locator('[aria-label="Units legend"]').innerText(), '-25 → 0 dB');
  await page.locator('select[aria-label=Band]').selectOption('HV_dB');
  await frame.waitForFunction(() => window.tileripper.viewer.bands[window.tileripper.viewer.bandChoice].name === 'HV_dB');
  await page.waitForFunction(() => window.values.state.band === 'HV_dB' && window.values.state.range?.[0] === -25);
  await page.locator('select[aria-label=Band]').selectOption('HH_linear');
  await page.waitForFunction(() => window.values.state.band === 'HH_linear' && window.values.state.range === null);
  await frame.waitForFunction(() => window.tileripper.viewer.renderNow().complete);
  await page.screenshot({path: path.join(root, "data/reports/nisar/player-linear.png")});
  await page.locator('select[aria-label=Band]').selectOption('HV_dB');
  await page.waitForFunction(() => window.values.state.band === 'HV_dB' && window.values.state.range?.[0] === -25);
  await page.evaluate(() => window.model.set('controls', false));
  assert.equal(await page.locator('select[aria-label=Band]').isVisible(), false);
  assert.equal(await page.locator('select[aria-label=Product]').isVisible(), false);
  assert.equal(await page.locator('[aria-label="Display minimum"]').isVisible(), false);
  assert.equal(await page.locator('[aria-label=Timestep]').isVisible(), true);
  await page.evaluate(() => window.model.set('controls', true));
  await page.evaluate(() => window.model.set('t', 1));
  await page.waitForFunction(() => window.values.state.t === 1 && window.values.state.range?.[0] === -25);
  await page.locator('[aria-label="Display minimum"]').fill('-30');
  await page.locator('[aria-label="Display minimum"]').dispatchEvent('change');
  await page.waitForFunction(() => window.values.state.range?.[0] === -30);
  await page.getByText('Auto limits', {exact: true}).click();
  await page.waitForFunction(() => window.values.state.range === null);
  assert.deepEqual(errors, []);
  await page.screenshot({path: path.join(root, 'data/reports/nisar/player.png')});
  await page.evaluate(() => window.dispose());
  console.log('NISAR player: initial fixed limits, band switch, timestep stability, editable limits and auto reset passed.');
} finally { await browser.close(); await server.close(); }
