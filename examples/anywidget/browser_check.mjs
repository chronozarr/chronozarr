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
      store_url: live ? 'https://data.tileripper.com/ucayali_santa_maria/png-1' : `${base}/data/stores/ucayali_santa_maria/png-1`,
      viewer_url: live ? 'https://tileripper.com/tileripper/' : `${base}/js/tileripper/index.html`,
      controls: true, height: 560, theme: 'light', t: 0, product: '', speed: 4, playing: false,
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
  assert.equal(await page.evaluate(() => window.values.times.length), 36);
  const frame = page.frames().find(frame => frame !== page.mainFrame());
  await frame.waitForFunction(() => window.tileripper?.ready, null, { timeout: 60000 });
  await frame.evaluate(() => window.tileripper.ready);
  // Commands from a kernel-side trait change reach the viewer.
  await page.evaluate(() => window.model.set('t', 9));
  await frame.waitForFunction(() => window.tileripper.viewer.t === 9);
  await page.waitForFunction(() => window.values.state.t === 9);
  await page.locator('select[aria-label=Product]').selectOption('band');
  await page.waitForFunction(() => window.values.state.product === 'band');
  await page.locator('select[aria-label=Product]').selectOption('true_color');
  // Invalid commands report an error and restore the accepted state.
  await page.evaluate(() => window.model.set('t', 9999));
  await page.waitForFunction(() => window.values.error.code === 'bad_set' && window.values.t === 9);
  // Matching origin alone is insufficient; a sibling/source-less event is rejected.
  await page.evaluate(() => window.dispatchEvent(new MessageEvent('message', {
    origin: new URL(window.values.viewer_url).origin,
    data: { v: 1, type: 'tileripper:time', t: 999, time: 'forged' }, source: null,
  })));
  assert.equal(await page.evaluate(() => window.values.t), 9);
  await page.evaluate(() => { window.model.set('speed', 2); window.model.set('playing', true); });
  await page.waitForFunction(() => window.values.state.playing === true);
  await frame.waitForFunction(() => window.tileripper.viewer.t !== 9, null, { timeout: 30000 });
  await page.evaluate(() => window.model.set('playing', false));
  await page.waitForFunction(() => window.values.state.playing === false);
  await page.locator('input[aria-label=Timestep]').fill('9');
  await page.locator('input[aria-label=Timestep]').dispatchEvent('input');
  await frame.waitForFunction(() => window.tileripper.viewer.paintedT === 9, null, { timeout: 30000 });
  const canvas = frame.locator('canvas').first();
  const bounds = await canvas.boundingBox();
  await page.mouse.click(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await page.waitForFunction(() => window.values.click.type === 'tileripper:click');
  assert.equal(await page.evaluate(() => Object.keys(window.values.click.values).length), 3);
  await page.screenshot({ path: path.join(root, `data/reports/anywidget-${live ? 'live' : 'local'}.png`) });
  await page.evaluate(() => window.dispose());
  assert.equal(await page.locator('iframe').count(), 0);
  assert.equal(await page.evaluate(() => window.listenerCount()), 0);
  assert.deepEqual(errors, []);
  console.log(`${live ? 'Live' : 'Local'} player: metadata, kernel/UI commands, errors, sender checks, playback, pixel clicks and cleanup passed.`);
} finally {
  await browser.close(); await server.close();
}
