import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs/promises';
import { chromium } from '../../js/node_modules/playwright/index.mjs';
import { startStaticServer } from '../../js/support/static-server.js';

const root = path.resolve(import.meta.dirname, '../..');
const quality = process.argv.includes('--usable') ? 'usable' : process.argv.includes('--good') ? 'good' : 'local';
const suffix = quality === 'local' ? '' : `-${quality}`;
const report = JSON.parse(await fs.readFile(path.join(root, `data/reports/swot-roanoke-20261002${suffix}.json`)));
const server = await startStaticServer(root);
const store = `${server.url}/data/stores/swot_roanoke/${quality}-20261002`;
const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 850 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(`${server.url}/js/tileripper/index.html?store=${encodeURIComponent(store)}`);
  await page.waitForFunction(() => window.tileripper?.ready, null, { timeout: 30000 });
  await page.evaluate(() => window.tileripper.ready);
  const results = await page.evaluate(async () => {
    const s = window.tileripper.viewer.store;
    const hash = async data => [...new Uint8Array(await crypto.subtle.digest('SHA-256', data))].map(v => v.toString(16).padStart(2, '0')).join('');
    const results = [];
    for (let t = 0; t < s.times.length; t++) {
      const cell = await s.getCell(0, 0, 0, t), mask = await s.getMask(0, 0, 0, t);
      const index = cell.data.findIndex((value, i) => mask[i] && value < 0 && i > 512 * 30 && i < 512 * 480 && i % 512 > 30 && i % 512 < 480);
      results.push({ data_sha256: await hash(cell.data), mask_sha256: await hash(mask),
        index, value: cell.data[index], dtype: cell.data.constructor.name, units: window.tileripper.viewer.bands[0].units });
    }
    return results;
  });
  for (let t = 0; t < 2; t++) {
    assert.equal(results[t].data_sha256, report.sources[t].data_sha256);
    assert.equal(results[t].mask_sha256, report.sources[t].mask_sha256);
    assert.equal(results[t].dtype, 'Float32Array'); assert.equal(results[t].units, 'm');
    assert(results[t].index >= 0);
    await page.evaluate(({ t, index }) => {
      const v = window.tileripper.viewer;
      v.hooks = { click: event => { window.clicked = event; } };
      v.goToTime(t); v.setView({ zoom: 8, center: { col: index % 512 + 0.5, row: Math.floor(index / 512) + 0.5 } });
    }, { t, index: results[t].index });
    await page.waitForFunction(t => window.tileripper.viewer.paintedT === t && window.tileripper.viewer.renderNow().complete, t);
    const rect = await page.locator('#gl-canvas').boundingBox();
    await page.mouse.click(rect.x + rect.width / 2, rect.y + rect.height / 2);
    await page.waitForFunction(t => window.clicked?.t === t, t);
    const clicked = await page.evaluate(() => window.clicked.info);
    assert.equal(clicked.bands[0].value, results[t].value);
    assert.equal(clicked.masked, false);
  }
  await page.evaluate(() => window.tileripper.viewer.fit());
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(root, `data/reports/swot-roanoke-viewer${suffix}.png`) });
  await page.goto(`${server.url}/js/maplibre/index.html?store=${encodeURIComponent(store)}&p=band`);
  try {
    await page.waitForFunction(() => window.chronozarrDemo?.layer.store, null, { timeout: 15000 });
  } catch (error) {
    console.log('MapLibre errors:', errors, await page.locator('body').innerText());
    throw error;
  }
  const sample = await page.evaluate(async ({ index }) => {
    const { layer } = window.chronozarrDemo;
    const { createProjection } = await import('/js/maplibre/projection.js');
    const projection = createProjection(layer.store.crs);
    const [a, , c, , e, f] = layer.store.levels[0].transform;
    const col = index % 512 + 0.5, row = Math.floor(index / 512) + 0.5;
    return layer.getValueAt(projection.toLonLat(c + a * col, f + e * row), { t: 1 });
  }, { index: results[1].index });
  assert.equal(sample.bands[0].value, results[1].value); assert.equal(sample.valid, true);
  assert.deepEqual(errors, []);
  console.log('SWOT browser: both complete float32 chunks and masks bit-exact; signed click values, metre units and MapLibre getValueAt passed.');
} finally {
  await browser.close(); await server.close();
}
